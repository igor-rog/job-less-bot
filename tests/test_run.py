import pytest

from pipeline import db, run
from pipeline.sources import Job, gmail


@pytest.fixture
def con(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    monkeypatch.setattr(run.db, "connect", lambda: con)
    return con


def test_a_failing_step_doesnt_stop_the_others(con, monkeypatch):
    calls = []
    monkeypatch.setattr(run.discover, "run", lambda sources: calls.append(("discover", sources)) or 1 / 0)
    for mod in (run.score, run.tailor, run.cover_letter, run.apply):
        monkeypatch.setattr(mod, "run", lambda m=mod.__name__: calls.append(m))
    assert run.main(["jsearch"]) == 1
    assert calls == [("discover", ["jsearch"]), "pipeline.score", "pipeline.tailor", "pipeline.cover_letter", "pipeline.apply"]
    assert con.execute("SELECT failed FROM runs").fetchone()[0] == "discover"


def test_each_run_records_its_new_jobs_and_cost(con, monkeypatch):
    def discover(sources):
        db.save_new_jobs(con, [Job("Technician", "Acme", "Toronto", "u", "", "jsearch", "manual")])
        db.log_usage(con, "claude", 1500, cost_usd=0.25)
    monkeypatch.setattr(run.discover, "run", discover)
    for mod in (run.score, run.tailor, run.cover_letter, run.apply):
        monkeypatch.setattr(mod, "run", lambda: None)
    db.log_usage(con, "claude", 99, cost_usd=5)  # earlier spend isn't this run's
    assert run.main() == 0
    r = con.execute("SELECT * FROM runs").fetchone()
    assert (r["new_jobs"], r["claude_tokens"], r["claude_cost_usd"], r["failed"]) == (1, 1500, 0.25, None)
    assert r["finished_at"]


def test_scheduled_run_never_waits_for_a_gmail_sign_in(con, tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "TOKEN", tmp_path / "missing.json")
    monkeypatch.setattr(gmail, "INTERACTIVE", True)
    seen = []
    monkeypatch.setattr(run.discover, "run", lambda sources: seen.append(gmail.INTERACTIVE))
    for mod in (run.score, run.tailor, run.cover_letter, run.apply):
        monkeypatch.setattr(mod, "run", lambda: None)
    run.main(scheduled=True)
    assert seen == [False]
    with pytest.raises(RuntimeError, match="sign-in expired"):  # instead of opening a browser and hanging
        gmail.service()


def test_a_run_whose_window_was_closed_is_marked_stopped(con, monkeypatch):
    with con:
        con.execute("INSERT INTO runs DEFAULT VALUES")  # never finished
    for mod in (run.discover, run.score, run.tailor, run.cover_letter, run.apply):
        monkeypatch.setattr(mod, "run", lambda *a: None)
    run.main()
    assert [r[0] for r in con.execute("SELECT failed FROM runs ORDER BY id")] == ["stopped before finishing", None]
