from playwright.sync_api import sync_playwright

from pipeline import db, workday
from pipeline.apply import personal
from pipeline.resume import load_master
from pipeline.settings import ROOT
from pipeline.sources import Job

FORM = (ROOT / "tests" / "fixtures" / "workday_form.html").as_uri()
BANK = {"How Did You Hear About Us?": "Job Board", "Have you previously worked for this company?": "No",
        "Are you legally authorized to work in Canada?": "Yes",
        "What are your salary expectations?": "$60,000 to $75,000 per year"}


def test_workday_wizard_is_filled_page_by_page_and_stops_at_review(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(workday.answers, "answer_for", lambda con, client, q, job=None, options=None:
                        asked.append(q) or {"answer": BANK.get(q, ""), "status": "approved" if q in BANK else "needs_input",
                                            "answer_id": 1})
    monkeypatch.setattr(workday, "ROOT", tmp_path)
    def sample_master():
        m = load_master(ROOT / "samples" / "master_resume.example.yaml")
        m["skills"]["Languages"] = ["English (professional)", "Russian (native)"]
        return m
    monkeypatch.setattr(workday, "load_master", sample_master)
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Technician", "Acme", "Toronto, ON", FORM, "", "watchlist", "workday")])
    job = con.execute("SELECT * FROM jobs").fetchone()
    (tmp_path / "Acme - Technician.pdf").write_bytes(b"%PDF")
    (tmp_path / "letter.pdf").write_bytes(b"%PDF")
    app = {"resume_path": str(tmp_path / "Acme - Technician.pdf"), "letter_path": str(tmp_path / "letter.pdf"),
           "tailoring": '{"bullets": [], "skills": ["Python", "Oscilloscopes", "Kubernetes"]}'}
    master = load_master(ROOT / "samples" / "master_resume.example.yaml")
    master["contact"]["address"] = {"street": "1 Main St", "city": "Toronto", "province": "Ontario",
                                    "postal_code": "M1M 1M1", "country": "Canada"}

    handovers = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FORM)

        filled = {}

        def you(prompt):  # stands in for you finishing a handed-over page in the browser
            title = session.heading()
            handovers.append(title)
            if title != "Voluntary Disclosures":
                raise AssertionError(f"handed over {title} unexpectedly")
            page.locator("section[data-step='4'] input[type=checkbox]").check()
            page.click("#next")
            return ""

        session = workday.Session(page, con, None, job, app, personal(master), wait_for_user=you)
        original_next = session.next_page

        def record():
            filled.update(page.evaluate("() => Object.fromEntries([...document.querySelectorAll('section:not([hidden]) "
                                        "input:not([type=radio]):not([type=file]), "
                                        "section:not([hidden]) textarea, section:not([hidden]) button')]"
                                        ".map(e => [e.id || e.getAttribute('aria-labelledby'), "
                                        "e.type === 'checkbox' ? e.checked : (e.value || e.innerText)]))"))
            filled["attached"] = page.locator("#attached").inner_text()
            if page.locator("#picked").count() and page.locator("section[data-step='2']").is_visible():
                filled["pills"] = page.locator(".pill").all_inner_texts()

        def record_then_next():
            record()
            original_next()
        session.next_page = record_then_next

        assert session.run_pages() is True
        assert session.heading() == "Review"
        assert page.locator("#submit").count() == 1  # Submit was never clicked
        browser.close()

    assert (filled["f1"], filled["f3"], filled["f5"], filled["f6"]) == ("Alex", "1 Main St", "M1M 1M1", "alex@example.com")
    assert (filled["l1"], filled["l2"], filled["l3"], filled["l4"]) == ("Canada", "Ontario", "Mobile", "Job Board")
    assert filled["q1"] == "Yes" and filled["q2"].startswith("$60,000")
    assert filled["li"] == "" or filled["li"].startswith("https://")  # sample profile has no LinkedIn
    assert "Acme - Technician.pdf" in filled["attached"] and "letter.pdf" in filled["attached"]
    assert handovers == ["Voluntary Disclosures"]  # only demographics and consent were left to you
    assert (filled["w1fm"], filled["w1fy"]) == ("06", "2022")  # dates from the resume
    assert filled["pills"].count("Python") == 1 and "Oscilloscopes" in filled["pills"]  # skills (Kubernetes not offered)
    assert "English" in filled["pills"] and "Russian" in filled["pills"]  # languages
    assert filled["url1"] == "https://github.com/alex-example"
    # "Electronics Engineering Technician" isn't in the list; the shortened "Electronics Engineering" matches
    assert "Electronics Engineering Technology" in filled["pills"]
    assert not any("gender" in q.lower() for q in asked)
    # Work experience comes from the master resume, overwriting Workday's wrong autofill...
    assert (filled["w1t"], filled["w1c"], filled["w1cur"]) == ("Test Technician", "Acme Sensors", True)
    assert "Tested 40 sensor boards" in filled["w1d"]
    # ...and is never sent to the answer bank as a "question"
    assert not any(q in ("Company", "Job Title", "Location", "Month", "Year") for q in asked)
