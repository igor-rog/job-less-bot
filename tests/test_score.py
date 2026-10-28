from types import SimpleNamespace

import pytest

from pipeline import db, score
from pipeline.resume import load_master
from pipeline.settings import ROOT
from pipeline.sources import Job


def row(title="Electrical Technician", location="Toronto, ON"):
    return {"title": title, "location": location}


@pytest.mark.parametrize("title,location,dropped", [
    ("Electrical Technician", "Toronto, ON", False),
    ("Senior Electrical Technician", "Toronto, ON", True),
    ("Sr. Test Technician", "Markham, ON", True),
    ("IT Support Administrator", "Toronto, ON", True),
    ("Team Leader Trainee", "Toronto, ON", False),  # "lead" must match the whole word, not "leader"
    ("Junior UX Designer", "Remote", False),
    ("Field Technician", "Calgary, AB", True),
    ("M&E Technician", "Aurora, CO", True),
    ("Lab Technician", "", False),  # unknown location: let the scorer read the description
])
def test_free_filters(title, location, dropped):
    assert (score.filter_reason(row(title, location)) is not None) == dropped


def test_profile_is_long_enough_to_cache_and_uses_only_master_resume():
    master = load_master(ROOT / "samples" / "master_resume.example.yaml")
    text = score.profile_text(master)
    assert "Tested 40 sensor boards" in text and "Preferences:" in text
    assert score.profile_text(master) == text  # deterministic, or the prompt cache never hits


def usage(inp=100, out=20, write=0, read=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=write, cache_read_input_tokens=read)


def test_cost_counts_cache_and_batch_discounts():
    full = score.cost_usd(usage(inp=1_000_000, out=0))
    assert full == pytest.approx(score.CONFIG["pricing"][score.MODEL]["input"])
    assert score.cost_usd(usage(inp=0, out=0, read=1_000_000)) == pytest.approx(full * 0.1)
    assert score.cost_usd(usage(inp=1_000_000, out=0), batch=True) == pytest.approx(full / 2)


def answer(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, usage=usage(), content=[SimpleNamespace(type="text", text=text)])


def test_save_score_sets_match_or_low_fit_and_logs_cost(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Test Technician A", "X", "Toronto", "u1", "", "jsearch", "manual"),
                           Job("Test Technician B", "Y", "Toronto", "u2", "", "jsearch", "manual"),
                           Job("Test Technician C", "Z", "Toronto", "u3", "", "jsearch", "manual")])
    ids = [r[0] for r in con.execute("SELECT id FROM jobs ORDER BY id")]
    min_score = score.CONFIG["scoring"]["min_fit_score"]
    assert score.save_score(con, ids[0], answer(f'{{"score": {min_score}, "reason": "good"}}')) == min_score
    assert score.save_score(con, ids[1], answer('{"score": 150, "reason": "x"}')) == 100  # clamped
    assert score.save_score(con, ids[2], answer("", stop="refusal")) is None
    statuses = [r[0] for r in con.execute("SELECT status FROM jobs ORDER BY id")]
    assert statuses == ["match", "match", "new"]  # unusable answer stays 'new' for a retry
    assert db.usage_this_month(con, "claude")[1] > 0


def test_batch_mode_saves_results_by_job_id_at_half_price(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("A", "X", "Toronto", "u1", "", "jsearch", "manual"),
                           Job("B", "Y", "Toronto", "u2", "", "jsearch", "manual")])
    jobs = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    ok = SimpleNamespace(type="succeeded", message=answer('{"score": 90, "reason": "fit"}'))
    results = [SimpleNamespace(custom_id=str(jobs[1]["id"]), result=ok),  # results arrive in any order
               SimpleNamespace(custom_id=str(jobs[0]["id"]), result=SimpleNamespace(type="expired"))]
    batches = SimpleNamespace(create=lambda requests: SimpleNamespace(id="b1"),
                              retrieve=lambda _id: SimpleNamespace(processing_status="ended"),
                              results=lambda _id: results)
    score._run_batch(con, SimpleNamespace(messages=SimpleNamespace(batches=batches)), "system", jobs)
    assert [r[0] for r in con.execute("SELECT status FROM jobs ORDER BY id")] == ["new", "match"]
    assert db.usage_this_month(con, "claude")[1] == pytest.approx(score.cost_usd(usage(), batch=True))


def test_tiers_are_capped_in_code(tmp_path):
    con = db.connect(tmp_path / "t.db")
    titles = ("Electronics Technician II", "Junior UX Designer", "Personal Banking Associate")
    db.save_new_jobs(con, [Job(t, "X", "Toronto", f"u{t}", "", "jsearch", "manual") for t in titles])
    a, b, c = [r[0] for r in con.execute("SELECT id FROM jobs ORDER BY id")]
    cfg = score.CONFIG["scoring"]
    assert score.save_score(con, a, answer('{"score": 88, "reason": "r"}')) == 88
    assert score.save_score(con, b, answer('{"score": 88, "reason": "r"}')) == 88 - cfg["tier_penalty"]["design"]
    assert score.save_score(con, c, answer('{"score": 88, "reason": "r"}')) == cfg["tier_caps"]["other"]


@pytest.mark.parametrize("title,tier", [
    ("Electronics Technician II", "main"), ("Field Service Technician - CNC Machines", "main"),
    ("Engineering Designer - Electrical", "main"), ("Junior UX UI Designer Figma", "design"),
    ("Digital Graphic Designer", "design"), ("Personal Banking Associate", "other"), ("Building UI kits", "design"),
])
def test_tier_from_title(title, tier):
    assert score.tier_for(title) == tier
