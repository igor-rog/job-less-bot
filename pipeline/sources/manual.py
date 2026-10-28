"""Jobs you add yourself by link.

The page is opened in a headless browser (honest user agent) and the small model reads the title,
company and location from its text. Indeed and LinkedIn pages are never fetched (site rules); for
those, save the job description to a text file and pass it along with the link.
"""
from urllib.parse import urlparse

from pipeline import db
from pipeline.settings import CONFIG
from pipeline.sources import USER_AGENT, Job, apply_system_for

NO_FETCH = ("indeed.", "linkedin.")  # never scraped, per the project rules
MAX_CHARS = 12_000
SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "company": {"type": "string"}, "location": {"type": "string"}},
    "required": ["title", "company", "location"], "additionalProperties": False,
}
PROMPT = """From this job posting, give the exact job title, the hiring company's name, and the job's
location (city and province/state, or "Remote"). Use an empty string for anything not stated."""


def blocked(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in NO_FETCH)


def page_text(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5000)  # job boards render the posting with JavaScript
        text = page.inner_text("body")
        browser.close()
    return text


def read_job(client, con, url: str, text: str) -> Job:
    from pipeline.tailor import _ask  # shared helper: logs cost, structured JSON

    text = text[:MAX_CHARS]
    fields = _ask(client, con, CONFIG["models"]["scoring"], PROMPT, f"URL: {url}\n\n{text}", SCHEMA, 1000)
    if fields is None or not fields["title"]:
        raise ValueError("Couldn't find a job title on that page. Save the description to a file and use --file.")
    return Job(fields["title"], fields["company"], fields["location"], url, text, "manual", apply_system_for(url))


def add(con, client, url: str, text: str | None = None) -> str:
    """Saves the job (or re-activates it if already known) and marks it to be prepared.
    Returns a one-line result for the user."""
    if text is None:
        if blocked(url):
            return (f"Not fetched: {urlparse(url).netloc} pages aren't scraped. Copy the job description into a "
                    f"text file and run again with --file <that file>.")
        text = page_text(url)
    job = read_job(client, con, url, text)
    new = db.save_new_jobs(con, [job])
    with con:
        if new:
            return f"Added: {job.title} @ {job.company} ({job.location or 'location not stated'})"
        # Already found by another source: make sure it gets prepared anyway.
        con.execute("UPDATE jobs SET source = 'manual' WHERE dedupe_key = ? AND status IN ('new', 'filtered', 'low_fit')",
                    (job.dedupe_key(),))
        con.execute("UPDATE jobs SET status = 'new' WHERE dedupe_key = ? AND status IN ('filtered', 'low_fit')",
                    (job.dedupe_key(),))
    return f"Already in your list: {job.title} @ {job.company}; it will be prepared."
