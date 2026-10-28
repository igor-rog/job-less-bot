"""Form filling. Greenhouse and Lever forms are filled automatically; everything else is "apply manually".

Run: python -m pipeline.apply          (prepare every application that has a resume and cover letter)

prepare: opens the form in a headless browser, fills your details, uploads the tailored resume and
cover letter, answers each question from the answer bank, saves a full-page screenshot and a log,
and closes WITHOUT submitting.
submit: only through approve_and_submit(), which the dashboard's Approve button calls. It fills the form
again the same way and clicks submit. If a captcha challenge appears, it stops and the job becomes
"apply manually"; captchas are never bypassed.

Demographic / equal-opportunity questions are never answered automatically.
Workday, SuccessFactors, Indeed, LinkedIn, Job Bank and others: marked "apply manually" with the direct
link; the tailored resume, cover letter and answers are ready to copy.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.sync_api import sync_playwright

from pipeline import answers, db
from pipeline.resume import load_master
from pipeline.settings import ROOT, claude
from pipeline.sources import USER_AGENT, apply_system_for

AUTOFILL = {"greenhouse", "lever"}
DEMOGRAPHIC = re.compile(r"gender|race|ethnic|veteran|disabilit|sexual orientation|pronoun|transgender|"
                         r"indigenous|aboriginal|visible minorit|demographic", re.I)
CAPTCHA_CHALLENGE = "iframe[src*='recaptcha'][src*='bframe'], iframe[title*='challenge' i], iframe[src*='hcaptcha'][src*='challenge']"
DONE_TEXT = re.compile(r"thank you|thanks for applying|application (has been )?(received|submitted)|we('ve| have) received", re.I)


class Form:
    """One fill attempt: the page plus a step log and the answers used."""

    def __init__(self, page, job_id: int):
        self.page, self.job_id = page, job_id
        self.log_lines: list[str] = []
        self.filled: list[dict] = []   # {question, answer, status}
        self.missing: list[str] = []   # required questions left blank

    def log(self, msg: str):
        self.log_lines.append(f"{datetime.now():%H:%M:%S} {msg}")

    def answer(self, con, client, job, label: str, options, required: bool) -> str:
        if DEMOGRAPHIC.search(label):
            self.log(f"skipped demographic question: {label}")
            if required:  # never answered for you, but a required one must not be left silently
                self.missing.append(label)
            return ""
        res = answers.answer_for(con, client, label, job, options or None)
        self.filled.append({"question": label, "answer": res["answer"], "status": res["status"]})
        self.log(f"{res['status']}: {label} -> {res['answer'][:80]!r}")
        if not res["answer"] and required:
            self.missing.append(label)
        return res["answer"]


def personal(master: dict) -> dict:
    c = master["contact"]
    first, _, last = c["name"].partition(" ")
    links = {re.sub(r"^www\.", "", urlparse("https://" + u).netloc).split(".")[0]: "https://" + u for u in c["links"]}
    current = next((j["company"] for j in master["jobs"] if j["end"] == "Present" and j["company"] != "Self-employed"), "")
    a = c.get("address") or {}
    return {"first": first, "last": last, "name": c["name"], "email": c["email"], "phone": c["phone"],
            "location": c["location"], "company": current, "linkedin": links.get("linkedin", ""),
            "github": links.get("github", ""), "street": a.get("street", ""), "city": a.get("city", ""),
            "province": a.get("province", ""), "postal_code": a.get("postal_code", ""),
            "country": a.get("country", "")}


# ---------- Greenhouse ----------

def greenhouse_ids(url: str) -> tuple[str, str] | None:
    m = re.search(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    jid = parse_qs(urlparse(url).query).get("gh_jid")
    token = parse_qs(urlparse(url).query).get("for")
    return (token[0], jid[0]) if jid and token else None


def greenhouse_questions(token: str, job_id: str) -> list[dict]:
    r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}", params={"questions": "true"},
                  headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("questions", []) + data.get("location_questions", [])


GH_STANDARD = {"first_name", "last_name", "email", "phone", "resume", "resume_text", "cover_letter", "cover_letter_text"}


def pick_option(page, selector: str, text: str):
    """Greenhouse dropdowns are searchable comboboxes: open, type, click the exact option."""
    box = page.locator(selector)
    box.click()
    box.fill(text)
    page.get_by_role("option", name=text, exact=True).first.click(timeout=5000)


def upload(form: Form, selector: str, path: str, label: str) -> bool:
    """Attaches a file and confirms the page actually shows it."""
    page, name = form.page, Path(path).name
    page.set_input_files(selector, path)
    try:
        page.get_by_text(name, exact=False).first.wait_for(timeout=10_000)
        form.log(f"attached {label}")
        return True
    except Exception:
        form.log(f"{label} not shown as attached")
        return False


def fill_greenhouse(form: Form, con, client, job, app, me: dict, form_url: str, questions: list[dict]):
    page = form.page
    page.goto(form_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#first_name", timeout=30_000)
    form.log(f"opened {form_url}")
    # Resume first: Greenhouse may read it and prefill fields, which ours then overwrite.
    # Its resume uploader starts a few seconds after the page loads; attaching earlier breaks the field
    # until the page reloads, so wait, and on failure reload once and try again.
    for attempt in range(2):
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass  # some boards keep connections open
        page.wait_for_timeout(4000)
        if upload(form, "#resume", app["resume_path"], "resume"):
            break
        if attempt == 0:
            form.log("reloading the form to retry the resume upload")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#first_name", timeout=30_000)
    else:
        form.missing.append("Resume/CV (upload failed)")
    for field, value in [("first_name", me["first"]), ("last_name", me["last"]), ("email", me["email"]), ("phone", me["phone"])]:
        page.fill(f"#{field}", value)
    if page.locator("#country").count():  # phone country picker
        try:
            page.locator("#country").click()
            page.locator("#country").fill("Canada")
            page.get_by_role("option", name=re.compile(r"^\s*Canada")).first.click(timeout=5000)
        except Exception as e:
            form.log(f"could not set country: {e!r}"[:300])
    if app["letter_path"] and page.locator("#cover_letter").count():
        upload(form, "#cover_letter", app["letter_path"], "cover letter")
    for q in questions:
        for f in q["fields"]:
            if f["name"] in GH_STANDARD or f["type"] == "input_file":
                continue
            selector = "#" + f["name"].removesuffix("[]")
            if f["type"] == "input_text" and re.search(r"\b(location|city)\b", q["label"], re.I):
                try:  # location autocomplete: type the city, click the first suggestion if one appears.
                    # Never press Enter here: with no suggestion open, Enter would submit the form.
                    page.fill(selector, me["location"])
                    suggestion = page.get_by_role("option").first
                    try:
                        suggestion.click(timeout=2500)
                    except Exception:
                        pass  # no suggestions: the typed city stays
                    form.filled.append({"question": q["label"].strip(), "answer": me["location"], "status": "approved"})
                except Exception as e:
                    form.log(f"could not set location: {e!r}"[:300])
                continue
            options = [v["label"] for v in f.get("values", [])]
            value = form.answer(con, client, job, q["label"].strip(), options, q.get("required", False))
            if not value:
                continue
            try:
                if f["type"].startswith("multi_value"):
                    pick_option(page, selector, value)
                else:
                    page.fill(selector, value)
            except Exception as e:  # keep going; the screenshot and note show what's missing
                form.log(f"could not set {q['label'][:60]!r}: {e!r}"[:300])
                if q.get("required"):
                    form.missing.append(q["label"].strip())
    verify_greenhouse(form)


def verify_greenhouse(form: Form):
    """Reads the form back: a required field that isn't actually filled counts as missing, whatever the
    earlier steps reported."""
    page = form.page
    for field, label in [("first_name", "First Name"), ("last_name", "Last Name"), ("email", "Email"), ("phone", "Phone")]:
        if not page.locator(f"#{field}").input_value().strip():
            form.missing.append(f"{label} (empty on the form)")
    if page.locator("#country").count() and not page.locator("#country").input_value().strip() \
            and not page.get_by_text("Canada", exact=False).count():
        form.missing.append("Country (empty on the form)")
    form.log("verified required fields" + (f"; missing: {form.missing}" if form.missing else ""))


# ---------- Lever ----------

def lever_apply_url(url: str) -> str:
    return url.split("?")[0].rstrip("/").removesuffix("/apply") + "/apply"


LEVER_QUESTIONS_JS = """() => {
  const groups = {};
  for (const el of document.querySelectorAll("form [name^='cards[']")) {
    if (el.type === 'hidden') continue;
    const q = el.closest('li.application-question, .application-question');
    const label = q ? (q.querySelector('.application-label, .text') || q).innerText.replace(/\\s+/g, ' ').replace('✱', '').trim() : el.name;
    const g = groups[el.name] || (groups[el.name] = {name: el.name, type: el.type, label, required: el.required, options: []});
    if (el.type === 'radio' || el.type === 'checkbox') g.options.push(el.value);
    if (el.tagName === 'SELECT') { g.type = 'select'; g.options = [...el.options].map(o => o.text).filter(t => t.trim()); }
    if (el.required) g.required = true;
  }
  return Object.values(groups);
}"""


def fill_lever(form: Form, con, client, job, app, me: dict, form_url: str, letter_text: str):
    page = form.page
    page.goto(form_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("input[name='name']", timeout=30_000)
    form.log(f"opened {form_url}")
    page.set_input_files("input[name='resume']", app["resume_path"])
    page.wait_for_timeout(3000)  # Lever parses the resume and may prefill fields; ours overwrite them
    form.log("uploaded resume")
    for name, value in [("name", me["name"]), ("email", me["email"]), ("phone", me["phone"]),
                        ("location", me["location"]), ("org", me["company"]),
                        ("urls[LinkedIn]", me["linkedin"]), ("urls[GitHub]", me["github"])]:
        el = page.locator(f"input[name='{name}']")
        if value and el.count():
            el.fill(value)
    if letter_text and page.locator("textarea[name='comments']").count():
        page.fill("textarea[name='comments']", letter_text)
        form.log("pasted cover letter into additional information")
    for q in page.evaluate(LEVER_QUESTIONS_JS):
        value = form.answer(con, client, job, q["label"], q["options"], q["required"])
        if not value:
            continue
        try:
            if q["type"] in ("radio", "checkbox"):
                page.check(f"input[name='{q['name']}'][value='{value}']")
            elif q["type"] == "select":
                page.select_option(f"select[name='{q['name']}']", label=value)
            else:
                page.fill(f"[name='{q['name']}']", value)
        except Exception as e:
            form.log(f"could not set {q['label'][:60]!r}: {e!r}"[:300])
            if q["required"]:
                form.missing.append(q["label"])


# ---------- prepare / submit ----------

SUBMIT_BUTTON = "button[type='submit'], #btn-submit, button:has-text('Submit application')"


def _folder(job_id):
    path = ROOT / "output" / "applications" / str(job_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_one(con, client, job, app, submit: bool = False, form_url: str | None = None,
                questions: list[dict] | None = None, headless: bool = True) -> str:
    """Fills one application. Returns the new apply_status. Never submits unless submit=True."""
    system = job["apply_system"]
    if system not in AUTOFILL:
        note = f"Apply manually on {urlparse(job['url']).netloc}: {job['url']}"
        with con:
            con.execute("UPDATE applications SET apply_status = 'manual', apply_note = ? WHERE job_id = ?", (note, job["id"]))
        return "manual"

    master, folder = load_master(), _folder(job["id"])
    me = personal(master)
    letter_text = open(app["letter_text_path"], encoding="utf-8").read() if app["letter_text_path"] else ""
    status, note = "failed", None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
        form = Form(page, job["id"])
        try:
            if system == "greenhouse":
                ids = greenhouse_ids(job["url"])
                if not ids and form_url is None:
                    raise ValueError("can't find the Greenhouse board and job id in the link")
                url = form_url or f"https://job-boards.greenhouse.io/{ids[0]}/jobs/{ids[1]}"
                fill_greenhouse(form, con, client, job, app, me, url,
                                questions if questions is not None else greenhouse_questions(*ids))
            else:
                fill_lever(form, con, client, job, app, me, form_url or lever_apply_url(job["url"]), letter_text)
            page.screenshot(path=str(folder / "form.png"), full_page=True)
            form.log("saved screenshot")
            if form.missing:
                status, note = "needs_answers", "Answer these, then prepare again: " + " | ".join(form.missing)
            elif not submit:
                status = "filled"
                drafts = [f["question"] for f in form.filled if f["status"] == "draft"]
                note = ("Review AI-drafted answers: " + " | ".join(drafts)) if drafts else None
            else:
                status, note = _submit(form, headed=not headless)
        except Exception as e:
            form.log(f"error: {e!r}"[:500])
            note = f"Form filling failed ({type(e).__name__}); apply manually: {job['url']}"
            try:
                page.screenshot(path=str(folder / "form.png"), full_page=True)
            except Exception:
                pass
        finally:
            browser.close()

    log_path = folder / "apply.log"
    log_path.write_text("\n".join(form.log_lines) + "\n", encoding="utf-8")
    with con:
        con.execute("UPDATE applications SET apply_status = ?, apply_note = ?, form_answers = ?, screenshot_path = ?, "
                    "apply_log_path = ?, submitted_at = CASE WHEN ? = 'submitted' THEN datetime('now', 'localtime') "
                    "ELSE submitted_at END WHERE job_id = ?",
                    (status, note, json.dumps(form.filled), str(folder / "form.png"), str(log_path), status, job["id"]))
    print(f"  {status}" + (f" | {note[:140]}" if note else ""))
    return status


def _submit(form: Form, headed: bool = False) -> tuple[str, str | None]:
    """Clicks submit once. If the site then asks for an email security code or a captcha, a visible
    window waits up to 10 minutes for you to finish it yourself; a hidden one hands the job to you."""
    page = form.page
    # Postings often say "Thank you for your interest" already; only a success phrase that appears
    # after the click counts.
    seen_before = len(list(DONE_TEXT.finditer(page.inner_text("body"))))
    page.locator(SUBMIT_BUTTON).first.click()
    form.log("clicked submit (approved by you)")
    asked = False
    for second in range(600 if headed else 20):
        page.wait_for_timeout(1000)
        body = page.inner_text("body")
        if len(list(DONE_TEXT.finditer(body))) > seen_before:
            form.log("confirmation shown")
            page.screenshot(path=str(_folder(form.job_id) / "submitted.png"), full_page=True)
            return "submitted", None
        check = "security code" in body.lower() or any(f.is_visible() for f in page.locator(CAPTCHA_CHALLENGE).all())
        if check and not headed:
            form.log("the site asked for a security code / captcha; stopped")
            return "manual", "The site asked for a security check on submit; submit this one yourself from the form link."
        if check and not asked:
            asked = True
            form.log("the site asked for a security code / captcha: waiting for you to finish it in the window")
            print("  In the browser window: enter the security code from your email (or complete the check), "
                  "then press Submit. Waiting up to 10 minutes...", flush=True)
        if not check and not asked and second >= 20:
            break
    return "failed", "No confirmation after submitting; check your email or the company's site before retrying."


def approve_and_submit(con, client, job_id: int, **kwargs) -> str:
    """The only way anything is submitted: called by the dashboard's Approve button."""
    job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    with con:
        con.execute("UPDATE applications SET approved_at = datetime('now', 'localtime') WHERE job_id = ?", (job_id,))
    app = con.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if app["apply_status"] != "filled":
        raise ValueError(f"Not ready to submit (status: {app['apply_status']}). Prepare it and fix what's flagged first.")
    # Visible window: if the site asks for a security code or captcha, you finish it yourself.
    return prepare_one(con, client, job, app, submit=True, **{"headless": False, **kwargs})


def mark_submitted(con, job_id: int):
    """For jobs you applied to yourself (manual)."""
    with con:
        con.execute("UPDATE applications SET apply_status = 'submitted', submitted_at = datetime('now', 'localtime') "
                    "WHERE job_id = ?", (job_id,))


def relink(con, client, job_id: int, url: str) -> str:
    """Points a job at the employer's own posting (found from a job-board copy) and prepares it again."""
    if not url.startswith("http"):
        raise SystemExit("That doesn't look like a link.")
    with con:
        con.execute("UPDATE jobs SET url = ?, apply_system = ? WHERE id = ?", (url, apply_system_for(url), job_id))
        con.execute("UPDATE applications SET apply_status = NULL, apply_note = NULL WHERE job_id = ?", (job_id,))
    job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    app = con.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    status = prepare_one(con, client, job, app)
    return f"Now applying through {job['apply_system']}: {status}"


def run():
    con = db.connect()
    # Approved means Submit was already clicked: never re-arm it for a second send.
    rows = con.execute("SELECT * FROM applications WHERE resume_path IS NOT NULL AND letter IS NOT NULL "
                       "AND (apply_status IS NULL OR apply_status IN ('needs_answers', 'failed')) "
                       "AND approved_at IS NULL").fetchall()
    print(f"{len(rows)} applications to prepare.")
    client = claude()
    for app in rows:
        job = con.execute("SELECT * FROM jobs WHERE id = ?", (app["job_id"],)).fetchone()
        print(f"- {job['title']} @ {job['company']} ({job['apply_system']})")
        prepare_one(con, client, job, app)


if __name__ == "__main__":
    import sys

    # "submit <job id>" is what the dashboard's Approve button runs: your click is the approval.
    if len(sys.argv) == 3 and sys.argv[1] == "submit" and sys.argv[2].isdigit():
        print(approve_and_submit(db.connect(), claude(), int(sys.argv[2])))
    elif len(sys.argv) == 4 and sys.argv[1] == "relink" and sys.argv[2].isdigit():
        print(relink(db.connect(), claude(), int(sys.argv[2]), sys.argv[3]))
    else:
        run()
