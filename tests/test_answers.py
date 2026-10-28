import pytest

from pipeline import answers, db, resume, tailor
from pipeline.settings import ROOT
from pipeline.sources import Job


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "t.db")
    answers.import_file(con, ROOT / "samples" / "answer_bank.example.yaml")
    return con


class Model:
    """Stands in for _ask: 'same' answers the same-question check, 'drafts' the drafting calls."""

    def __init__(self, monkeypatch, same=0, draft=None):
        self.calls = []
        self.same, self.draft = same, draft or {"answer": "A drafted answer.", "needs_input": False}
        monkeypatch.setattr(tailor, "_ask", self.ask)
        original = resume.load_master
        monkeypatch.setattr(resume, "load_master", lambda: original(ROOT / "samples" / "master_resume.example.yaml"))

    def ask(self, client, con, model, system, user, schema, max_tokens):
        self.calls.append(user)
        return {"same_as": self.same} if "same_as" in schema["properties"] else dict(self.draft)


def test_exact_and_reworded_punctuation_match_without_a_model(con):
    row = answers.find(con, "are you legally authorized to work in canada")
    assert row["answer"] == "Yes"


def test_paraphrase_is_confirmed_by_the_model_then_saved_as_alias(con, monkeypatch):
    model = Model(monkeypatch, same=1)
    q = "What is your desired salary?"
    assert "55,000" in answers.answer_for(con, "client", q)["answer"]
    assert len(model.calls) == 1
    answers.answer_for(con, "client", q)  # second time: alias, no model call
    assert len(model.calls) == 1


def test_look_alike_question_is_not_matched_when_model_says_different(con, monkeypatch):
    model = Model(monkeypatch, same=0, draft={"answer": "", "needs_input": True})
    result = answers.answer_for(con, "client", "Are you legally authorized to work in the United States?")
    assert result["status"] == "needs_input" and result["answer"] == ""
    assert len(model.calls) == 2  # same-question check, then a draft that correctly refused to guess


def test_without_a_model_only_near_exact_matches_count(con):
    assert answers.find(con, "What is your desired salary?", client=None) is None


def test_unknown_question_is_drafted_once_then_reused_as_draft(con, monkeypatch):
    model = Model(monkeypatch)
    q = "Describe your experience with oscilloscopes."
    first = answers.answer_for(con, "client", q)
    assert first["status"] == "draft"
    calls = len(model.calls)
    assert answers.answer_for(con, "client", q)["answer_id"] == first["answer_id"]
    assert len(model.calls) == calls  # reused, not re-drafted


def test_per_job_questions_are_drafted_fresh_for_each_job(con, monkeypatch):
    model = Model(monkeypatch)
    db.save_new_jobs(con, [Job(t, "Acme", "Toronto", f"u{t}", "", "jsearch", "manual") for t in ("A", "B")])
    jobs = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    a = answers.answer_for(con, "client", "Why do you want to work at Acme?", jobs[0])
    b = answers.answer_for(con, "client", "Why do you want to work at Acme?", jobs[1])
    assert a["answer_id"] != b["answer_id"] and a["status"] == b["status"] == "draft"
    again = answers.answer_for(con, "client", "Why do you want to work at Acme?", jobs[0])
    assert again["answer_id"] == a["answer_id"]  # same job: reuse its draft


def test_answers_fit_the_forms_options(con, monkeypatch):
    Model(monkeypatch, draft={"answer": "Maybe later", "needs_input": False})
    assert answers.answer_for(con, None, "Are you legally authorized to work in Canada?", options=["Yes", "No"])["answer"] == "Yes"
    off = answers.answer_for(con, "client", "Do you enjoy teamwork?", options=["Yes", "No"])
    assert off["status"] == "needs_input"  # the draft fit no option, so it's left for you


def test_approve_with_your_wording(con, monkeypatch):
    Model(monkeypatch)
    drafted = answers.answer_for(con, "client", "Describe your soldering experience.")
    answers.approve(con, drafted["answer_id"], "I solder through-hole and SMD parts.")
    row = answers.find(con, "Describe your soldering experience.")
    assert (row["status"], row["source"], row["answer"]) == ("approved", "you", "I solder through-hole and SMD parts.")


def test_empty_answers_import_as_needs_input(con):
    assert answers.find(con, "Why do you want to work here?") is None  # per-job entries aren't reusable
    with con:
        con.execute("INSERT INTO answers (question, answer, status, source) VALUES ('Notice period?', '', 'needs_input', 'you')")
    assert answers.answer_for(con, None, "Notice period?")["status"] == "needs_input"


def test_short_reworded_question_still_reaches_the_model_with_the_right_candidate(con, monkeypatch):
    """Regression: WRatio ranked unrelated questions above the real one, so the model never saw it."""
    model = Model(monkeypatch, same=0)
    answers.answer_for(con, "client", "Authorization to work in Canada")
    candidates = model.calls[0]
    assert "are you legally authorized to work in canada" in candidates


def test_revise_applies_your_change_and_keeps_it_a_draft(con, monkeypatch):
    model = Model(monkeypatch, draft={"answer": "Toronto, Ontario. Yes, I'm a Canadian citizen."})
    with con:
        aid = con.execute("INSERT INTO answers (question, answer, status, source) VALUES "
                          "('Where are you located and are you eligible to work in Canada?', '', 'needs_input', 'ai')").lastrowid
    new = answers.revise(con, None, aid, "", "I live in Toronto and I'm a Canadian citizen")
    row = con.execute("SELECT answer, status FROM answers WHERE id = ?", (aid,)).fetchone()
    assert (row["answer"], row["status"]) == (new, "draft")  # still yours to approve
    assert "I'm a Canadian citizen" in model.calls[0] or "Canadian citizen" in model.calls[0]
