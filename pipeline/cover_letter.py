"""Cover letters for every tailored resume.

Run: python -m pipeline.cover_letter

1. Style guide: your own writing in writing_samples/*.txt is distilled once into a short style guide
   (data/style_guide.md), rebuilt only when the samples change.
2. Letter: the stronger model writes three short paragraphs from the posting, the bullets already on
   the tailored resume, the style guide, and the master resume (cached).
3. Checks (plain code): banned phrases, dashes, three-item lists, length, numbers and resume bullets
   that don't trace to the master resume, and a company detail that must come from the posting.
   One retry with the problems listed; anything left is flagged for you.
4. Render: a PDF with the resume's header and fonts, plus a .txt for paste-in boxes. Every letter
   starts as needs_edit, so you change a sentence or two in your own words before approving.
"""
import hashlib
import json
import re
from datetime import date
from pathlib import Path

import anthropic
from rapidfuzz import fuzz

from pipeline import db
from pipeline.resume import _env, html_to_pdf, load_master
from pipeline.score import over_budget
from pipeline.settings import CONFIG, DATA, ROOT, claude
from pipeline.tailor import TAILOR_MODEL, _ask, _has_term, _norm, _numbers, _posting, master_text, safe_filename

CFG = CONFIG["cover_letter"]
SAMPLES = ROOT / "writing_samples"
STYLE_GUIDE = DATA / "style_guide.md"
BANNED = ", ".join(f'"{p}"' for p in CFG["banned_phrases"])

STYLE_SCHEMA = {"type": "object", "properties": {"style_guide": {"type": "string"}},
                "required": ["style_guide"], "additionalProperties": False}
LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
        "sign_off": {"type": "string"},
        "company_detail": {"type": "string"},
        "accomplishment_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["greeting", "paragraphs", "sign_off", "company_detail", "accomplishment_ids"],
    "additionalProperties": False,
}

STYLE_PROMPT = f"""Below is writing by one person (emails or cover letters). Describe their voice as a
short style guide: under 150 words, bullet points. Cover tone, sentence and paragraph length, how they
open and close, and typical wording worth keeping. Leave out these phrases even if they use them:
{BANNED}. Do not describe dashes or lists of three as part of their style."""

LETTER_RULES = f"""You write a cover letter in the candidate's own voice, using only facts from their
master resume below.

- Exactly three short paragraphs, under {CFG['max_words']} words in total, plus a greeting and sign-off.
- Paragraph 1: the role, and one specific, real detail about the company or the role taken from the
  posting (what they make, a project, a requirement). company_detail: copy the posting's exact words
  that this detail comes from.
- Paragraph 2: one or two concrete accomplishments from the tailored resume bullets you are given,
  stated plainly. accomplishment_ids: the ids of those bullets.
- Paragraph 3: a short, plain close.
- Only facts from the master resume. Never claim a skill, tool, credential, employer or experience
  that isn't there, and never invent numbers.
- Don't present the candidate as seeking an apprenticeship unless the job title is an apprenticeship.
- Greeting: "Dear <name>," if the posting names the hiring contact, otherwise "Dear Hiring Manager,".
- Never use these phrases: {BANNED}. At most one dash in the whole letter. Don't pack lists of three
  into sentences. Write like a person, not a template.

Candidate's style guide (from their own writing):
"""


def style_guide(client, con) -> str:
    samples = sorted(SAMPLES.glob("*.txt"))
    if not samples:
        raise SystemExit("Add a few emails or cover letters you wrote to writing_samples/*.txt first.")
    text = "\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in samples)
    marker = f"<!-- samples {hashlib.sha256(text.encode()).hexdigest()[:16]} -->"
    if STYLE_GUIDE.exists():
        first, _, body = STYLE_GUIDE.read_text(encoding="utf-8").partition("\n")
        if first == marker:
            return body
    out = _ask(client, con, TAILOR_MODEL, STYLE_PROMPT, text, STYLE_SCHEMA, 4000)
    if out is None:
        raise SystemExit("Could not build the style guide; try again.")
    STYLE_GUIDE.write_text(f"{marker}\n{out['style_guide']}", encoding="utf-8")
    print(f"Style guide saved to {STYLE_GUIDE} (edit it freely; it's rebuilt only if the samples change).")
    return out["style_guide"]


def style_problems(letter: dict) -> list[str]:
    """Plain-code check for AI-sounding writing. Returns problems; empty means it passes."""
    body = " ".join(letter["paragraphs"])
    problems = [f'uses the banned phrase "{p}"' for p in CFG["banned_phrases"] if p.lower() in body.lower()]
    words = len(body.split())
    if words > CFG["max_words"]:
        problems.append(f"{words} words; keep it under {CFG['max_words']}")
    if len(letter["paragraphs"]) != 3:
        problems.append(f"{len(letter['paragraphs'])} paragraphs; write exactly three")
    dashes = len(re.findall(r"[—–]| - ", body))
    if dashes > CFG["max_dashes"]:
        problems.append(f"{dashes} dashes; use at most {CFG['max_dashes']}")
    sentences = re.split(r"(?<=[.!?])\s+", body)
    # "A, B, and C" with short items (up to 5 words each); an opening clause plus "and" doesn't count.
    # shortcut: lists without the comma before "and" look like an opening clause, so they're not counted.
    item = r"(?:[\w'/&-]+ ){0,4}[\w'/&-]+"
    lists = [s for s in sentences if re.search(rf"{item}, {item}, (?:and|or) {item}", s)]
    if len(lists) > CFG["max_list_sentences"]:
        problems.append(f"{len(lists)} sentences with lists of three; use at most {CFG['max_list_sentences']}")
    return problems


def honesty_problems(master: dict, letter: dict, posting: str, terms: list[str],
                     job_title: str = "") -> tuple[list[str], list[str]]:
    """Returns (problems, notes). Problems: things that can't be traced. Notes: posting terms the
    letter mentions that aren't in the master resume (fine when describing the company, not as a skill)."""
    problems, notes = [], []
    bullets = {b["id"] for e in master["jobs"] + master["projects"] for b in e["bullets"]}
    unknown = [i for i in letter["accomplishment_ids"] if i not in bullets]
    if unknown:
        problems.append(f"accomplishment ids not in the master resume: {unknown}")
    if not letter["accomplishment_ids"]:
        problems.append("no accomplishment from the resume")
    detail = _norm(letter["company_detail"])
    if not detail or fuzz.partial_ratio(detail, _norm(posting)) < 85:
        problems.append("the company detail isn't from the posting")
    body = " ".join(letter["paragraphs"])
    if "apprentice" in body.lower() and "apprentice" not in job_title.lower():
        problems.append("mentions an apprenticeship, but this job isn't one")
    whole_master = master_text(master)
    added = _numbers(body) - _numbers(whole_master) - _numbers(posting)
    if added:
        problems.append(f"numbers not in the master resume or posting: {sorted(added)}")
    for term in terms[1:]:  # terms[0] is the job title; naming the role you apply for is expected
        if _has_term(body, term) and not _has_term(whole_master, term):
            notes.append(f'mentions "{term}" from the posting; make sure it isn\'t claimed as your skill')
    return problems, notes


def tidy(letter: dict) -> dict:
    """The model sometimes repeats the sign-off at the end of the last paragraph; drop it there."""
    last = letter["paragraphs"][-1].rstrip() if letter["paragraphs"] else ""
    sign_off = letter["sign_off"].strip()
    if sign_off and last.lower().endswith(sign_off.lower()):
        letter["paragraphs"][-1] = last[:-len(sign_off)].rstrip()
    return letter


def render_letter(master: dict, job, letter: dict) -> tuple[Path, Path]:
    """Writes the PDF (resume's header and fonts) and the paste-in .txt. Also used after you edit a letter."""
    folder = ROOT / "output" / "applications" / str(job["id"])
    stem = f"{safe_filename(job['company'])} - {safe_filename(job['title'])} - Cover Letter"
    pdf, txt = folder / f"{stem}.pdf", folder / f"{stem}.txt"
    today = date.today()
    html_to_pdf(_env.get_template("cover_letter.html.j2").render(
        contact=master["contact"], letter=letter, company=job["company"],
        date=f"{today:%B} {today.day}, {today.year}"), pdf)
    txt.write_text(letter_text(letter, master["contact"]["name"]), encoding="utf-8")
    return pdf, txt


def letter_text(letter: dict, name: str) -> str:
    return "\n\n".join([letter["greeting"], *letter["paragraphs"], f"{letter['sign_off']}\n{name}"]) + "\n"


def write_letter(con, client, master: dict, system: list, job, app) -> str:
    posting = _posting(job)
    tailoring = json.loads(app["tailoring"])
    terms = json.loads(app["keywords"])
    request = posting + "\n\nBullets on the tailored resume:\n" + "\n".join(
        f"- {b['id']}: {b['text']}" for b in tailoring["bullets"])
    letter, problems, notes = None, [], []
    for attempt in range(2):
        user = request if attempt == 0 else (
            request + "\n\nYour previous letter was rejected by the automated check. Fix these:\n- "
            + "\n- ".join(problems))
        letter = _ask(client, con, TAILOR_MODEL, system, user, LETTER_SCHEMA, 8000)
        if letter is None:
            return "error"
        letter = tidy(letter)
        honesty, notes = honesty_problems(master, letter, posting, terms, job["title"])
        problems = style_problems(letter) + honesty
        if not problems:
            break

    pdf, txt = render_letter(master, job, letter)
    flag ="; ".join([f"Check: {p}" for p in problems] + notes) or None
    with con:
        con.execute("UPDATE applications SET letter = ?, letter_path = ?, letter_text_path = ?, letter_flag = ?, "
                    "letter_status = 'needs_edit' WHERE job_id = ?",
                    (json.dumps(letter), str(pdf), str(txt), flag, job["id"]))
    words = len(" ".join(letter["paragraphs"]).split())
    print(f"  {words} words" + (f" | {flag[:140]}" if flag else ""))
    return "flagged" if problems else "ok"


def run():
    con = db.connect()
    rows = con.execute("SELECT a.job_id, a.tailoring, a.keywords FROM applications a JOIN jobs j ON j.id = a.job_id "
                       "WHERE a.resume_path IS NOT NULL AND a.letter IS NULL ORDER BY j.fit_score DESC").fetchall()
    print(f"{len(rows)} cover letters to write with {TAILOR_MODEL}.")
    if not rows or over_budget(con):
        if rows:
            print("Monthly Claude budget reached; skipping.")
        return
    client = claude()
    master = load_master()
    system = [{"type": "text", "text": LETTER_RULES + style_guide(client, con) + "\n\nMaster resume:\n"
               + master_text(master), "cache_control": {"type": "ephemeral"}}]
    for app in rows:
        if over_budget(con):
            print("Monthly Claude budget reached; stopping.")
            break
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (app["job_id"],)).fetchone()
        print(f"- {job['title']} @ {job['company']}")
        try:
            write_letter(con, client, master, system, job, app)
        except anthropic.APIConnectionError as e:
            print(f"Network error, stopping: {e}")
            break
        except anthropic.APIStatusError as e:  # letter stays empty, so the next run retries it
            print(f"  API error {e.status_code}: {e.message}")
    _, spent = db.usage_this_month(con, "claude")
    print(f"Claude spend this month: ${spent:.4f}")


if __name__ == "__main__":
    run()
