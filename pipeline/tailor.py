"""Resume tailoring for every matched job.

Run: python -m pipeline.tailor

1. Keywords: the small model pulls ATS terms (skills, tools, certifications, exact title) from the posting.
2. Select and reword: the stronger model picks bullet IDs, skills and a summary from the master resume,
   lightly rewording bullets to the posting's phrases where they truthfully match.
3. Validate (plain code): every bullet ID, skill and summary must exist in the master resume, and
   reworded text may not add numbers, drift far from the original, or grow much longer. One retry
   with the problems listed, then the job is flagged for you instead of rendered. Posting phrases
   swapped in that the master never uses are rendered but noted for you to check.
4. Keyword report: "18 of 22 terms" plus the missing terms, shown as gaps, never added to the resume.
5. Render the PDF to output/applications/<job id>/<Company> - <Title>.pdf
"""
import json
import re
from pathlib import Path

import anthropic
import yaml
from rapidfuzz import fuzz

from pipeline import db
from pipeline.resume import build_html, html_to_pdf, load_master
from pipeline.score import cost_usd, over_budget, tier_for
from pipeline.settings import CONFIG, ROOT, claude

KEYWORD_MODEL = CONFIG["models"]["scoring"]
TAILOR_MODEL = CONFIG["models"]["tailoring"]
OUTPUT = ROOT / "output" / "applications"
MAX_DESCRIPTION_CHARS = 12_000  # same cut as scoring; requirements sit near the top

KEYWORD_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "terms": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "terms"], "additionalProperties": False,
}
TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_id": {"type": "string"},
        "summary": {"type": "string"},
        "bullets": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"], "additionalProperties": False}},
        "skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary_id", "summary", "bullets", "skills"], "additionalProperties": False,
}

KEYWORD_PROMPT = """List the terms an applicant tracking system would match for this job posting:
hard skills, tools, software, equipment, standards, certifications and licences, written exactly as
the posting writes them. 8-25 short terms. Skip soft skills ("team player") and benefits.
Also give the job title exactly as written."""

TAILOR_RULES = """You tailor a resume for one job posting, using ONLY the candidate's master resume below.

Rules (an automated check rejects any answer that breaks them):
- summary_id: the id of the summary that best fits. A summary with only_if_title_has may be used only
  when the job title contains one of those words. summary: that summary's text, lightly reworded to
  use the posting's wording. Add no facts, tools, numbers or credentials.
- bullets: the most relevant bullet ids, most relevant first, from any job or project. How many, and
  the page limit the resume must fit, are given with the posting. Keep each bullet's meaning and facts. Applicant tracking systems match
  exact wording, so wherever the posting's exact phrase truthfully names what the bullet already
  describes, use the posting's phrase (e.g. "stock tracking" -> "inventory management", "identify and
  fix" -> "root cause analysis"). Never add a tool, skill, number, responsibility or result that the
  original bullet doesn't state. Keep about the same length.
- skills: 10-25 skills copied exactly, character for character, from the master resume's skills list.
- Never include a posting term the candidate doesn't have. Missing terms are reported separately.
- Never write the posting's job title as if it were the candidate's own title.

Master resume:
"""


def master_text(master: dict) -> str:
    # No contact details: the model doesn't need them. Deterministic for prompt caching.
    return yaml.safe_dump({k: v for k, v in master.items() if k != "contact"}, sort_keys=False,
                          allow_unicode=True, width=120)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#./ ]+", " ", text.lower()).split())


def _has_term(text: str, term: str) -> bool:
    t, n = _norm(term), _norm(text)
    return bool(t) and (t in n or (len(t) > 4 and fuzz.partial_ratio(t, n) >= 90))


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def validate(master: dict, result: dict, terms: list[str], job_title: str = "") -> tuple[list[str], list[str]]:
    """Plain-code honesty check. Returns (problems, notes): any problem blocks the resume; notes are
    posting phrases swapped in that the master resume never uses (e.g. "inventory management" for
    "stock tracking"). Code can't tell a true synonym from an invented skill, so those go to you."""
    problems, notes = [], []
    bullets = {b["id"]: b["text"] for e in master["jobs"] + master["projects"] for b in e["bullets"]}
    summaries = {s["id"]: s["text"] for s in master["summaries"]}
    skills = {s for group in master["skills"].values() for s in group}
    whole_master = master_text(master)

    def check_rewrite(label: str, original: str, new: str):
        added = _numbers(new) - _numbers(original)
        if added:
            problems.append(f"{label}: adds numbers not in the original: {sorted(added)}")
        if fuzz.token_set_ratio(_norm(original), _norm(new)) < 60:
            problems.append(f"{label}: reworded too far from the original")
        if len(new) > len(original) * 1.6 + 40:
            problems.append(f"{label}: much longer than the original")
        for term in terms:
            if _has_term(new, term) and not _has_term(original, term) and not _has_term(whole_master, term):
                notes.append(f'{label} now says "{term}", which your master resume never uses; check it is accurate')

    if result["summary_id"] not in summaries:
        problems.append(f"unknown summary_id {result['summary_id']!r}")
    else:
        check_rewrite("summary", summaries[result["summary_id"]], result["summary"])
        only_if = next(s for s in master["summaries"] if s["id"] == result["summary_id"]).get("only_if_title_has")
        if only_if and not any(w.lower() in job_title.lower() for w in only_if):
            problems.append(f"summary {result['summary_id']} is only for job titles containing {only_if}; pick another")
    seen = set()
    for b in result["bullets"]:
        if b["id"] not in bullets:
            problems.append(f"unknown bullet id {b['id']!r}")
        elif b["id"] in seen:
            problems.append(f"bullet {b['id']} used twice")
        else:
            check_rewrite(f"bullet {b['id']}", bullets[b["id"]], b["text"])
        seen.add(b["id"])
    if not result["bullets"]:
        problems.append("no bullets chosen")
    problems += [f'skill "{s}" is not in the master skills list' for s in result["skills"] if s not in skills]
    return problems, notes


def keyword_report(terms: list[str], result: dict) -> tuple[int, list[str]]:
    text = " ".join([result["summary"], *(b["text"] for b in result["bullets"]), *result["skills"]])
    missing = [t for t in terms if not _has_term(text, t)]
    return len(terms) - len(missing), missing


def _ask(client, con, model: str, system: list | str, user: str, schema: dict, max_tokens: int) -> dict | None:
    if over_budget(con):  # every Sonnet/Haiku call except scoring comes through here
        raise SystemExit(f"Monthly Claude budget of ${CONFIG['limits']['monthly_budget_usd']:.2f} reached; "
                         "raise it in Settings to continue.")
    params = dict(model=model, max_tokens=max_tokens, system=system,
                  output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                  messages=[{"role": "user", "content": user}])
    if model == KEYWORD_MODEL:
        msg = client.messages.create(thinking={"type": "disabled"}, **params)  # extraction needs no reasoning
    else:
        # If a safety classifier wrongly declines, the API re-runs the request on its recommended
        # fallback model. shortcut: cost is logged at this model's price even then; rare.
        msg = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    u = msg.usage
    tokens = u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0) + u.output_tokens
    db.log_usage(con, "claude", tokens, cost_usd(u, model=model), note=f"tailor {model}")
    if msg.stop_reason != "end_turn":
        print(f"  no answer (stop reason {msg.stop_reason})")
        return None
    return json.loads(next(b.text for b in msg.content if b.type == "text"))


def _posting(job) -> str:
    return (f"Title: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n\n"
            f"{(job['description'] or '(no description)')[:MAX_DESCRIPTION_CHARS]}")


def safe_filename(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", text).strip()[:80] or "job"


def page_count(pdf: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))


def render_to_fit(master: dict, result: dict, path: Path, pages: int = 1, min_bullets: int = 5) -> Path:
    """Renders the PDF, dropping the least relevant bullets (the model lists them last) until it fits
    in `pages` pages. Dropped bullets are removed from `result` too, so the stored tailoring matches the PDF."""
    while True:
        html_to_pdf(build_html(master, summary_id=result["summary_id"],
                               bullet_ids=[b["id"] for b in result["bullets"]], skills=result["skills"],
                               rewrites={b["id"]: b["text"] for b in result["bullets"]},
                               summary_text=result["summary"]), path)
        if page_count(path) <= pages or len(result["bullets"]) <= min_bullets:
            return path
        result["bullets"].pop()


def tailor_job(con, client, master: dict, system: list, job) -> str:
    """Tailors one job. Returns 'tailored' or 'flagged'."""
    posting = _posting(job)
    kw = _ask(client, con, KEYWORD_MODEL, KEYWORD_PROMPT, posting, KEYWORD_SCHEMA, 2000)
    if kw is None:
        return "error"
    terms = list(dict.fromkeys([kw["title"], *kw["terms"]]))  # title first, no duplicates

    # Technician and electrical jobs get 2 pages, so hands-on and GitHub projects fit; everything else 1.
    pages = 2 if tier_for(job["title"]) == "main" else 1
    request = (f"{posting}\n\nPosting terms to use where they truthfully fit: {', '.join(terms)}\n\n" +
               ("Page limit: 2 pages. Choose 12-18 bullets, and include the relevant hands-on and GitHub projects."
                if pages == 2 else "Page limit: 1 page. Choose 6-10 bullets."))
    result, problems = None, []
    for attempt in range(2):
        user = request if attempt == 0 else (
            request + "\n\nYour previous answer was rejected by the automated check for these reasons. "
            "Fix them:\n- " + "\n- ".join(problems))
        result = _ask(client, con, TAILOR_MODEL, system, user, TAILOR_SCHEMA, 8000)
        if result is None:
            return "error"
        problems, notes = validate(master, result, terms, job["title"])
        if not problems:
            break

    path = None
    if problems:
        flag = "Blocked: failed the honesty check twice: " + "; ".join(problems)
    else:
        flag = "; ".join(notes) or None
        path = OUTPUT / str(job["id"]) / f"{safe_filename(job['company'])} - {safe_filename(job['title'])}.pdf"
        render_to_fit(master, result, path, pages)
    matched, missing = keyword_report(terms, result)  # after rendering: counts only what's on the page
    status = "flagged" if problems else "tailored"
    with con:
        con.execute(
            "INSERT INTO applications (job_id, tailoring, keywords, keywords_matched, keywords_total, "
            "missing_keywords, resume_path, flag) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET tailoring = excluded.tailoring, keywords = excluded.keywords, "
            "keywords_matched = excluded.keywords_matched, keywords_total = excluded.keywords_total, "
            "missing_keywords = excluded.missing_keywords, flag = excluded.flag, "
            "resume_path = COALESCE(excluded.resume_path, applications.resume_path)",  # a blocked redo keeps the old PDF
            # re-tailoring keeps the cover letter, form answers and status
            (job["id"], json.dumps(result), json.dumps(terms), matched, len(terms), json.dumps(missing),
             str(path) if path else None, flag))
        con.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job["id"]))
    print(f"  {status}: {matched} of {len(terms)} terms" + (f" | {flag[:120]}" if flag else ""))
    return status


def run():
    con = db.connect()
    jobs = con.execute("SELECT * FROM jobs WHERE status = 'match' ORDER BY fit_score DESC").fetchall()
    cap = CONFIG["limits"]["daily_application_cap"]
    jobs = jobs[:cap] if cap else jobs
    print(f"{len(jobs)} matched jobs to tailor with {TAILOR_MODEL}.")
    if not jobs:
        return
    master = load_master()
    system = [{"type": "text", "text": TAILOR_RULES + master_text(master), "cache_control": {"type": "ephemeral"}}]
    client = claude()
    for job in jobs:
        if over_budget(con):
            print("Monthly Claude budget reached; stopping.")
            break
        print(f"- {job['title']} @ {job['company']} (fit {job['fit_score']})")
        try:
            tailor_job(con, client, master, system, job)
        except anthropic.APIConnectionError as e:
            print(f"Network error, stopping: {e}")
            break
        except anthropic.APIStatusError as e:  # leave the job as 'match' so the next run retries it
            print(f"  API error {e.status_code}: {e.message}")
    _, spent = db.usage_this_month(con, "claude")
    print(f"Claude spend this month: ${spent:.4f}")


if __name__ == "__main__":
    run()
