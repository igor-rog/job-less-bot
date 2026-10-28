import httpx

from pipeline import db
from pipeline.sources import Job, apply_system_for, html_to_text, jsearch, watchlist


def job(**kw):
    base = dict(title="Electrical Technician", company="Acme Inc.", location="Toronto, ON",
                url="https://x/1", description="", source="jsearch", apply_system="manual")
    return Job(**(base | kw))


def test_same_job_from_two_sources_dedupes():
    a = job()
    b = job(company="ACME", location="Toronto, Ontario, Canada", url="https://other/2", source="gmail")
    assert a.dedupe_key() == b.dedupe_key()
    assert a.dedupe_key() != job(title="Electrician").dedupe_key()


def test_save_new_jobs_only_returns_unseen(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert len(db.save_new_jobs(con, [job(), job(company="ACME")])) == 1
    assert db.save_new_jobs(con, [job()]) == []
    assert len(db.save_new_jobs(con, [job(title="Electrician")])) == 1


def test_usage_tracking_and_daily_search_check(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert not db.searched_recently(con, "jsearch", "q")
    db.log_usage(con, "jsearch", 1, note="q")
    assert db.searched_recently(con, "jsearch", "q")
    with con:
        con.execute("UPDATE api_usage SET ts = datetime('now', 'localtime', '-1 day')")
    assert not db.searched_recently(con, "jsearch", "q", days=1)
    assert db.searched_recently(con, "jsearch", "q", days=2)
    assert db.usage_this_month(con, "jsearch")[0] == 1


def test_apply_system_detection():
    assert apply_system_for("https://abb.wd3.myworkdayjobs.com/x") == "workday"
    assert apply_system_for("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert apply_system_for("https://ca.indeed.com/viewjob?jk=1") == "manual"


def test_jsearch_prefers_company_application_link():
    raw = {"job_title": "Technician", "employer_name": "Acme   This link will open in a new window",
           "job_location": "Toronto, ON", "job_description": "d", "job_apply_link": "https://ca.indeed.com/x",
           "apply_options": [{"apply_link": "https://ca.indeed.com/x"},
                             {"apply_link": "https://acme.wd1.myworkdayjobs.com/ext/job/1"}]}
    j = jsearch.parse(raw)
    assert (j.company, j.apply_system) == ("Acme", "workday")


def test_title_filter_uses_whole_words():
    assert watchlist.title_matches("RF Test Technician")
    assert not watchlist.title_matches("Performance Marketing Lead")


def test_html_to_text_handles_escaped_html():
    assert html_to_text("&lt;p&gt;Hello&lt;/p&gt;&lt;li&gt;World&lt;/li&gt;").split() == ["Hello", "World"]


def test_workday_keeps_local_jobs_and_resolves_multi_location():
    def handler(req):
        if req.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobPostings": [
                {"title": "Field Service Technician", "externalPath": "/job/1", "locationsText": "Mississauga, Ontario, Canada"},
                {"title": "Test Technician", "externalPath": "/job/2", "locationsText": "3 Locations"},
                {"title": "Electrical Technician", "externalPath": "/job/3", "locationsText": "Calgary, Alberta"},
                {"title": "Accountant", "externalPath": "/job/4", "locationsText": "Toronto"}]})
        details = {"/job/1": {"location": "Mississauga, Ontario, Canada"},
                   "/job/2": {"location": "Montreal", "additionalLocations": ["Toronto, Ontario"]}}
        path = req.url.path.split("/site")[-1]
        return httpx.Response(200, json={"jobPostingInfo": details[path] | {"jobDescription": "<p>Duties</p>"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        jobs = watchlist.workday(client, "Acme", "https://acme.wd3.myworkdayjobs.com/en-US/site")
    assert sorted((j.title, j.location) for j in jobs) == [
        ("Field Service Technician", "Mississauga, Ontario, Canada"), ("Test Technician", "Toronto, Ontario")]
    assert jobs[0].description == "Duties"


def test_company_title_words_override_defaults():
    bank = ["banking associate", "contact centre"]
    assert watchlist.title_matches("Personal Banking Associate", bank)
    assert not watchlist.title_matches("Electrical Technician", bank)


INDEED_DIGEST = """<html><style>.x{}</style><body>
<a href="https://ca.indeed.com/">Find jobs</a>
<a href="https://ca.indeed.com/pagead/clk/dl?from=email&amp;ad=AAA"><span>Electrical Technician</span></a>
<div>Acme Power</div><div>Toronto, ON</div><div>$60,000&ndash;$70,000 a year</div><div>Easily apply</div>
<a href="https://ca.indeed.com/pagead/clk/dl?from=email&amp;ad=BBB">RF Test Technician</a>
<div>Wave Labs</div><div>Markham, ON</div>
<a href="https://ca.indeed.com/?co=CA">&#8203; View more jobs &#8203;</a><div>Unsubscribe</div>
</body></html>"""

INDEED_SINGLE = """<html><body>
<a href="https://ca.indeed.com/rc/clk/dl?jk=abc123&amp;from=x">&#8203; View job &#8203;</a>
<a href="https://cts.indeed.com/v3/zzz">This is a bad match</a>
<a href="https://ca.indeed.com/rc/clk/dl?jk=abc123&amp;from=x">Junior Product Designer</a>
<div>Smile.io</div><div>Remote</div><div>Salary</div><div>$85,000 a year</div>
<a href="https://ca.indeed.com/rc/clk/dl?jk=abc123&amp;from=x">Learn more</a>
</body></html>"""


def test_indeed_digest_email_parses_every_job():
    from pipeline.sources.gmail import SENDERS, parse_alert
    jobs = parse_alert(INDEED_DIGEST, SENDERS["indeed"][1], "indeed")
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("Electrical Technician", "Acme Power", "Toronto, ON"), ("RF Test Technician", "Wave Labs", "Markham, ON")]
    assert "$60,000" in jobs[0].description and "View more" not in jobs[1].description
    assert all(j.apply_system == "manual" for j in jobs)  # never auto-apply through Indeed


def test_indeed_single_job_email_skips_buttons_and_uses_stable_url():
    from pipeline.sources.gmail import SENDERS, parse_alert
    jobs = parse_alert(INDEED_SINGLE, SENDERS["indeed"][1], "indeed")
    assert len(jobs) == 1
    assert (jobs[0].title, jobs[0].company, jobs[0].url) == (
        "Junior Product Designer", "Smile.io", "https://ca.indeed.com/viewjob?jk=abc123")


def test_remote_counts_as_local_only_within_canada():
    from pipeline.sources import is_local
    assert is_local("Mississauga, Ontario, Canada")
    assert is_local("REMOTE/TELETRAVAIL, ON, CAN")
    assert not is_local("Remote")
    assert not is_local("Remote, Massachusetts, United States of America")
    assert not is_local("Calgary, Alberta")
    assert not is_local("Aurora, CO")
    assert is_local("Aurora, ON")


JOBBANK_ALERT = """<html><body>
<a href="https://www.jobbank.gc.ca/home"><img alt=""></a>
<p>We found new jobs for you based on your search criteria.</p>
<b>indeed.com</b> <a href="https://www.jobbank.gc.ca/jobsearch/jobposting/50589720?utm_source=JobBank&amp;utm_medium=Email">Electrical Technician</a>
<div>Acme Power</div><div>Aurora, ON</div><div>$40.00 to $50.00 hourly</div><div>Full time</div>
<a href="https://www.jobbank.gc.ca/jobsearch/?token=abc">View all similar jobs</a>
<a href="https://www.jobbank.gc.ca/unsubscribe/jobalert?token=abc">Unsubscribe</a>
</body></html>"""


def test_jobbank_alert_parses_job_and_drops_tracking():
    from pipeline.sources.gmail import SENDERS, parse_alert
    jobs = parse_alert(JOBBANK_ALERT, SENDERS["jobbank"][1], "jobbank")
    assert [(j.title, j.company, j.location, j.url) for j in jobs] == [(
        "Electrical Technician", "Acme Power", "Aurora, ON", "https://www.jobbank.gc.ca/jobsearch/jobposting/50589720")]
    assert jobs[0].description == "$40.00 to $50.00 hourly\nFull time"


def test_jsearch_survives_a_timeout_and_keeps_other_results(tmp_path, monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url.params["query"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"data": {"jobs": [{
            "job_title": "Technician", "employer_name": "Acme", "job_location": "Toronto, ON",
            "job_description": "", "job_apply_link": "https://acme.example/1", "apply_options": []}]}})

    monkeypatch.setattr(jsearch, "http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("JSEARCH_API_KEY", "test")
    monkeypatch.setitem(jsearch.CONFIG["jsearch"], "queries", ["q1", "q2"])
    monkeypatch.setitem(jsearch.CONFIG["jsearch"], "weekly_queries", [])
    con = db.connect(tmp_path / "t.db")
    jobs = jsearch.fetch(con)
    assert calls == ["q1", "q2"] and len(jobs) == 1
    assert db.usage_this_month(con, "jsearch")[0] == 2  # the failed request is counted too


def test_jsearch_prefers_employer_site_over_job_board_copies():
    base = {"job_title": "Technician", "employer_name": "Veoneer", "job_location": "Markham, ON", "job_description": ""}
    raw = {**base, "job_apply_link": "https://www.jobleads.com/ca/job/x",
           "apply_options": [{"apply_link": "https://www.jobleads.com/ca/job/x"},
                             {"apply_link": "https://ca.indeed.com/viewjob?jk=1"},
                             {"apply_link": "https://careers.veoneer.com/job/123", "is_direct": True}]}
    assert jsearch.parse(raw).url == "https://careers.veoneer.com/job/123"
    only_boards = {**raw, "apply_options": raw["apply_options"][:2]}
    assert jsearch.parse(only_boards).url == "https://ca.indeed.com/viewjob?jk=1"  # a real board beats a copy site


def test_bot_check_pages_are_recognised():
    from types import SimpleNamespace
    from pipeline.assist import is_bot_check
    page = lambda title, body: SimpleNamespace(title=lambda: title, inner_text=lambda sel: body)
    assert is_bot_check(page("Just a moment...", "Performing security verification"))
    assert not is_bot_check(page("Apply - Veoneer", "First name Last name"))


def test_a_failing_source_keeps_the_other_sources_jobs(tmp_path, monkeypatch):
    import pytest
    from pipeline import discover
    con = db.connect(tmp_path / "t.db")
    monkeypatch.setattr(discover.db, "connect", lambda: con)

    def broken(con):
        raise RuntimeError("Gmail login expired")
    monkeypatch.setattr(discover, "SOURCES", {"jsearch": lambda con: [job()], "gmail": broken,
                                              "watchlist": lambda con: [job(title="Electrician")]})
    with pytest.raises(RuntimeError, match="gmail"):  # still reported as a failed step
        discover.run()
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_gmail_error_midway_still_returns_jobs_from_emails_it_marked_read(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pipeline.sources import gmail
    con = db.connect(tmp_path / "t.db")

    def get(userId, id, format):
        if id == "m2":
            raise ConnectionError("network dropped")
        return SimpleNamespace(execute=lambda: {"payload": {"mimeType": "text/html", "body": {
            "data": __import__("base64").urlsafe_b64encode(JOBBANK_ALERT.encode()).decode()}}})
    msgs = SimpleNamespace(list=lambda userId, q, maxResults: SimpleNamespace(
        execute=lambda: {"messages": [{"id": "m1"}, {"id": "m2"}]} if "jobbank" in q else {}), get=get)
    monkeypatch.setattr(gmail, "service", lambda: SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: msgs)))
    jobs = gmail.fetch(con)
    assert [j.title for j in jobs] == ["Electrical Technician"]
    assert [r[0] for r in con.execute("SELECT message_id FROM processed_emails")] == ["m1"]
