"""Company watchlist: reads watchlist.csv and pulls each company's jobs from its own job board.

One fetcher per application system, picked from the careers URL:
  Greenhouse / Lever: their public JSON APIs.
  Workday: the JSON endpoint behind every myworkdayjobs.com site.
  Anything else: Playwright loads the careers page and keeps links whose text looks like a matching job.
"""
import csv
import re
from urllib.parse import urlparse

from pipeline.settings import CONFIG, ROOT
from pipeline.sources import USER_AGENT, Job, apply_system_for, html_to_text, http, is_local

WATCHLIST_PATH = ROOT / "watchlist.csv"
_WORDS = CONFIG["search"]["watchlist_title_words"]
# Workday has no OR search, so one small search per word; a short list keeps request count down.
_WORKDAY_SEARCHES = ["electrical", "electrician", "technician", "technologist", "apprentice", "RF", "UX", "AI",
                     "security", "production"]


def title_matches(title: str, words: list[str] | None = None) -> bool:
    """words: a company's own title words from watchlist.csv; default is the config list."""
    pattern = r"\b(" + "|".join(re.escape(w) for w in words or _WORDS) + r")"
    return bool(re.search(pattern, title or "", re.I))


def load_watchlist(path=WATCHLIST_PATH) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if row.get("careers_url")]


def greenhouse(client, company: str, url: str, words=None) -> list[Job]:
    token = urlparse(url).path.strip("/").split("/")[0]
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    r.raise_for_status()
    return [Job(j["title"], company, j["location"]["name"], j["absolute_url"], html_to_text(j["content"]),
                "greenhouse", "greenhouse")
            for j in r.json()["jobs"] if title_matches(j["title"], words) and is_local(j["location"]["name"])]


def lever(client, company: str, url: str, words=None) -> list[Job]:
    token = urlparse(url).path.strip("/").split("/")[0]
    r = client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    r.raise_for_status()
    jobs = []
    for j in r.json():
        location = j.get("categories", {}).get("location") or ""
        if title_matches(j["text"], words) and is_local(location):
            jobs.append(Job(j["text"], company, location, j["hostedUrl"], j.get("descriptionPlain") or "",
                            "lever", "lever"))
    return jobs


def workday(client, company: str, url: str, words=None) -> list[Job]:
    # https://{tenant}.wd3.myworkdayjobs.com/en-US/{site} -> POST https://{host}/wday/cxs/{tenant}/{site}/jobs
    parts = urlparse(url)
    tenant = parts.netloc.split(".")[0]
    site = [p for p in parts.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)][0]
    api = f"https://{parts.netloc}/wday/cxs/{tenant}/{site}"
    found = {}
    for word in words or _WORKDAY_SEARCHES:
        r = client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": word})
        r.raise_for_status()
        for p in r.json().get("jobPostings", []):
            loc = p.get("locationsText", "")
            # "3 Locations" hides the cities; the detail call below resolves those.
            if title_matches(p["title"], words) and (is_local(loc) or loc.endswith("Locations")):
                found[p["externalPath"]] = p
    jobs = []
    for path, p in found.items():
        detail = client.get(f"{api}{path}").json().get("jobPostingInfo", {})
        locations = [detail.get("location", "")] + detail.get("additionalLocations", [])
        local = [loc for loc in locations if is_local(loc)]
        if not local:
            continue
        jobs.append(Job(p["title"], company, local[0],
                        detail.get("externalUrl") or f"https://{parts.netloc}/{site}{path}",
                        html_to_text(detail.get("jobDescription", "")), "workday", "workday"))
    return jobs


def careers_page(company: str, url: str, words=None) -> list[Job]:
    """Fallback for unknown systems. Location isn't visible from a link, so these aren't
    location-filtered here; the plain-code filters in Phase 3 handle that from the description."""
    from playwright.sync_api import sync_playwright

    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        # Many careers sites never go network-idle (chat widgets, trackers), so wait a fixed time instead.
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6000)
        links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.innerText.trim(), e.href])")
        seen = set()
        for text, href in links:
            # shortcut: "looks like a job posting" = link path mentions job/posting/requisition and has a digit;
            # drops menu links like "Testing", may miss sites with unusual URLs.
            if not re.search(r"(job|posting|requisition|career).*\d", urlparse(href).path + urlparse(href).query, re.I):
                continue
            if title_matches(text, words) and len(text) < 120 and href not in seen:
                seen.add(href)
                page.goto(href, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1500)  # be polite between page loads
                jobs.append(Job(text, company, "", href, page.inner_text("body")[:8000],
                                "careers_page", apply_system_for(href)))
        browser.close()
    return jobs


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "workday": workday}


def fetch(_con=None, rows=None) -> list[Job]:
    jobs = []
    with http() as client:
        for row in rows if rows is not None else load_watchlist():
            company, url = row["company"], row["careers_url"]
            words = [w.strip() for w in (row.get("title_words") or "").split(";") if w.strip()] or None
            fetcher = FETCHERS.get(apply_system_for(url))
            try:
                found = fetcher(client, company, url, words) if fetcher else careers_page(company, url, words)
            except Exception as e:  # one broken careers site shouldn't stop the run
                print(f"Watchlist: {company} failed: {e}")
                continue
            print(f"Watchlist: {company}: {len(found)} matching jobs")
            jobs += found
    return jobs
