from playwright.sync_api import sync_playwright

from pipeline import assist, db, workday
from pipeline.apply import personal
from pipeline.resume import load_master
from pipeline.settings import ROOT
from pipeline.sources import Job

PAGE = """<form onsubmit="event.preventDefault(); document.title = 'SUBMITTED'">
  <label for=fn>First Name</label><input id=fn>
  <label for=em>Email</label><input id=em>
  <label for=co>Company</label><input id=co>
  <label for=q>Are you legally authorized to work in Canada?</label><input id=q>
  <button type=submit>Submit</button></form>"""


def test_autofill_button_fills_the_page_only_when_clicked(tmp_path, monkeypatch):
    monkeypatch.setattr(workday, "ROOT", tmp_path)
    monkeypatch.setattr(workday.answers, "answer_for", lambda con, client, q, job=None, options=None:
                        {"answer": "Yes" if "authorized" in q else "", "status": "approved", "answer_id": 1})
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Technician", "Acme", "Toronto", "u", "", "jsearch", "manual")])
    job = con.execute("SELECT * FROM jobs").fetchone()
    me = personal(load_master(ROOT / "samples" / "master_resume.example.yaml"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        assist.install(context)
        page = context.new_page()
        (tmp_path / "form.html").write_text(PAGE, encoding="utf-8")
        page.goto((tmp_path / "form.html").as_uri())  # a real navigation, like a job site
        page.wait_for_selector("#jlb-autofill")
        session = workday.Session(page, con, None, job, {"resume_path": "r.pdf", "letter_path": None}, me)
        assert assist.fill_if_clicked(session) is False and page.input_value("#fn") == ""  # nothing until clicked
        page.click("#jlb-autofill")
        assert assist.fill_if_clicked(session) is True
        values = [page.input_value(s) for s in ("#fn", "#em", "#co", "#q")]
        label = page.inner_text("#jlb-autofill")
        title = page.title()
        browser.close()
    assert values == ["Alex", "alex@example.com", "", "Yes"]  # "Company" isn't a question; never guessed
    assert label.startswith("Autofill · Filled") and title != "SUBMITTED"  # the button never submits
