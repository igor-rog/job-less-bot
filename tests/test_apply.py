import pytest
from playwright.sync_api import sync_playwright

from pipeline import apply, db
from pipeline.resume import load_master
from pipeline.settings import ROOT
from pipeline.sources import Job

FIXTURES = ROOT / "tests" / "fixtures"
GH_URL = (FIXTURES / "greenhouse_form.html").as_uri()
LEVER_URL = (FIXTURES / "lever_form.html").as_uri()
BANK = {  # what the answer bank would return
    "Authorization to work in Canada": ("Yes", "approved"),
    "Years of soldering experience": ("3", "draft"),
    "Are you legally authorized to work in Canada?": ("Yes", "approved"),
    "What are your salary expectations?": ("$60,000 to $75,000", "approved"),
    "How did you hear about us?": ("Job board", "approved"),
}
GH_QUESTIONS = [
    {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
    {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"}]},
    {"label": "Authorization to work in Canada", "required": True,
     "fields": [{"name": "question_1", "type": "multi_value_single_select", "values": [{"label": "Yes"}, {"label": "No"}]}]},
    {"label": "Years of soldering experience", "required": False, "fields": [{"name": "question_2", "type": "input_text"}]},
    {"label": "Location (City)", "required": True, "fields": [{"name": "question_3", "type": "input_text"}]},
    {"label": "Gender", "required": False, "fields": [{"name": "question_4", "type": "input_text"}]},
]


@pytest.fixture
def bank(monkeypatch):
    asked = []

    def answer_for(con, client, question, job=None, options=None):
        asked.append(question)
        answer, status = BANK.get(question, ("", "needs_input"))
        return {"answer": answer, "status": status, "answer_id": 1}

    monkeypatch.setattr(apply.answers, "answer_for", answer_for)
    monkeypatch.setattr(apply, "load_master", lambda: load_master(ROOT / "samples" / "master_resume.example.yaml"))
    return asked


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, "ROOT", tmp_path)
    con = db.connect(tmp_path / "t.db")
    (tmp_path / "r.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "c.pdf").write_bytes(b"%PDF-1.4 letter")
    (tmp_path / "c.txt").write_text("Dear Hiring Manager,\n\nLetter body.", encoding="utf-8")

    def make(system, url="https://job-boards.greenhouse.io/acme/jobs/1"):
        db.save_new_jobs(con, [Job("Technician", f"Acme {system}", "Toronto, ON", url, "", "jsearch", system)])
        job = con.execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
        with con:
            con.execute("INSERT INTO applications (job_id, resume_path, letter, letter_path, letter_text_path) "
                        "VALUES (?, ?, '{}', ?, ?)", (job["id"], str(tmp_path / "r.pdf"), str(tmp_path / "c.pdf"),
                                                     str(tmp_path / "c.txt")))
        return job, con.execute("SELECT * FROM applications WHERE job_id = ?", (job["id"],)).fetchone()

    return con, make


def run_on_page(fn, *args):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        form = apply.Form(page, 1)
        fn(form, *args)
        values = page.evaluate("() => Object.fromEntries([...document.querySelectorAll('input:not([type=file]),textarea,select')]"
                               ".filter(e => e.type !== 'radio' || e.checked).map(e => [e.id || e.name, e.value]))")
        browser.close()
    return form, values


def test_greenhouse_form_is_filled_from_profile_and_bank(setup, bank):
    con, make = setup
    job, app = make("greenhouse")
    me = apply.personal(load_master(ROOT / "samples" / "master_resume.example.yaml"))
    form, v = run_on_page(apply.fill_greenhouse, con, None, job, app, me, GH_URL, GH_QUESTIONS)
    assert (v["first_name"], v["last_name"], v["email"]) == ("Alex", "Example", "alex@example.com")
    assert v["question_1"] == "Yes"            # searchable dropdown picked
    assert v["question_2"] == "3"
    assert v["question_3"] == "Toronto, ON"    # location filled from your profile, not the AI
    assert v["question_4"] == "" and "Gender" not in bank  # demographic: never answered
    assert form.missing == []


def test_lever_form_is_filled_and_survey_left_alone(setup, bank):
    con, make = setup
    job, app = make("lever", "https://jobs.lever.co/acme/123")
    me = apply.personal(load_master(ROOT / "samples" / "master_resume.example.yaml"))
    form, v = run_on_page(apply.fill_lever, con, None, job, app, me, LEVER_URL, "Dear Hiring Manager,\n\nLetter.")
    assert (v["name"], v["org"], v["urls[GitHub]"]) == ("Alex Example", "Acme Sensors", "https://github.com/alex-example")
    assert v["cards[abc][field0]"] == "Yes" and v["cards[abc][field2]"] == "Job board"
    assert v["comments"].startswith("Dear Hiring Manager")
    assert "surveysResponses[s1][responses][field0]" not in v  # gender survey not ticked
    assert not any("gender" in q.lower() for q in bank)


def test_prepare_fills_screenshots_and_stops_before_submitting(setup, bank):
    con, make = setup
    job, app = make("greenhouse")
    assert apply.prepare_one(con, None, job, app, form_url=GH_URL, questions=GH_QUESTIONS) == "filled"
    row = con.execute("SELECT * FROM applications WHERE job_id = ?", (job["id"],)).fetchone()
    assert row["submitted_at"] is None and "Years of soldering" in row["apply_note"]  # draft flagged for review
    folder = apply._folder(job["id"])
    assert (folder / "form.png").exists() and not (folder / "submitted.png").exists()
    assert "clicked submit" not in (folder / "apply.log").read_text()


def test_unanswered_required_question_blocks_until_you_answer(setup, monkeypatch, bank):
    con, make = setup
    job, app = make("greenhouse")
    monkeypatch.setitem(BANK, "Authorization to work in Canada", ("", "needs_input"))
    assert apply.prepare_one(con, None, job, app, form_url=GH_URL, questions=GH_QUESTIONS) == "needs_answers"
    note = con.execute("SELECT apply_note FROM applications WHERE job_id = ?", (job["id"],)).fetchone()[0]
    assert "Authorization to work in Canada" in note


def test_other_systems_become_apply_manually_with_the_link(setup):
    con, make = setup
    job, app = make("workday", "https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/123")
    assert apply.prepare_one(con, None, job, app) == "manual"
    note = con.execute("SELECT apply_note FROM applications WHERE job_id = ?", (job["id"],)).fetchone()[0]
    assert "myworkdayjobs.com" in note


def test_submit_needs_approval_of_a_filled_form(setup, bank):
    con, make = setup
    job, app = make("greenhouse")
    with pytest.raises(ValueError, match="Not ready"):
        apply.approve_and_submit(con, None, job["id"], form_url=GH_URL, questions=GH_QUESTIONS, headless=True)
    apply.prepare_one(con, None, job, app, form_url=GH_URL, questions=GH_QUESTIONS)
    assert apply.approve_and_submit(con, None, job["id"], form_url=GH_URL, questions=GH_QUESTIONS, headless=True) == "submitted"
    row = con.execute("SELECT * FROM applications WHERE job_id = ?", (job["id"],)).fetchone()
    assert row["approved_at"] and row["submitted_at"]
    assert "clicked submit (approved by you)" in (apply._folder(job["id"]) / "apply.log").read_text()


def test_greenhouse_ids_from_links():
    assert apply.greenhouse_ids("https://job-boards.greenhouse.io/rocketlab/jobs/7991545003") == ("rocketlab", "7991545003")
    assert apply.greenhouse_ids("https://boards.greenhouse.io/acme/jobs/12?gh_src=x") == ("acme", "12")
    assert apply.lever_apply_url("https://jobs.lever.co/acme/abc-123") == "https://jobs.lever.co/acme/abc-123/apply"


def test_filling_never_submits_the_form(setup, bank):
    """Guard against any filling step (e.g. a key press) submitting a real application."""
    con, make = setup
    job, app = make("greenhouse")
    me = apply.personal(load_master(ROOT / "samples" / "master_resume.example.yaml"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        apply.fill_greenhouse(apply.Form(page, 1), con, None, job, app, me, GH_URL, GH_QUESTIONS)
        assert "Thank you" not in page.inner_text("body")
        page.goto(LEVER_URL)
        apply.fill_lever(apply.Form(page, 1), con, None, job, app, me, LEVER_URL, "letter")
        assert "submitted" not in page.inner_text("body")
        browser.close()


def test_required_demographic_question_is_flagged_not_skipped_silently():
    form = apply.Form(None, 1)
    assert form.answer(None, None, None, "What is your gender?", [], required=True) == ""
    assert form.missing == ["What is your gender?"]


def test_upload_that_never_shows_counts_as_missing(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_bytes(b"%PDF")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<input id='resume' type='file'><input id='first_name' value=''>")
        form = apply.Form(page, 1)
        assert apply.upload(form, "#resume", str(f), "resume") is False
        browser.close()


def test_verify_reads_the_form_back(tmp_path):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<input id='first_name' value=''><input id='last_name' value='Lee'>"
                         "<input id='email' value='a@b.c'><input id='phone' value='1'>")
        form = apply.Form(page, 1)
        apply.verify_greenhouse(form)
        browser.close()
    assert form.missing == ["First Name (empty on the form)"]


def test_security_code_on_submit_is_handed_to_you_not_failed(tmp_path):
    page_html = ("<form onsubmit=\"event.preventDefault(); document.body.innerHTML = "
                 "'<p>Enter the security code we emailed you</p><input id=sc>';\">"
                 "<button type=submit>Submit application</button></form>")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(page_html)
        form = apply.Form(page, 1)
        apply_folder = tmp_path
        status, note = apply._submit(form, headed=False)
        browser.close()
    assert status == "manual" and "security check" in note


def test_relink_switches_a_copy_site_job_to_the_employer_form(tmp_path, monkeypatch):
    from pipeline import apply
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Designer", "Smile.io", "Remote", "https://ca.indeed.com/viewjob?jk=1", "", "gmail", "manual")])
    with con:
        con.execute("INSERT INTO applications (job_id, apply_status) VALUES (1, 'manual')")
    monkeypatch.setattr(apply, "prepare_one", lambda con, client, job, app: job["apply_system"])
    assert apply.relink(con, None, 1, "https://jobs.lever.co/smile/abc") == "Now applying through lever: lever"
    assert con.execute("SELECT apply_status FROM applications").fetchone()[0] is None


def test_thank_you_text_already_on_the_page_is_not_a_confirmation(tmp_path):
    page_html = ("<p>Thank you for your interest in Acme.</p>"
                 "<form onsubmit=\"event.preventDefault();\"><button type=submit>Submit application</button></form>")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(page_html)
        status, _ = apply._submit(apply.Form(page, 1), headed=False)
        browser.close()
    assert status == "failed"  # the click did nothing; the posting's own "thank you" doesn't count


def test_approved_jobs_are_never_prepared_again(setup, monkeypatch):
    con, make = setup
    approved, _ = make("greenhouse")
    retry, _ = make("lever")
    with con:
        con.execute("UPDATE applications SET apply_status = 'failed'")
        con.execute("UPDATE applications SET approved_at = datetime('now') WHERE job_id = ?", (approved["id"],))
    prepared = []
    monkeypatch.setattr(apply, "prepare_one", lambda con, client, job, app: prepared.append(job["id"]))
    monkeypatch.setattr(apply, "claude", lambda: None)
    monkeypatch.setattr(apply.db, "connect", lambda: con)
    apply.run()
    assert prepared == [retry["id"]]  # a plain failure is retried; a clicked Submit never is
