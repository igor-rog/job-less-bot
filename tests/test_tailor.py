import json
from types import SimpleNamespace

import pytest

from pipeline import db, tailor
from pipeline.resume import build_html, load_master
from pipeline.settings import ROOT
from pipeline.sources import Job

MASTER = load_master(ROOT / "samples" / "master_resume.example.yaml")
ORIGINAL = "Tested 40 sensor boards a day against written specifications."


def result(text=ORIGINAL, bullet_id="acme_test", skills=("Python",), summary_id="sum_main"):
    return {"summary_id": summary_id,
            "summary": "Electronics technician with experience testing and repairing sensor hardware.",
            "bullets": [{"id": bullet_id, "text": text}], "skills": list(skills)}


def test_honest_light_rewording_passes():
    problems, notes = tailor.validate(MASTER, result("Tested 40 sensor boards a day against written specs."), [])
    assert problems == [] and notes == []


@pytest.mark.parametrize("bad,expected", [
    (result(bullet_id="made_up"), "unknown bullet id"),
    (result(skills=["Kubernetes"]), 'skill "Kubernetes"'),
    (result(summary_id="sum_x"), "unknown summary_id"),
    (result("Tested 400 sensor boards a day against written specifications."), "adds numbers"),
    (result("Led a team of engineers designing satellite firmware in Rust."), "too far"),
])
def test_dishonest_answers_are_blocked(bad, expected):
    problems, _ = tailor.validate(MASTER, bad, [])
    assert any(expected in p for p in problems), problems


def test_posting_phrase_the_master_never_uses_is_noted_not_blocked():
    reworded = "Tested 40 sensor boards a day against written specifications as part of quality assurance."
    problems, notes = tailor.validate(MASTER, result(reworded), ["quality assurance", "Python"])
    assert problems == []
    assert len(notes) == 1 and "quality assurance" in notes[0]  # Python is in the master: no note


def test_keyword_report_counts_terms_on_the_resume():
    matched, missing = tailor.keyword_report(["Python", "sensor", "Soldering", "PLC"], result())
    assert (matched, missing) == (2, ["Soldering", "PLC"])


def test_renderer_uses_reworded_text():
    html = build_html(MASTER, bullet_ids=["acme_test"], rewrites={"acme_test": "Tested 40 boards daily."},
                      summary_text="Custom summary.")
    assert "Tested 40 boards daily." in html and "Custom summary." in html and ORIGINAL not in html


class FakeClient:
    """Answers the keyword call, then the tailoring calls in order."""

    def __init__(self, tailor_answers):
        self.answers = list(tailor_answers)
        self.prompts = []
        create = self._create
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    def _create(self, **params):
        self.prompts.append(params["messages"][0]["content"])
        data = {"title": "Test Technician", "terms": ["Python", "PLC"]} if params["model"] == tailor.KEYWORD_MODEL \
            else self.answers.pop(0)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(stop_reason="end_turn", usage=usage,
                               content=[SimpleNamespace(type="text", text=json.dumps(data))])


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(tailor, "OUTPUT", tmp_path / "apps")
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Test Technician", "Acme", "Toronto, ON", "u", "Python, PLC", "jsearch", "manual")])
    return con, con.execute("SELECT * FROM jobs").fetchone()


def test_tailor_job_retries_once_then_renders(tmp_path, monkeypatch):
    con, job = _setup(tmp_path, monkeypatch)
    client = FakeClient([result(skills=["Kubernetes"]), result()])
    assert tailor.tailor_job(con, client, MASTER, [], job) == "tailored"
    assert "rejected by the automated check" in client.prompts[-1]
    app = con.execute("SELECT * FROM applications").fetchone()
    assert (app["keywords_matched"], app["keywords_total"]) == (1, 3)  # Python on the resume
    assert json.loads(app["missing_keywords"]) == ["Test Technician", "PLC"]
    assert (tmp_path / "apps" / str(job["id"]) / "Acme - Test Technician.pdf").read_bytes().startswith(b"%PDF")


def test_tailor_job_flags_after_two_failures_and_renders_nothing(tmp_path, monkeypatch):
    con, job = _setup(tmp_path, monkeypatch)
    client = FakeClient([result(skills=["Kubernetes"]), result(skills=["Kubernetes"])])
    assert tailor.tailor_job(con, client, MASTER, [], job) == "flagged"
    app = con.execute("SELECT * FROM applications").fetchone()
    assert app["resume_path"] is None and "Kubernetes" in app["flag"]
    assert con.execute("SELECT status FROM jobs").fetchone()[0] == "flagged"
    assert not (tmp_path / "apps").exists()


def test_render_to_fit_drops_least_relevant_bullets_until_it_fits(tmp_path):
    long_text = "Tested 40 sensor boards a day against written specifications. " * 3
    master = {**MASTER, "jobs": [{**MASTER["jobs"][0], "bullets": [
        {"id": f"b{i}", "text": long_text} for i in range(30)]}]}
    res = {**result(), "bullets": [{"id": f"b{i}", "text": long_text} for i in range(30)]}
    path = tailor.render_to_fit(master, res, tmp_path / "r.pdf")
    assert tailor.page_count(path) == 1
    assert 5 <= len(res["bullets"]) < 30 and res["bullets"][0]["id"] == "b0"  # kept the most relevant


def test_restricted_summary_only_for_matching_job_titles():
    restricted = {**MASTER["summaries"][0], "only_if_title_has": ["apprentice"]}
    master = {**MASTER, "summaries": [restricted]}
    problems, _ = tailor.validate(master, result(), [], "Electronics Technician")
    assert any("only for job titles" in p for p in problems)
    assert tailor.validate(master, result(), [], "1st Year Electrical Apprentice") == ([], [])


def test_render_to_fit_allows_two_pages_when_asked(tmp_path):
    long_text = "Tested 40 sensor boards a day against written specifications. " * 3
    master = {**MASTER, "jobs": [{**MASTER["jobs"][0], "bullets": [
        {"id": f"b{i}", "text": long_text} for i in range(30)]}]}
    one = {**result(), "bullets": [{"id": f"b{i}", "text": long_text} for i in range(30)]}
    two = {**result(), "bullets": list(one["bullets"])}
    tailor.render_to_fit(master, one, tmp_path / "one.pdf")
    path = tailor.render_to_fit(master, two, tmp_path / "two.pdf", pages=2)
    assert tailor.page_count(path) == 2 and len(two["bullets"]) > len(one["bullets"])


def test_every_model_call_stops_at_the_monthly_budget(tmp_path, monkeypatch):
    import pytest
    con = db.connect(tmp_path / "t.db")
    db.log_usage(con, "claude", 1000, cost_usd=tailor.CONFIG["limits"]["monthly_budget_usd"])
    with pytest.raises(SystemExit, match="budget"):
        tailor._ask(object(), con, tailor.TAILOR_MODEL, "s", "u", {}, 10)  # the client is never touched


def test_a_blocked_redo_keeps_the_earlier_resume_and_letter(tmp_path, monkeypatch):
    con, job = _setup(tmp_path, monkeypatch)
    assert tailor.tailor_job(con, FakeClient([result()]), MASTER, [], job) == "tailored"
    with con:
        con.execute("UPDATE applications SET letter = 'my letter'")
    good = con.execute("SELECT resume_path FROM applications").fetchone()[0]
    bad = [result(skills=["Kubernetes"])] * 2
    assert tailor.tailor_job(con, FakeClient(bad), MASTER, [], job) == "flagged"
    app = con.execute("SELECT * FROM applications").fetchone()
    assert (app["resume_path"], app["letter"]) == (good, "my letter") and "Kubernetes" in app["flag"]
