from playwright.sync_api import sync_playwright

from pipeline import db, userscript
from pipeline.resume import load_master
from pipeline.settings import ROOT

PAGE = """<form onsubmit="event.preventDefault(); document.title = 'SUBMITTED'">
  <label for=fn>First Name *</label><input id=fn required>
  <input id=ln autocomplete=family-name placeholder="Surname">
  <label>Email address <input id=em type=email></label>
  <label for=co>Company</label><input id=co>
  <label for=rp>Reference phone number</label><input id=rp>
  <label for=ph>Phone</label><input id=ph>
  <label for=ci>City</label><input id=ci>
  <label for=pr>Province</label>
  <select id=pr><option value="">Select...</option><option value=ab>AB</option><option value=on>ON</option></select>
  <label for=au>Are you legally authorized to work in Canada?</label>
  <select id=au><option value="">Select...</option><option value=y>Yes</option><option value=n>No</option></select>
  <label for=us>Are you legally authorized to work in the United States?</label><input id=us>
  <fieldset><legend>Will you now or in the future require sponsorship?</legend>
    <label><input type=radio name=sp value=1> Yes</label><label><input type=radio name=sp value=0> No</label></fieldset>
  <label for=sal>Desired salary</label><input id=sal type=number>
  <label for=ge>Gender</label><input id=ge>
  <label for=why>Why do you want to work here?</label><textarea id=why required></textarea>
  <button type=submit>Submit</button></form>"""

FIELDS = ("#fn", "#ln", "#em", "#co", "#rp", "#ph", "#ci", "#pr", "#au", "#us", "#sal", "#ge", "#why")


def test_userscript_fills_the_right_fields_and_never_submits(tmp_path):
    con = db.connect(tmp_path / "t.db")
    with con:
        con.executemany("INSERT INTO answers (question, answer, status, source) VALUES (?, ?, 'approved', 'you')", [
            ("Are you legally authorized to work in Canada?", "Yes"),
            ("Will you now or in the future require sponsorship?", "No"),
            ("Desired salary", "$60,000 to $75,000")])
    master = load_master(ROOT / "samples" / "master_resume.example.yaml")
    master["contact"]["address"] = {"city": "Toronto", "province": "Ontario"}
    script = userscript.build(con, master)
    (tmp_path / "form.html").write_text(PAGE, encoding="utf-8")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.add_init_script(script)
        page.goto((tmp_path / "form.html").as_uri())
        page.click("#jlb-autofill")
        got = dict(zip(FIELDS, (page.input_value(s) for s in FIELDS)))
        no_checked = page.is_checked("input[name=sp][value='0']")
        label, title = page.inner_text("#jlb-autofill"), page.title()
        browser.close()
    me = userscript.personal(master)
    assert got == {"#fn": me["first"], "#ln": me["last"], "#em": me["email"], "#co": "", "#rp": "",
                   "#ph": me["phone"], "#ci": "Toronto", "#pr": "on", "#au": "y",
                   "#us": "",  # a Canada answer is never reused for a US question
                   "#sal": "60000", "#ge": "", "#why": ""}
    assert no_checked
    assert "Still needed: Why do you want" in label and title != "SUBMITTED"


def test_refresh_rewrites_only_when_answers_change(tmp_path, monkeypatch):
    monkeypatch.setattr(userscript, "OUT", tmp_path / "s.user.js")
    monkeypatch.setattr(userscript, "load_master", lambda: load_master(ROOT / "samples" / "master_resume.example.yaml"))
    con = db.connect(tmp_path / "t.db")
    assert userscript.refresh(con) and not userscript.refresh(con)
    with con:
        con.execute("INSERT INTO answers (question, answer, status, source) VALUES ('When can you start?', 'Now', 'approved', 'you')")
    assert userscript.refresh(con) and "When can you start?" in userscript.OUT.read_text(encoding="utf-8")
