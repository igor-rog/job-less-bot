"""Gmail job alerts (Indeed, LinkedIn, Job Bank), read-only.

First run opens a browser to sign in: python -m pipeline.sources.gmail
Never modifies the inbox; processed message IDs are remembered in the database.

Every alert email lists jobs the same way: a link whose text is the job title, then the company,
then the location, then a few detail lines (salary, job type). One parser handles all three senders;
only the job-link pattern differs.
"""
import base64
import html as html_lib
import re
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from pipeline.settings import ROOT
from pipeline.sources import Job

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# Only outreach asks for this, the first time you approve sending an email.
SEND_SCOPES = SCOPES + ["https://www.googleapis.com/auth/gmail.send"]
INTERACTIVE = True  # False in scheduled runs: nobody is there to sign in, so fail instead of waiting
CREDENTIALS = ROOT / "credentials.json"
TOKEN = ROOT / "token.json"

# shortcut: Indeed and Job Bank are verified against real emails; the LinkedIn pattern follows its
# public job URLs and needs checking once LinkedIn job alerts arrive.
SENDERS = {
    "indeed": ("from:(match.indeed.com OR alert@indeed.com)", re.compile(r"indeed\.com/(rc|pagead)/clk")),
    "linkedin": ("from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)",
                 re.compile(r"linkedin\.com/(comm/)?jobs/view/\d+")),
    "jobbank": ("from:(no-reply-jobalert@esdc-edsc.gc.ca OR jobbank.gc.ca)",
                re.compile(r"jobbank\.gc\.ca/jobsearch/jobposting/\d+")),
}
_SKIP_TITLES = {"view job", "learn more", "apply now", "see job", "view more jobs", "view all similar jobs"}


def service(scopes=SCOPES):
    creds = Credentials.from_authorized_user_file(TOKEN) if TOKEN.exists() else None
    if creds and not creds.has_scopes(scopes):
        creds = None  # needs a permission not granted yet (e.g. send): Google asks you once
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # refresh tokens expire weekly while the Google app is in Testing mode
            creds = None
    if not creds or not creds.valid:
        if not INTERACTIVE:
            raise RuntimeError("Gmail sign-in expired. Sign in again: python -m pipeline.sources.gmail")
        creds = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, scopes).run_local_server(port=0)
    TOKEN.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _clean_url(url: str) -> str:
    # Indeed links with a job key -> stable job page URL; digest ad links have no key, so keep them whole.
    # Other sites: drop tracking parameters (utm_...).
    jk = parse_qs(urlparse(url).query).get("jk")
    if jk:
        return f"https://ca.indeed.com/viewjob?jk={jk[0]}"
    return url if "indeed.com" in url else url.split("?")[0]


def parse_alert(body: str, link_re: re.Pattern, source: str) -> list[Job]:
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)

    def mark(m):  # turn each job link into a marker line: \x01url\x02title
        url = html_lib.unescape(m[1])
        title = " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", m[2])).split()).strip("​‌ ")
        return f"<br>\x01{url}\x02{title}<br>" if link_re.search(url) else m[0]

    body = re.sub(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', mark, body, flags=re.S | re.I)
    lines = [" ".join(html_lib.unescape(t).split()).strip("​‌  ") for t in re.split(r"<[^>]+>", body)]
    lines = [t for t in lines if t]

    jobs, seen = [], set()
    for i, line in enumerate(lines):
        if not line.startswith("\x01"):
            continue
        url, title = line[1:].split("\x02", 1)
        url = _clean_url(url)
        if not title or title.lower() in _SKIP_TITLES or url in seen:
            continue
        seen.add(url)
        after = []
        for t in lines[i + 1:i + 14]:
            if t.startswith("\x01") or t.lower() in _SKIP_TITLES:  # next job or the email footer
                break
            after.append(t)
        if len(after) < 2:
            continue
        company, location = after[0], after[1]
        jobs.append(Job(title, company, location, url, "\n".join(after[2:]), f"gmail:{source}", "manual"))
    return jobs


def _html_body(payload) -> str:
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    return next((b for p in payload.get("parts") or [] if (b := _html_body(p))), "")


def fetch(con) -> list[Job]:
    svc = service()
    done = {r[0] for r in con.execute("SELECT message_id FROM processed_emails")}
    jobs = []
    try:
        for source, (query, link_re) in SENDERS.items():
            msgs = svc.users().messages().list(userId="me", q=f"{query} newer_than:14d", maxResults=100).execute()
            for m in msgs.get("messages", []):
                if m["id"] in done:
                    continue
                full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
                jobs += parse_alert(_html_body(full["payload"]), link_re, source)
                with con:
                    con.execute("INSERT OR IGNORE INTO processed_emails (message_id) VALUES (?)", (m["id"],))
    except Exception as e:  # emails already marked processed must still hand back their jobs
        print(f"Gmail stopped early: {e!r}")
    return jobs


if __name__ == "__main__":
    profile = service().users().getProfile(userId="me").execute()
    print(f"Connected to Gmail as {profile['emailAddress']} (read-only).")
