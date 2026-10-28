"""Fit scoring: free plain-code filters first, then a small model scores what's left.

Run: python -m pipeline.score           (one request per job, results right away)
     python -m pipeline.score --batch   (Batch API: half price, usually done within the hour)

Each job moves from status 'new' to 'filtered' (dropped for free), 'low_fit', or 'match'.
"""
import json
import re
import sys
import time

import anthropic

from pipeline import db
from pipeline.resume import load_master
from pipeline.settings import CONFIG, claude
from pipeline.sources import is_local

MODEL = CONFIG["models"]["scoring"]
# shortcut: very long postings are cut to this length (~3k tokens); the requirements are almost
# always near the top. Raise it if scores look off for long postings.
MAX_DESCRIPTION_CHARS = 12_000
SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
    "additionalProperties": False,
}
INSTRUCTIONS = """You rate how well a job posting fits the candidate described below.

The preferences list MAIN TARGET, DESIGN and OTHER roles. Score every role on whether the candidate could
realistically get an interview, then on pay. A role in none of the lists scores low.

Score 0-100:
- 90-100: strong fit. Meets nearly all requirements.
- 70-89: good fit, worth applying. Gaps a hiring manager would accept in an entry-level hire.
- 40-69: long shot. Several missing requirements, but not impossible.
- 0-39: poor fit. Needs a licence, certificate or degree the candidate lacks (e.g. a 309A
  licence, P.Eng, CIRO registration), many years of specific experience, or it is a role type
  not in the preferences (e.g. pure IT support, automotive, healthcare).

Apprentice, helper and first-year trainee postings do not need a licence; score those on the
apprenticeship itself. The candidate lives in Toronto: the GTA or remote within Canada is fine.
Reason: one short sentence (under 20 words) naming the deciding factor."""


def tier_for(title: str) -> str:
    """main / design / other from the job title (plain code: the model mislabelled clear technician jobs)."""
    for tier in ("main", "design"):
        words = CONFIG["scoring"]["tier_words"][tier]
        if re.search(r"\b(" + "|".join(re.escape(w) for w in words) + r")", title or "", re.I):
            return tier
    return "other"


def filter_reason(job) -> str | None:
    """Free checks before any AI call. Returns why the job is dropped, or None to keep it."""
    title = job["title"].lower()
    for term in CONFIG["search"]["exclude_title_terms"]:
        if re.search(rf"(?<![a-z]){re.escape(term.lower())}(?![a-z])", title):
            return f'title contains "{term}"'
    loc = (job["location"] or "").strip()
    # JSearch and the email alerts only cover Canada, so bare "Remote"/"Canada" is fine there.
    if loc and not is_local(loc) and loc.lower() not in ("remote", "canada", "anywhere"):
        return f"not in the GTA: {loc}"
    return None


def profile_text(master: dict) -> str:
    """Short candidate profile from the master resume. Deterministic, so the prompt cache hits."""
    lines = ["Candidate profile", f"Headline: {master['summaries'][0]['headline']}", "Experience:"]
    for j in master["jobs"]:
        lines.append(f"* {j['title']}, {j['company']} ({j['start']} - {j['end']})")
        lines += [f"  - {b['text']}" for b in j["bullets"]]
    lines.append("Projects:")
    for p in master["projects"]:
        lines.append(f"* {p['name']}")
        lines += [f"  - {b['text']}" for b in p["bullets"]]
    lines.append("Education: " + "; ".join(f"{e['credential']}, {e['school']} ({e['years']})"
                                         for e in master["education"]))
    lines.append("Skills: " + "; ".join(f"{g}: {', '.join(s)}" for g, s in master["skills"].items()))
    lines.append("Preferences: " + " ".join(CONFIG["scoring"]["preferences"].split()))
    return "\n".join(lines)


def request_params(system: str, job) -> dict:
    posting = (f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n\n"
               f"{(job['description'] or '(no description)')[:MAX_DESCRIPTION_CHARS]}")
    return {
        "model": MODEL,
        "max_tokens": 300,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "disabled"},  # a 0-100 rating doesn't need reasoning tokens
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": posting}],
    }


def cost_usd(usage, batch: bool = False, model: str = MODEL) -> float:
    price = CONFIG["pricing"][model]
    dollars = (usage.input_tokens * price["input"]
               + (usage.cache_creation_input_tokens or 0) * price["input"] * 1.25
               + (usage.cache_read_input_tokens or 0) * price["input"] * 0.1
               + usage.output_tokens * price["output"]) / 1e6
    return dollars * (0.5 if batch else 1)


def save_score(con, job_id: int, message, batch: bool = False) -> int | None:
    """Stores one model answer. Returns the score, or None if the answer was unusable."""
    u = message.usage
    tokens = u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0) + u.output_tokens
    db.log_usage(con, "claude", tokens, cost_usd(u, batch), note=f"score {MODEL}")
    if message.stop_reason != "end_turn":
        print(f"  job {job_id}: no score (stop reason {message.stop_reason}); will retry next run")
        return None
    data = json.loads(next(b.text for b in message.content if b.type == "text"))
    score = max(0, min(100, int(data["score"])))
    title = con.execute("SELECT title FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    tier = tier_for(title)
    score -=CONFIG["scoring"].get("tier_penalty", {}).get(tier, 0)  # design: just below equal technician jobs
    cap = CONFIG["scoring"].get("tier_caps", {}).get(tier)
    if cap is not None:
        score = min(score, cap)
    score = max(0, score)
    manual =con.execute("SELECT source FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == "manual"
    # Jobs you added yourself are always prepared; the score is shown for information.
    status = "match" if manual or score >= CONFIG["scoring"]["min_fit_score"] else "low_fit"
    db.set_result(con, job_id, status, score, data["reason"])
    return score


def over_budget(con) -> bool:
    _, spent = db.usage_this_month(con, "claude")
    return spent >= CONFIG["limits"]["monthly_budget_usd"]


def run(batch: bool = False):
    con = db.connect()
    with con:  # re-apply the threshold to earlier scores, so changing min_fit_score needs no re-scoring
        con.execute("UPDATE jobs SET status = CASE WHEN fit_score >= ? THEN 'match' ELSE 'low_fit' END "
                    "WHERE status IN ('match', 'low_fit') AND source != 'manual'", (CONFIG["scoring"]["min_fit_score"],))
    keep = []
    for job in con.execute("SELECT * FROM jobs WHERE status = 'new'").fetchall():
        reason = None if job["source"] == "manual" else filter_reason(job)  # you picked it: no filters
        if reason:
            db.set_result(con, job["id"], "filtered", None, reason)
        else:
            keep.append(job)
    print(f"{len(keep)} jobs to score with {MODEL}.")
    if not keep:
        return
    if over_budget(con):
        print("Monthly Claude budget reached; scoring skipped until next month.")
        return

    system = INSTRUCTIONS + "\n\n" + profile_text(load_master())
    client = claude()
    if batch:
        _run_batch(con, client, system, keep)
    else:
        for job in keep:
            if over_budget(con):
                print("Monthly Claude budget reached; stopping.")
                break
            try:
                save_score(con, job["id"], client.messages.create(**request_params(system, job)))
            except anthropic.APIConnectionError as e:
                print(f"Network error, stopping: {e}")
                break
            except anthropic.APIStatusError as e:  # one bad request shouldn't stop the run
                print(f"  job {job['id']}: API error {e.status_code}: {e.message}")
    _summary(con)


def _run_batch(con, client, system, jobs):
    batch = client.messages.batches.create(
        requests=[{"custom_id": str(job["id"]), "params": request_params(system, job)} for job in jobs])
    print(f"Batch {batch.id} submitted; checking every 30 seconds...")
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        time.sleep(30)
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            save_score(con, int(result.custom_id), result.result.message, batch=True)
        else:  # errored/expired jobs stay 'new' and are retried next run
            print(f"  job {result.custom_id}: {result.result.type}")


def _summary(con):
    counts = dict(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    _, spent = db.usage_this_month(con, "claude")
    print(f"Matches: {counts.get('match', 0)}, low fit: {counts.get('low_fit', 0)}, "
          f"filtered: {counts.get('filtered', 0)}, waiting: {counts.get('new', 0)}. "
          f"Claude spend this month: ${spent:.4f}")


if __name__ == "__main__":
    run(batch="--batch" in sys.argv)
