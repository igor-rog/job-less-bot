"""Answer bank for application questions.

Run: python -m pipeline.answers import data/answer_bank.yaml   (add or update your own answers)
     python -m pipeline.answers list                           (drafts and questions waiting on you)
     python -m pipeline.answers approve <id> ["new answer"]    (approve a draft, optionally edited)
     python -m pipeline.answers ask "question" [--job ID]      (try the matcher)

answer_for() is what the form filler calls for every question:
1. Per-job questions ("Why do you want to work here?") are always drafted fresh for that job.
2. Otherwise the question is matched against the bank. Fuzzy matching shortlists the closest
   questions; anything short of a near-exact hit is confirmed by the small model, because character
   similarity alone confuses "salary expectations" with "hours expectations". A confirmed wording is
   saved as an alias, so the next site that phrases it that way matches for free.
3. No match: the stronger model drafts an answer from the master resume and the job, saved as a draft
   for your review. Questions only you can answer (work authorization, salary, availability, licences,
   references, demographics) are never guessed: they're saved as needs_input and left blank.
"""
import argparse
import re
import sys

import yaml
from rapidfuzz import fuzz, process, utils

from pipeline import db
from pipeline.settings import CONFIG

CFG = CONFIG["answers"]
NEAR_EXACT = 97
SHORTLIST_MIN = 30
SHORTLIST_SIZE = 10  # the model sees up to this many candidates; cheap, and avoids missing the right one

SAME_SCHEMA = {"type": "object", "properties": {"same_as": {"type": "integer"}},
               "required": ["same_as"], "additionalProperties": False}
DRAFT_SCHEMA = {"type": "object",
                "properties": {"answer": {"type": "string"}, "needs_input": {"type": "boolean"}},
                "required": ["answer", "needs_input"], "additionalProperties": False}

SAME_PROMPT = """Does the new application question ask for the same information as one of the numbered
questions, so that question's answer fully answers it? Answer with that number, or 0 if none does.
A different country, licence, amount or topic means different. If the new question also asks for
anything more (e.g. "licence and access to a vehicle"), it is different."""

DRAFT_RULES = """You answer one job application question for the candidate, in first person, using only
facts from their master resume below and the job posting.

- Keep it short: one to three sentences, unless the question asks for more. A yes/no question gets
  "Yes" or "No" plus at most one short sentence.
- If options are given, the answer must be exactly one of the options.
- Never invent facts. If the answer depends on something only the candidate knows and the resume
  doesn't state (work authorization, citizenship, salary, availability or start date, notice period,
  licences or certifications not listed, references, relocation, criminal record, demographic or
  equal-opportunity questions, past contact with this employer, willingness to work onsite or shifts),
  set needs_input to true and leave answer empty.
- Judgment calls are the candidate's, not yours: whether they meet a posting's qualifications, how many
  years of experience count as "relevant", or rating their own skill level. Set needs_input to true.
- No phrases like "I am excited", "passionate", "leverage", "proven track record".

Master resume:
"""


def normalize(question: str) -> str:
    return utils.default_process(question)


def is_per_job(question: str) -> bool:
    return any(re.search(p, question, re.I) for p in CFG["per_job_patterns"])


def _bank(con):
    return con.execute("SELECT * FROM answers WHERE per_job = 0 AND job_id IS NULL").fetchall()


def find(con, question: str, client=None):
    """Returns the bank row that answers this question, or None."""
    q = normalize(question)
    rows = _bank(con)
    for r in rows:
        if normalize(r["question"]) == q:
            return r
    alias = con.execute("SELECT answer_id FROM answer_aliases WHERE question = ?", (q,)).fetchone()
    if alias:
        return con.execute("SELECT * FROM answers WHERE id = ?", (alias[0],)).fetchone()
    if not rows:
        return None
    choices = {r["id"]: normalize(r["question"]) for r in rows}
    # Shared words, not WRatio: WRatio's partial matching rates short unrelated questions ~85.
    shortlist = process.extract(q, choices, scorer=fuzz.token_set_ratio, limit=SHORTLIST_SIZE,
                                score_cutoff=SHORTLIST_MIN)
    if not shortlist:
        return None
    best_text, _, best_id = shortlist[0]
    if fuzz.token_sort_ratio(q, best_text) >= NEAR_EXACT:
        return next(r for r in rows if r["id"] == best_id)
    if client is None:  # no model available: only near-exact matches count
        return None
    from pipeline.tailor import _ask

    numbered = "\n".join(f"{i}. {text}" for i, (text, _, _) in enumerate(shortlist, 1))
    verdict = _ask(client, con, CONFIG["models"]["scoring"], SAME_PROMPT,
                   f"New question: {question}\n\n{numbered}", SAME_SCHEMA, 200)
    pick = (verdict or {}).get("same_as", 0)
    if not 1 <= pick <= len(shortlist):
        return None
    answer_id = shortlist[pick - 1][2]
    with con:
        con.execute("INSERT OR IGNORE INTO answer_aliases (question, answer_id) VALUES (?, ?)", (q, answer_id))
    return next(r for r in rows if r["id"] == answer_id)


def _fit_option(answer: str, options: list[str] | None) -> str | None:
    """Maps an answer onto the form's options; None if it doesn't fit any."""
    if not options:
        return answer
    best = process.extractOne(answer, options, scorer=fuzz.WRatio, processor=utils.default_process,
                              score_cutoff=85)
    return best[0] if best else None


def draft(con, client, question: str, job=None, options: list[str] | None = None, per_job: bool = False) -> dict:
    """Asks the stronger model for a draft and saves it to the bank for your review."""
    from pipeline.resume import load_master
    from pipeline.tailor import TAILOR_MODEL, _ask, _posting, master_text

    system = [{"type": "text", "text": DRAFT_RULES + master_text(load_master()), "cache_control": {"type": "ephemeral"}}]
    user = (_posting(job) + "\n\n" if job is not None else "") + f"Question: {question}"
    if options:
        user += "\nOptions: " + " | ".join(options)
    out = _ask(client, con, TAILOR_MODEL, system, user, DRAFT_SCHEMA, 4000) or {"answer": "", "needs_input": True}
    answer = "" if out["needs_input"] else (_fit_option(out["answer"], options) or "")
    status = "needs_input" if out["needs_input"] or not answer else "draft"
    with con:
        cur = con.execute("INSERT INTO answers (question, answer, status, per_job, job_id, source) VALUES (?, ?, ?, ?, ?, 'ai')",
                          (question, answer, status, int(per_job), job["id"] if per_job and job is not None else None))
    return {"answer": answer, "status": status, "answer_id": cur.lastrowid}


def answer_for(con, client, question: str, job=None, options: list[str] | None = None) -> dict:
    """The form filler's entry point. Returns {answer, status, answer_id}; status approved means
    use it as is, draft means fill it in but flag for review, needs_input means leave it blank."""
    if is_per_job(question):
        if job is not None:
            done = con.execute("SELECT * FROM answers WHERE per_job = 1 AND job_id = ? AND question = ?",
                               (job["id"], question)).fetchone()
            if done:
                return {"answer": done["answer"], "status": done["status"], "answer_id": done["id"]}
        return draft(con, client, question, job, options, per_job=True)
    row = find(con, question, client)
    if row is not None:
        answer = _fit_option(row["answer"], options) if row["status"] != "needs_input" else ""
        if answer is not None:
            return {"answer": answer, "status": row["status"], "answer_id": row["id"]}
    return draft(con, client, question, job, options)


REVISE_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
                 "required": ["answer"], "additionalProperties": False}


def revise(con, client, answer_id: int, current: str, change: str) -> str:
    """Rewrites an answer the way you asked ("shorter", "mention the RF project", "drop the last
    sentence"). Facts may come only from the resume, the job, and what you wrote in the change."""
    from pipeline.resume import load_master
    from pipeline.tailor import TAILOR_MODEL, _ask, _posting, master_text

    row = con.execute("SELECT * FROM answers WHERE id = ?", (answer_id,)).fetchone()
    job = con.execute("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)).fetchone() if row["job_id"] else None
    system = [{"type": "text", "text": DRAFT_RULES + master_text(load_master()), "cache_control": {"type": "ephemeral"}}]
    user = ((_posting(job) + "\n\n" if job is not None else "") + f"Question: {row['question']}\n\n"
            f"Current answer:\n{current or '(empty)'}\n\nThe candidate asks for this change:\n{change}\n\n"
            "Rewrite the answer with that change, as one cohesive answer to the question: put new material where it "
            "fits the flow, group related points, and reorder, merge or trim the other sentences so it reads "
            "naturally and stays a sensible length. Keep the current answer's facts unless the change says otherwise. "
            "Facts the candidate states in the change are theirs and may be used; add no other facts. "
            "Return the full new answer.")
    out = _ask(client, con, TAILOR_MODEL, system, user, REVISE_SCHEMA, 4000)
    if out is None:
        return current
    with con:
        con.execute("UPDATE answers SET answer = ?, status = 'draft', source = 'ai', "
                    "updated_at = datetime('now', 'localtime') WHERE id = ?", (out["answer"], answer_id))
    return out["answer"]


def approve(con, answer_id: int, new_answer: str | None = None):
    """Your approval (optionally with your own wording). The answer is reused for every future form."""
    with con:
        if new_answer is not None:
            con.execute("UPDATE answers SET answer = ?, source = 'you' WHERE id = ?", (new_answer, answer_id))
        con.execute("UPDATE answers SET status = 'approved', updated_at = datetime('now', 'localtime') WHERE id = ?",
                    (answer_id,))


def import_file(con, path) -> int:
    """Adds or updates your answers from a YAML list of {question, answer, per_job?}."""
    entries = yaml.safe_load(open(path, encoding="utf-8")) or []
    with con:
        for e in entries:
            row = con.execute("SELECT id FROM answers WHERE question = ? AND job_id IS NULL", (e["question"],)).fetchone()
            answer = str(e.get("answer") or "")
            status = "approved" if answer else "needs_input"
            if row:
                con.execute("UPDATE answers SET answer = ?, status = ?, per_job = ?, source = 'you' WHERE id = ?",
                            (answer, status, int(e.get("per_job", False)), row[0]))
            else:
                con.execute("INSERT INTO answers (question, answer, status, per_job, source) VALUES (?, ?, ?, ?, 'you')",
                            (e["question"], answer, status, int(e.get("per_job", False))))
    return len(entries)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Answer bank")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import").add_argument("path")
    sub.add_parser("list")
    p = sub.add_parser("approve")
    p.add_argument("id", type=int)
    p.add_argument("answer", nargs="?")
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--job", type=int)
    args = parser.parse_args(argv)
    con = db.connect()

    if args.cmd == "import":
        print(f"Imported {import_file(con, args.path)} answers.")
    elif args.cmd == "list":
        for r in con.execute("SELECT * FROM answers WHERE status != 'approved' ORDER BY status, id"):
            print(f"[{r['id']}] {r['status']}: {r['question']}\n      {r['answer'] or '(blank: only you can answer)'}")
    elif args.cmd == "approve":
        approve(con, args.id, args.answer)
        print("Approved.")
    else:
        from pipeline.settings import claude

        job = con.execute("SELECT * FROM jobs WHERE id = ?", (args.job,)).fetchone() if args.job else None
        print(answer_for(con, claude(), args.question, job))
    return 0


if __name__ == "__main__":
    sys.exit(main())
