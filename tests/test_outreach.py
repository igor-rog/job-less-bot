import httpx
import pytest

from pipeline import db, outreach

BIG = {"business_id": "a1", "name": "Acme Electric Ltd", "phone_number": "+1 416-555-0100", "website": "https://acme.example",
       "full_address": "1 Main St, Toronto, ON", "rating": 4.6, "review_count": 120,
       "emails_and_contacts": {"emails": ["info@acme.example"]}}
SMALL = {**BIG, "business_id": "a2", "name": "Joe's Electrical", "review_count": 7}
SUPPLY = {**BIG, "business_id": "a3", "name": "Spark Electrical Supply", "review_count": 300}
NO_EMAIL = {**BIG, "business_id": "a4", "name": "Bolt Contracting", "emails_and_contacts": {"emails": []}}


def test_only_established_companies_are_kept_and_deduped(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUSINESS_API_KEY", "test")
    monkeypatch.setitem(outreach.CFG, "queries", ["q1", "q2"])
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": [BIG, SMALL, SUPPLY, NO_EMAIL]})))
    con = db.connect(tmp_path / "t.db")
    assert outreach.find(con, client) == 2  # second query returns the same businesses: nothing new
    rows = {r["name"]: r for r in con.execute("SELECT * FROM outreach")}
    assert set(rows) == {"Acme Electric Ltd", "Bolt Contracting"}  # small contractor and supply store left out
    assert (rows["Acme Electric Ltd"]["email"], rows["Acme Electric Ltd"]["status"]) == ("info@acme.example", "new")
    assert rows["Bolt Contracting"]["status"] == "no_email"  # kept with its phone number
    assert db.usage_this_month(con, "local_business")[0] == 2


def test_organizations_are_added_once(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert outreach.add_organizations(con) == 4
    assert outreach.add_organizations(con) == 0
    eata = con.execute("SELECT * FROM outreach WHERE name LIKE 'Electrical Apprenticeship%'").fetchone()
    assert (eata["kind"], eata["email"], eata["status"]) == ("organization", "eatainformation@electricalapprenticeship.ca", "new")
    assert con.execute("SELECT status FROM outreach WHERE name = 'IBEW Local 353'").fetchone()[0] == "no_email"


def test_searches_repeat_monthly_and_failed_ones_are_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUSINESS_API_KEY", "test")
    monkeypatch.setitem(outreach.CFG, "queries", ["ok", "limited"])
    calls = []

    def handler(req):
        calls.append(req.url.params["query"])
        return httpx.Response(429 if req.url.params["query"] == "limited" else 200, json={"data": [BIG]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    con = db.connect(tmp_path / "t.db")
    outreach.find(con, client)
    outreach.find(con, client)
    assert calls == ["ok", "limited", "limited"]  # "ok" isn't repeated within the month; the 429 is retried


# ---------- emails ----------
import base64
import json
from types import SimpleNamespace

from pipeline import cover_letter, tailor
from pipeline.resume import load_master
from pipeline.settings import ROOT

SAMPLE = ROOT / "samples" / "master_resume.example.yaml"
GOOD = {"subject": "309A apprenticeship inquiry", "body": "Hello Acme Electric team,\n\nI finished my diploma and "
        "tested 40 sensor boards a day at Acme Sensors. Do you take on 309A apprentices?\n\nMy resume is attached."}


def test_email_checks():
    known = "Tested 40 sensor boards a day"
    assert outreach.email_problems(GOOD, known) == []
    bad = {**GOOD, "body": GOOD["body"].replace("40", "400") + " I am passionate about wiring."}
    problems = outreach.email_problems(bad, known)
    assert any("400" in p for p in problems) and any("passionate" in p for p in problems)


@pytest.fixture
def listed(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.resume.load_master", lambda *a: load_master(SAMPLE))
    con = db.connect(tmp_path / "t.db")
    outreach.save(con, [outreach.parse(BIG), outreach.parse({**BIG, "business_id": "b", "name": "Volt Co",
                                                             "emails_and_contacts": {"emails": ["hi@volt.example"]}})],
                  "company")
    return con


def test_draft_writes_checked_emails(listed, monkeypatch):
    answers = [{**GOOD, "body": GOOD["body"] + " I am passionate."}, GOOD, GOOD]
    monkeypatch.setattr(tailor, "_ask", lambda *a, **k: dict(answers.pop(0)))
    monkeypatch.setattr(cover_letter, "style_guide", lambda client, con: "- plain and direct")
    assert outreach.draft(listed, None) == 2
    drafts = [json.loads(r[0]) for r in listed.execute("SELECT draft FROM outreach ORDER BY id")]
    assert all(d["problems"] == [] for d in drafts)  # the first draft was rejected and rewritten
    assert {r[0] for r in listed.execute("SELECT status FROM outreach")} == {"drafted"}


class FakeGmail:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.sent.append(base64.urlsafe_b64decode(body["raw"]).decode(errors="ignore"))
        return SimpleNamespace(execute=lambda: {"id": "m1", "threadId": f"t{len(self.sent)}"})


def test_review_sends_only_what_you_choose(listed, monkeypatch, tmp_path):
    with listed:
        listed.execute("UPDATE outreach SET status = 'drafted', draft = ?", (json.dumps({**GOOD, "problems": []}),))
    resume = tmp_path / "Alex Example - Resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(outreach, "apprentice_resume", lambda con, client: resume)
    choices = iter(["e", "s", "n"])
    edited = lambda email: {**email, "body": email["body"] + "\n\nThanks for reading."}
    gmail = FakeGmail()
    assert outreach.review(listed, ask=lambda _: next(choices), edit=edited, gmail_service=gmail, client=object()) == 1
    statuses = {r["name"]: (r["status"], r["thread_id"]) for r in listed.execute("SELECT * FROM outreach")}
    assert statuses["Acme Electric Ltd"] == ("sent", "t1") and statuses["Volt Co"][0] == "do_not_contact"
    mail = gmail.sent[0]
    assert "To: info@acme.example" in mail and "Thanks for reading." in mail  # your edit was sent
    assert "Alex Example" in mail and "alex@example.com" in mail              # signature added by code
    assert 'filename="Alex Example - Resume.pdf"' in mail


def test_daily_limit_stops_sending(listed, monkeypatch, tmp_path):
    with listed:
        listed.execute("UPDATE outreach SET status = 'drafted', draft = ?", (json.dumps({**GOOD, "problems": []}),))
    monkeypatch.setitem(outreach.CFG, "max_per_day", 1)
    (tmp_path / "r.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(outreach, "apprentice_resume", lambda con, client: tmp_path / "r.pdf")
    assert outreach.review(listed, ask=lambda _: "s", gmail_service=FakeGmail(), client=object()) == 1


def test_replies_and_bounces_are_detected(listed):
    with listed:
        listed.execute("UPDATE outreach SET status = 'sent', thread_id = 't-' || id")
    me = "alex@example.com"
    threads = {"t-1": ["Alex <alex@example.com>", "Acme <info@acme.example>"],
               "t-2": ["Alex <alex@example.com>", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"]}
    svc = SimpleNamespace(users=lambda: SimpleNamespace(threads=lambda: SimpleNamespace(
        get=lambda userId, id, format, metadataHeaders: SimpleNamespace(execute=lambda: {"messages": [
            {"payload": {"headers": [{"name": "From", "value": f}]}, "snippet": "Thanks, we&#39;re not hiring"}
            for f in threads[id]]}))))
    assert outreach.check_replies(listed, svc, me) == {"replied": 1, "bounced": 1}
    rows = listed.execute("SELECT status, reply FROM outreach ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("replied", "Thanks, we're not hiring"), ("bounced", "Thanks, we're not hiring")]
    assert outreach.check_replies(listed, svc, me) == {"replied": 0, "bounced": 0}  # not counted twice


def test_sign_off_is_never_doubled():
    assert outreach.strip_sign_off("Hello,\n\nBody text.\n\nBest regards,") == "Hello,\n\nBody text."
    assert outreach.strip_sign_off("Hello,\n\nThank you for your time.") == "Hello,\n\nThank you for your time."
    assert outreach.signature({"name": "A", "phone": "1", "email": "a@b"}).startswith("\n\nBest regards,\nA")


def test_a_search_timeout_keeps_going_and_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUSINESS_API_KEY", "test")
    monkeypatch.setitem(outreach.CFG, "queries", ["slow", "ok"])
    calls = []

    def handler(req):
        calls.append(req.url.params["query"])
        if req.url.params["query"] == "slow":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"data": [BIG]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    con = db.connect(tmp_path / "t.db")
    assert outreach.find(con, client) == 1  # the second area still ran
    outreach.find(con, client)
    assert calls == ["slow", "ok", "slow"]  # the timed-out area is retried, not skipped for a month


def test_apprentice_resume_survives_an_empty_model_answer(tmp_path, monkeypatch):
    from pipeline import resume, tailor
    from pipeline.settings import ROOT
    monkeypatch.setattr(outreach, "ROOT", tmp_path)
    sample = resume.load_master(ROOT / "samples" / "master_resume.example.yaml")
    monkeypatch.setattr(resume, "load_master", lambda: sample)
    answers = [None, {"bullets": []}]  # first call: no usable answer; second: a good one
    monkeypatch.setattr(tailor, "_ask", lambda *a: answers.pop(0))
    monkeypatch.setattr(tailor, "validate", lambda master, result, terms, title: ([], []))
    monkeypatch.setattr(tailor, "render_to_fit", lambda master, result, path: path)
    assert outreach.apprentice_resume(db.connect(tmp_path / "t.db"), None).name.endswith("Resume.pdf")
    assert answers == []
