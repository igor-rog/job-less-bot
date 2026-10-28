import json
from types import SimpleNamespace

import pytest

from pipeline import cover_letter as cl
from pipeline import db
from pipeline.resume import load_master
from pipeline.settings import ROOT
from pipeline.sources import Job

MASTER = load_master(ROOT / "samples" / "master_resume.example.yaml")
POSTING = "Title: Test Technician\nCompany: Acme\n\nAcme builds flight hardware for small satellites. Test boards daily."
PLAIN = ["I'm applying for the Test Technician role at Acme. Building flight hardware for small satellites is "
         "the kind of work I want to do.",
         "At Acme Sensors I tested 40 sensor boards a day against written specifications. I also found and "
         "repaired solder faults using an oscilloscope.",
         "I'd be glad to talk about the role. Thank you for your time."]


def letter(paragraphs=PLAIN, detail="flight hardware for small satellites", ids=("acme_test",)):
    return {"greeting": "Dear Hiring Manager,", "paragraphs": list(paragraphs), "sign_off": "Sincerely,",
            "company_detail": detail, "accomplishment_ids": list(ids)}


def test_plain_letter_passes_both_checks():
    assert cl.style_problems(letter()) == []
    assert cl.honesty_problems(MASTER, letter(), POSTING, []) == ([], [])


@pytest.mark.parametrize("paragraphs,expected", [
    (["I am excited to apply for this role.", PLAIN[1], PLAIN[2]], "excited to apply"),
    (["I want to leverage my skills.", PLAIN[1], PLAIN[2]], "leverage"),
    ([PLAIN[0] + " Testing — my favourite — is what I do.", PLAIN[1], PLAIN[2]], "dashes"),
    (["I test boards, solder joints, and document results.", "I plan, build, and ship.", PLAIN[2]], "lists of three"),
    ([PLAIN[0], PLAIN[1]], "paragraphs"),
    ([PLAIN[0], "word " * 260, PLAIN[2]], "words"),
])
def test_ai_sounding_letters_are_flagged(paragraphs, expected):
    assert any(expected in p for p in cl.style_problems(letter(paragraphs))), cl.style_problems(letter(paragraphs))


def test_untraceable_claims_are_problems():
    problems, _ = cl.honesty_problems(MASTER, letter(ids=["made_up"]), POSTING, [])
    assert any("not in the master resume" in p for p in problems)
    problems, _ = cl.honesty_problems(MASTER, letter(detail="a $2 billion IPO last year"), POSTING, [])
    assert any("company detail" in p for p in problems)
    inflated = [PLAIN[0], PLAIN[1].replace("40", "400"), PLAIN[2]]
    problems, _ = cl.honesty_problems(MASTER, letter(inflated), POSTING, [])
    assert any("400" in p for p in problems)


def test_posting_terms_not_in_master_are_noted():
    _, notes = cl.honesty_problems(MASTER, letter(), POSTING, ["Test Technician", "small satellites", "oscilloscope"])
    assert len(notes) == 1 and "small satellites" in notes[0]


def test_letter_text_for_paste_in_boxes():
    text = cl.letter_text(letter(), "Alex Example")
    assert text.startswith("Dear Hiring Manager,\n\nI'm applying") and text.endswith("Sincerely,\nAlex Example\n")


def test_style_guide_is_built_once_and_reused_until_samples_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "SAMPLES", tmp_path / "samples")
    monkeypatch.setattr(cl, "STYLE_GUIDE", tmp_path / "style_guide.md")
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "a.txt").write_text("Dear Sam, thanks for the note.", encoding="utf-8")
    calls = []
    monkeypatch.setattr(cl, "_ask", lambda *a, **k: calls.append(1) or {"style_guide": f"- guide {len(calls)}"})
    assert cl.style_guide(None, None) == "- guide 1"
    assert cl.style_guide(None, None) == "- guide 1" and len(calls) == 1  # cached
    (tmp_path / "samples" / "b.txt").write_text("Hi again.", encoding="utf-8")
    assert cl.style_guide(None, None) == "- guide 2"  # samples changed: rebuilt


def test_write_letter_retries_then_renders_pdf_and_text(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "ROOT", tmp_path)
    con = db.connect(tmp_path / "t.db")
    db.save_new_jobs(con, [Job("Test Technician", "Acme", "Toronto, ON", "u", POSTING, "jsearch", "manual")])
    job = con.execute("SELECT * FROM jobs").fetchone()
    with con:
        con.execute("INSERT INTO applications (job_id, tailoring, keywords, resume_path) VALUES (?, ?, ?, 'r.pdf')",
                    (job["id"], json.dumps({"bullets": [{"id": "acme_test", "text": "Tested 40 boards."}]}),
                     json.dumps(["Test Technician"])))
    app = con.execute("SELECT * FROM applications").fetchone()
    answers = [letter(["I am excited to apply.", PLAIN[1], PLAIN[2]]), letter()]
    prompts = []
    monkeypatch.setattr(cl, "_ask", lambda client, con, model, system, user, *a: prompts.append(user) or answers.pop(0))

    assert cl.write_letter(con, None, MASTER, [], job, app) == "ok"
    assert "rejected by the automated check" in prompts[1] and "excited to apply" in prompts[1]
    row = con.execute("SELECT * FROM applications").fetchone()
    assert row["letter_status"] == "needs_edit" and row["letter_flag"] is None
    folder = tmp_path / "output" / "applications" / str(job["id"])
    assert (folder / "Acme - Test Technician - Cover Letter.pdf").read_bytes().startswith(b"%PDF")
    assert "40 sensor boards" in (folder / "Acme - Test Technician - Cover Letter.txt").read_text(encoding="utf-8")


def test_apprenticeship_only_mentioned_for_apprentice_jobs():
    text = [PLAIN[0] + " I'm looking to start an electrical apprenticeship.", PLAIN[1], PLAIN[2]]
    problems, _ = cl.honesty_problems(MASTER, letter(text), POSTING, [], "Test Technician")
    assert any("apprenticeship" in p for p in problems)
    assert cl.honesty_problems(MASTER, letter(text), POSTING, [], "Electrical Apprentice") == ([], [])


@pytest.mark.parametrize("sentence,is_list", [
    ("I tracked inventory, shipped units, and maintained accurate records.", True),
    ("At Northwind Sensors, I executed QA protocols for LiDAR and sensor devices.", False),
    ("In my current role at Acme Labs, I work in cleanroom environments and keep careful records.", False),
])
def test_list_detector_only_counts_real_three_item_lists(sentence, is_list):
    paragraphs = [sentence, sentence, PLAIN[2]]
    assert any("lists of three" in p for p in cl.style_problems(letter(paragraphs))) == is_list


def test_tidy_removes_repeated_sign_off():
    l = cl.tidy(letter([PLAIN[0], PLAIN[1], PLAIN[2] + "\n\nSincerely,"]))
    assert l["paragraphs"][-1] == PLAIN[2]
    assert cl.letter_text(l, "A").count("Sincerely,") == 1


def test_job_title_mention_is_not_noted():
    _, notes = cl.honesty_problems(MASTER, letter(), POSTING, ["Test Technician", "small satellites"])
    assert [n for n in notes if "Test Technician" in n] == []
