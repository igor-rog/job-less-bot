from types import SimpleNamespace

from pipeline import db, score, tailor
from pipeline.sources import Job, manual

FIELDS = {"title": "Field Technician", "company": "Acme", "location": "Toronto, ON"}


def fake_ask(monkeypatch, fields=FIELDS):
    monkeypatch.setattr(tailor, "_ask", lambda *a, **k: dict(fields))


def test_indeed_and_linkedin_links_are_never_fetched(tmp_path, monkeypatch):
    monkeypatch.setattr(manual, "page_text", lambda url: (_ for _ in ()).throw(AssertionError("fetched")))
    con = db.connect(tmp_path / "t.db")
    for url in ["https://ca.indeed.com/viewjob?jk=1", "https://www.linkedin.com/jobs/view/123"]:
        assert manual.add(con, None, url).startswith("Not fetched")
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_pasted_description_is_saved_as_a_manual_job(tmp_path, monkeypatch):
    fake_ask(monkeypatch)
    con = db.connect(tmp_path / "t.db")
    msg = manual.add(con, None, "https://ca.indeed.com/viewjob?jk=1", "Field Technician at Acme. Duties...")
    assert msg.startswith("Added: Field Technician @ Acme")
    job = con.execute("SELECT * FROM jobs").fetchone()
    assert (job["source"], job["apply_system"], job["status"]) == ("manual", "manual", "new")
    assert job["description"].startswith("Field Technician at Acme")


def test_known_job_is_reactivated_instead_of_duplicated(tmp_path, monkeypatch):
    fake_ask(monkeypatch)
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Field Technician", "Acme", "Toronto, ON", "u", "", "jsearch", "manual")])
    with con:
        con.execute("UPDATE jobs SET status = 'low_fit', fit_score = 30")
    assert "Already in your list" in manual.add(con, None, "https://acme.example/job/1", "text")
    job = con.execute("SELECT * FROM jobs").fetchone()
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert (job["status"], job["source"]) == ("new", "manual")


def test_manual_jobs_skip_filters_and_are_always_prepared(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Senior Technician", "Acme", "Calgary, AB", "u", "", "manual", "manual")])
    job_id = con.execute("SELECT id FROM jobs").fetchone()[0]
    low = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"score": 20, "reason": "x"}')],
                          usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                                                cache_read_input_tokens=0))
    assert score.save_score(con, job_id, low) == 20
    assert con.execute("SELECT status FROM jobs").fetchone()[0] == "match"  # your pick, despite the low score
