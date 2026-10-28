r"""Assisted Workday applications in a visible browser.

Run: python -m pipeline.workday <job id>

1. Opens the job, clicks Apply -> "Autofill with Resume" and uploads the tailored resume.
2. You sign in or create the account in the window (once per company; the browser profile in
   %LOCALAPPDATA%\job-less-bot keeps you signed in). The tool never types passwords or creates accounts.
3. Each page is filled by its visible labels: your details and address, the resume and cover letter,
   LinkedIn, and application questions from the answer bank. Then it clicks "Save and Continue".
4. It hands the page to you when something is left: a required field it couldn't answer, the
   voluntary / self-identify pages (never answered for you), or consent checkboxes.
5. It stops at Review. Submit is always your click.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from pipeline import answers, db
from pipeline.apply import DEMOGRAPHIC, mark_submitted, personal
from pipeline.resume import load_master
from pipeline.settings import DATA, ROOT, claude

# Your Workday sign-ins stay on this computer, outside the project: a browser profile is thousands of
# constantly changing files, which would slow file browsing and OneDrive syncing.
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "job-less-bot" / "browser_profile"
HANDS_OFF_PAGE = re.compile(r"voluntary|self[- ]identif|disclosure|diversity", re.I)
REVIEW_PAGE = re.compile(r"^\s*review\b", re.I)
EMPTY_CHOICE = re.compile(r"^\s*(select one|select|choose|--)?\s*$", re.I)

# Visible label -> profile field. First match wins; labels are matched case-insensitively.
TEXT_FIELDS = [
    (r"first name|given name", "first"), (r"last name|family name|surname", "last"),
    (r"address line 1|street", "street"), (r"^\s*city|town", "city"), (r"postal|zip", "postal_code"),
    (r"phone number|^\s*phone\s*\*?$", "phone"), (r"e-?mail", "email"), (r"linkedin", "linkedin"),
]
CHOICE_FIELDS = [
    (r"country phone code", "Canada (+1)"), (r"^\s*country", "country"),
    (r"province|state|region", "province"), (r"phone device type", "Mobile"),
]
# The answer bank is only for real questions: the Application Questions page, plus these few that
# Workday puts on My Information. Anything else unknown (work history, education...) is never guessed.
QUESTIONS_PAGE = re.compile(r"question", re.I)
STEP_NAMES = re.compile(r"^(autofill with resume|quick apply|my information|my experience|application questions|"
                        r"voluntary disclosures|self[- ]identify|review|.*questions)$", re.I)
BANK_ANYWHERE = re.compile(r"how did you hear|previously (worked|been employed)|worked (here|for)|referred", re.I)
# Fields that make up one Work Experience entry; a "Job Title" label starts the next entry.
EXPERIENCE_LABELS = re.compile(r"^(job title|company|location|i currently work here|from|to|role description|"
                               r"month|year)$", re.I)

# Reads every visible form control with its label, current value and options.
CONTROLS_JS = r"""() => {
  const labelOf = el => {
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
    const fs = el.closest('fieldset');
    if (fs && fs.querySelector('legend')) return fs.querySelector('legend').innerText;
    return el.getAttribute('aria-label') || '';
  };
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [];
  document.querySelectorAll('input, textarea, button[aria-haspopup="listbox"]').forEach((el, i) => {
    // Workday hides the real file input behind its drop zone, so for files check the zone instead.
    if (!visible(el) && !(el.type === 'file' && el.parentElement && visible(el.parentElement))) return;
    const aid = el.getAttribute('data-automation-id') || '';
    if (aid === 'utilityMenuButton') return;  // site header menus (language, settings), not the form
    el.setAttribute('data-jlb', i);
    // Workday dates are separate Month / Day / Year boxes
    const kind = aid.startsWith('dateSection') ? 'date' : el.tagName === 'BUTTON' ? 'choice' : (el.type || 'text');
    out.push({id: i, kind, name: el.name || '', label: labelOf(el).replace(/\s+/g, ' ').replace('*', '').trim(),
              value: el.tagName === 'BUTTON' ? el.innerText.trim() : el.value, checked: !!el.checked,
              multiple: !!el.multiple, automation_id: aid, seen: el.dataset.seen === '1',
              aria_label: el.getAttribute('aria-label') || '', placeholder: el.getAttribute('placeholder') || '',
              required: el.required || el.getAttribute('aria-required') === 'true' ||
                        /\brequired\b/i.test(el.getAttribute('aria-label') || '')});
  });
  return out;
}"""


class Session:
    def __init__(self, page, con, client, job, app, me, wait_for_user=input):
        self.page, self.con, self.client, self.job, self.app, self.me = page, con, client, job, app, me
        self.wait_for_user = wait_for_user
        self.folder = ROOT / "output" / "applications" / str(job["id"])
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log_lines: list[str] = []
        self.log_path = self.folder / "workday.log"
        self.shots = 0
        self.on_questions_page = False
        self.experience_dates_needed: list[str] = []

    def log(self, msg: str):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.log_lines.append(line)
        with open(self.log_path, "a", encoding="utf-8") as f:  # written as we go, so a crash can't lose it
            f.write(line + "\n")
        print("  " + msg)

    def screenshot(self, tag: str):
        self.shots += 1
        self.page.screenshot(path=str(self.folder / f"workday_{self.shots:02d}_{tag}.png"), full_page=True)

    def save_field_map(self):
        """Labels, types and Workday ids of every field on the page, so unrecognized fields can be fixed."""
        fields = self.page.evaluate(CONTROLS_JS)
        (self.folder / f"workday_{self.shots:02d}_fields.json").write_text(json.dumps(fields, indent=1), encoding="utf-8")

    def decline_cookies(self):
        """Declines non-essential cookies when a banner is showing."""
        button = self.page.locator("[data-automation-id='legalNoticeDeclineButton'], button:has-text('Decline')")
        if button.count() and button.first.is_visible() and "cookie" in self.page.inner_text("body").lower():
            button.first.click()
            self.log("declined non-essential cookies")

    def heading(self) -> str:
        """The current step's name. Workday shows the job title in a heading above every step, so take
        the heading that names a step, not simply the first one."""
        texts = [t.strip() for t in self.page.locator("h1:visible, h2:visible, h3:visible").all_inner_texts() if t.strip()]
        return next((t for t in texts if STEP_NAMES.match(t)), texts[-1] if texts else "")

    def recover_from_error_page(self) -> bool:
        """Workday's own 'Something went wrong' page asks for a refresh; do that once."""
        if "something went wrong" not in self.page.inner_text("body").lower():
            return False
        self.log("Workday showed 'Something went wrong'; refreshing the page as it asks")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(4000)
        return True

    def profile_value(self, label: str, table) -> str | None:
        for pattern, key in table:
            if re.search(pattern, label, re.I):
                return self.me.get(key, key)  # fixed values (e.g. "Mobile") are their own key
        return None

    def answer(self, label: str, options=None) -> str:
        if DEMOGRAPHIC.search(label):
            return ""
        res = answers.answer_for(self.con, self.client, label, self.job, options or None)
        self.log(f"{res['status']}: {label[:70]} -> {res['answer'][:60]!r}")
        return res["answer"]

    def pick_choice(self, ctl, value: str) -> bool:
        button = self.page.locator(f"[data-jlb='{ctl['id']}']")
        button.click()
        texts = [t.strip() for t in self.page.get_by_role("option").all_inner_texts()]
        match = next((t for t in texts if t.lower() == value.lower()), None) or \
            next((t for t in texts if t.lower().startswith(value.lower())), None)
        if not match:
            self.page.keyboard.press("Escape")  # closes the list; never submits
            return False
        self.page.get_by_role("option", name=match, exact=True).first.click()
        return True

    def bank_ok(self, label: str) -> bool:
        # A label ending in "?" is a real question on any page; "Company" or "Location" never is.
        return self.on_questions_page or bool(BANK_ANYWHERE.search(label)) or label.strip().endswith("?")

    def experience_jobs(self) -> list[dict]:
        """Master resume jobs, newest first, with the tailored bullets for this job where chosen."""
        master = load_master()
        tailored = {}
        if "tailoring" in self.app.keys() and self.app["tailoring"]:
            tailored = {b["id"]: b["text"] for b in json.loads(self.app["tailoring"])["bullets"]}

        def started(j):
            try:
                return datetime.strptime(j["start"], "%B %Y")
            except ValueError:
                return datetime.min
        jobs = []
        for j in sorted(master["jobs"], key=started, reverse=True):
            chosen = [tailored[b["id"]] for b in j["bullets"] if b["id"] in tailored]
            jobs.append({**j, "description": "\n".join(chosen or [b["text"] for b in j["bullets"]])})
        return jobs

    def fill_experience(self, controls) -> set[int]:
        """Overwrites each Work Experience entry with the master resume's jobs, newest first: Workday's
        resume autofill often puts values in the wrong fields. Returns the control ids it handled."""
        blocks, current = [], None
        for c in controls:
            label = c["label"] or c["aria_label"]
            if re.fullmatch(r"job title", label, re.I):
                current = []
                blocks.append(current)
            if current is not None:
                if EXPERIENCE_LABELS.match(label):
                    current.append(c)
                elif label:
                    current = None  # left the work experience section
        if not blocks:
            return set()
        jobs, handled = self.experience_jobs(), set()
        for block, job in zip(blocks, jobs):
            for c in block:
                handled.add(c["id"])
                label, loc = (c["label"] or c["aria_label"]).lower(), self.page.locator(f"[data-jlb='{c['id']}']")
                value = {"job title": job["title"], "company": job["company"],
                         "role description": job["description"]}.get(label)
                if value is not None and c["kind"] in ("text", "textarea"):
                    loc.fill(value)
                elif label == "i currently work here" and c["kind"] == "checkbox":
                    loc.check() if job["end"] == "Present" else loc.uncheck()
                elif c["kind"] == "date" and label in ("from", "to"):
                    when = job["start"] if label == "from" else job["end"]
                    if when != "Present":
                        d = datetime.strptime(when, "%B %Y")
                        loc.fill(f"{d.month:02d}" if "Month" in c["automation_id"] else str(d.year))
            self.log(f"work experience: {job['title']} @ {job['company']} ({job['start']} - {job['end']})")
        self.experience_dates_needed = [f"{j['title']} @ {j['company']}: {j['start']} - {j['end']}" for j in jobs[:len(blocks)]]
        if len(blocks) < len(jobs):
            self.log(f"{len(jobs) - len(blocks)} more job(s) in your resume; add them with 'Add Another' if you want")
        return handled

    def search_pick(self, box, text: str) -> str | None:
        """Workday search boxes (skills, languages): type, then click the option that matches."""
        box.click()
        box.fill(text)
        self.page.wait_for_timeout(1500)
        if not self.page.get_by_role("option").count():
            box.press("Enter")  # Workday search boxes search on Enter; this doesn't submit anything
            self.page.wait_for_timeout(1500)
        texts = [t.strip() for t in self.page.get_by_role("option").all_inner_texts()]
        match = next((t for t in texts if t.lower() == text.lower()), None) or \
            next((t for t in texts if t.lower().startswith(text.lower())), None)
        if match:
            self.page.get_by_role("option", name=match, exact=True).first.click()
        else:
            box.fill("")
            self.page.keyboard.press("Escape")
        return match

    def add_button(self, section: str):
        """The 'Add' button that follows a section heading (Languages, Websites)."""
        return self.page.locator(f"xpath=//*[normalize-space(text())='{section}']/following::button"
                                 "[normalize-space(.)='Add' or normalize-space(.)='Add Another'][1]")

    def click_add(self, section: str) -> list[dict]:
        """Clicks a section's Add button and returns only the fields it created. Existing fields are
        marked first: positions shift when Add inserts fields mid-page, so they can't identify them."""
        self.page.evaluate("() => document.querySelectorAll('input, textarea, button').forEach(e => e.dataset.seen = '1')")
        self.add_button(section).first.click()
        self.page.wait_for_timeout(1500)
        return [c for c in self.page.evaluate(CONTROLS_JS) if not c["seen"]]

    def fill_fields_of_study(self):
        """Field of Study for each education entry: the program from your resume for that school, searched
        in Workday's list, shortened word by word until an option matches (e.g. 'Electrical Engineering')."""
        schools = self.page.get_by_label(re.compile(r"^school", re.I))
        fields = self.page.get_by_label(re.compile(r"^field of study", re.I))
        education = load_master()["education"]
        for i in range(min(schools.count(), fields.count())):
            school, box = schools.nth(i).input_value().lower(), fields.nth(i)
            if box.input_value().strip() or not school:
                continue
            edu = next((e for e in education if e["school"].lower() in school or school in e["school"].lower()), None)
            if not edu:
                continue
            words = edu["credential"].split(" - ")[0].split()
            picked = next((p for k in range(len(words), 0, -1) if (p := self.search_pick(box, " ".join(words[:k])))), None)
            self.log(f"field of study ({edu['school']}): {picked}" if picked else
                     f"field of study ({edu['school']}): no match in Workday's list")

    def fill_extras(self):
        """My Experience extras: skills from the tailored resume, languages, and your GitHub."""
        page, body = self.page, self.page.inner_text("body")
        skills_box = page.get_by_label(re.compile(r"add skills", re.I))
        if skills_box.count():
            skills = json.loads(self.app["tailoring"])["skills"] if "tailoring" in self.app.keys() and \
                self.app["tailoring"] else []
            added = [s for s in skills[:15] if s.lower() not in body.lower() and self.search_pick(skills_box.first, s)]
            self.log(f"skills added: {added}" if added else "no skills matched Workday's list")
        languages = [re.match(r"(.+?)\s*\((.+)\)", s).groups() for s in load_master()["skills"].get("Languages", [])
                     if "(" in s]
        for name, level in languages:
            if name.lower() in body.lower() or not self.add_button("Languages").count():
                continue
            prefer = ["native", "fluent", "expert", "advanced"] if "native" in level.lower() else \
                ["fluent", "advanced", "professional", "expert"]
            for c in self.click_add("Languages"):
                label, loc = (c["label"] or c["aria_label"]).lower(), page.locator(f"[data-jlb='{c['id']}']")
                if c["kind"] in ("text", "search") and "language" in label:
                    self.search_pick(loc, name)
                elif c["kind"] == "checkbox" and "fluent" in label:
                    loc.check()
                elif c["kind"] == "choice" and EMPTY_CHOICE.match(c["value"]):
                    loc.click()
                    opts = [t.strip() for t in page.get_by_role("option").all_inner_texts()]
                    pick = next((o for p in prefer for o in opts if o.lower().startswith(p)), None)
                    if pick:
                        page.get_by_role("option", name=pick, exact=True).first.click()
                    else:
                        page.keyboard.press("Escape")
            self.log(f"language added: {name} ({level})")
        self.fill_fields_of_study()
        github = self.me.get("github")
        if github and github not in body and self.add_button("Websites").count():
            url = next((c for c in self.click_add("Websites") if c["kind"] in ("text", "url")), None)
            if url:
                page.locator(f"[data-jlb='{url['id']}']").fill(github)
                self.log(f"website added: {github}")

    def fill_page(self) -> list[str]:
        """Fills what it can on the current page. Returns required fields still empty."""
        page, me = self.page, self.me
        if re.search(r"experience", self.heading(), re.I):
            try:
                self.fill_extras()
            except Exception as e:  # extras are optional; never let them stop the page
                self.log(f"skills/languages/websites: {e!r}"[:300])
        controls = page.evaluate(CONTROLS_JS)
        handled = self.fill_experience(controls)
        controls = [c for c in controls if c["id"] not in handled]
        files = [c for c in controls if c["kind"] == "file"]
        if files and Path(self.app["resume_path"]).name not in page.inner_text("body"):
            paths = [self.app["resume_path"]]
            if files[0]["multiple"] and self.app["letter_path"]:
                paths.append(self.app["letter_path"])
            page.locator(f"[data-jlb='{files[0]['id']}']").set_input_files(paths)
            page.wait_for_timeout(3000)
            self.log("uploaded " + " and ".join(Path(p).name for p in paths))
        groups = {}
        for c in controls:
            if c["kind"] in ("radio", "checkbox"):
                groups.setdefault(c["name"] or c["label"], []).append(c)
                continue
            if c["kind"] in ("file", "hidden", "password", "submit", "button", "date"):
                continue  # dates only come from your resume (work experience), never from guesses
            label = c["label"]
            if not label or DEMOGRAPHIC.search(label):
                continue
            if c["kind"] == "choice":
                if not EMPTY_CHOICE.match(c["value"]):
                    continue
                value = self.profile_value(label, CHOICE_FIELDS)
                if value is None and not self.bank_ok(label):
                    continue  # not a question: never guessed
                if value is None:
                    button = page.locator(f"[data-jlb='{c['id']}']")
                    button.click()
                    opts = [t.strip() for t in page.get_by_role("option").all_inner_texts() if t.strip()]
                    page.keyboard.press("Escape")
                    value = self.answer(label, opts)
                if value and self.pick_choice(c, value):
                    self.log(f"chose {value!r} for {label[:60]}")
            elif not c["value"]:
                value = self.profile_value(label, TEXT_FIELDS)
                if value is None and not self.bank_ok(label):
                    continue  # not a question: never guessed
                if value is None:
                    value = self.answer(label)
                if value:
                    page.locator(f"[data-jlb='{c['id']}']").fill(value)
                    self.log(f"filled {label[:60]}")
        for name, group in groups.items():
            if group[0]["kind"] == "checkbox" or any(g["checked"] for g in group):
                continue  # checkboxes are consents/agreements: always yours to tick
            question = self._question_for(group[0])
            if not question or DEMOGRAPHIC.search(question) or not self.bank_ok(question):
                continue
            value = self.answer(question, [g["label"] for g in group])
            pick = next((g for g in group if g["label"].lower() == value.lower()), None) if value else None
            if pick:
                page.locator(f"[data-jlb='{pick['id']}']").check()
        return self.missing()

    def _question_for(self, radio) -> str:
        """A radio's own label is the option ('Yes'); the question is the group's legend/heading."""
        return self.page.locator(f"[data-jlb='{radio['id']}']").evaluate(
            "el => { const fs = el.closest('fieldset, [role=radiogroup]'); if (!fs) return '';"
            " const l = fs.querySelector('legend, label[id], [id$=label]'); return l ? l.innerText.replace('*','').trim() :"
            " (fs.getAttribute('aria-label') || ''); }")

    def missing(self) -> list[str]:
        """Required fields still blank, read back from the page."""
        controls = self.page.evaluate(CONTROLS_JS)
        answered_groups = {c["name"] for c in controls if c["kind"] == "radio" and c["checked"]}
        empty = []
        for c in controls:
            if not c["required"] or c["kind"] == "file":
                continue
            if c["kind"] == "radio":
                if c["name"] not in answered_groups:
                    empty.append(self._question_for(c) or c["name"])
            elif c["kind"] == "checkbox":
                if not c["checked"]:  # consents and agreements: always yours to tick
                    empty.append(f"{c['label'] or 'checkbox'} (tick it yourself)")
            elif c["kind"] == "choice":
                if EMPTY_CHOICE.match(c["value"]):
                    empty.append(c["label"])
            elif not c["value"]:
                empty.append(c["label"] or c["name"])
        return list(dict.fromkeys(empty))

    def next_page(self):
        # "Continue" also matches "Save and Continue"; Submit is never matched.
        button = self.page.locator("[data-automation-id='bottom-navigation-next-button'], "
                                   "button:has-text('Continue'), button:has-text('Next')").first
        before = self.heading()
        try:
            button.click(timeout=10_000)
        except Exception:
            self.hand_over("Couldn't find the Continue button on this page.")
            return
        for _ in range(30):
            self.page.wait_for_timeout(500)
            if self.heading() != before:
                return
        self.hand_over("The page didn't move on after Continue (Workday may have rejected a field; it's shown in red).")

    def hand_over(self, why: str):
        self.screenshot("handed_to_you")
        self.log(why)
        prompt = "  -> Finish this page in the browser (the Autofill button helps), click Continue there, then press Enter here: "
        if self.wait_for_user is input:
            from pipeline.assist import wait_with_button
            wait_with_button(self, prompt)
        else:
            self.wait_for_user(prompt)

    def run_pages(self, max_pages: int = 12) -> bool:
        """Walks the application wizard. Returns True when it reaches Review."""
        for _ in range(max_pages):
            try:  # let the step finish loading before reading it (Workday never fully idles, hence the cap)
                self.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            self.recover_from_error_page()
            title = self.heading()
            self.log(f"page: {title}")
            if REVIEW_PAGE.search(title):
                self.screenshot("review")
                return True
            self.on_questions_page = bool(QUESTIONS_PAGE.search(title))
            self.experience_dates_needed = []
            try:
                self.decline_cookies()
                self.screenshot("before")
                self.save_field_map()
                if HANDS_OFF_PAGE.search(title):
                    self.hand_over("This page is voluntary/self-identification; it's yours to answer (or 'prefer not to say').")
                    continue
                left = self.fill_page()
                self.screenshot("filled")
                if left:
                    dates = ("\n     Dates from your resume: " + " | ".join(self.experience_dates_needed)
                             if self.experience_dates_needed else "")
                    self.hand_over("Still needed on this page: " + " | ".join(left) + dates)
                    continue
                self.next_page()
            except Exception as e:  # never crash out of a half-filled application: hand it to you
                self.log(f"error on this page: {e!r}"[:400])
                self.hand_over("Something went wrong on this page; please finish it yourself.")
        return False


def start(page, job, wait_for_user=input, log=print):
    """Job page -> Apply -> Autofill with Resume, then wait while you sign in."""
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)
    apply_btn = page.locator("[data-automation-id='adventureButton'], a:has-text('Apply'), button:has-text('Apply')").first
    if apply_btn.count():
        apply_btn.click()
        page.wait_for_timeout(2000)
    autofill = page.locator("[data-automation-id='autofillWithResume'], a:has-text('Autofill with Resume'), "
                            "button:has-text('Autofill with Resume')").first
    if autofill.count():
        autofill.click()
        page.wait_for_timeout(2000)
    log("Sign in or create your account in the browser window (the tool never types passwords).")
    wait_for_user("  -> When you see the application form (or a resume upload page), press Enter here: ")


def run(job_id: int, wait_for_user=input):
    from playwright.sync_api import sync_playwright

    con = db.connect()
    job = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    app = con.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if job is None or app is None or not app["resume_path"]:
        raise SystemExit(f"Job {job_id} has no prepared application yet (run the pipeline first).")
    print(f"{job['title']} @ {job['company']}\n  {job['url']}")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False,
                                                       viewport={"width": 1280, "height": 900})
        from pipeline.assist import install
        install(browser)  # the floating Autofill button, for pages handed to you
        page = browser.pages[0] if browser.pages else browser.new_page()
        session = Session(page, con, claude(), job, app, personal(load_master()), wait_for_user)
        session.log(f"start: {job['title']} @ {job['company']}")
        try:
            start(page, job, wait_for_user, session.log)
            if session.run_pages():
                session.log("Reached Review. Check everything, then click Submit in the browser yourself.")
            else:
                session.log("Stopped before Review; finish the application in the browser.")
        except Exception as e:  # keep the browser open so nothing you've entered is lost
            session.log(f"stopped by an error: {e!r}"[:400])
            print("  The tool hit an error, but the browser stays open: finish the application there.")
        answer = wait_for_user("  -> Did you submit the application? (y/n) ").strip().lower()
        if answer.startswith("y"):
            mark_submitted(con, job_id)
            session.log("marked as submitted")
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("Usage: python -m pipeline.workday <job id>")
    run(int(sys.argv[1]))
