"""JSearch (OpenWeb Ninja): Google for Jobs results, which include Indeed, LinkedIn, Job Bank and more.

Free plan is a hard 200 requests/month, so every request is logged and each query runs at most once
every `repeat_every_days` days.
"""
import os
from urllib.parse import urlparse

import httpx

from pipeline import db
from pipeline.settings import CONFIG
from pipeline.sources import Job, apply_system_for, http

URL = "https://api.openwebninja.com/jsearch/search-v2"


# Big job boards where you can actually apply; ranked after the employer's own site.
MAJOR_BOARDS = ("indeed.", "linkedin.", "glassdoor.", "jobbank.gc.ca", "workopolis.")
# Sites that mostly re-post other boards' jobs, often behind "verify you are human" checks; last resort.
COPY_SITES = ("jobleads.", "jooble.", "sercanto.", "bebee.", "learn4good.", "jobrapido.", "simplyhired.", "talent.com",
              "ziprecruiter.", "careerbeacon.", "jobillico.", "adzuna.", "eluta.", "jobmesh.", "careerjet.", "monster.")


def link_rank(url: str, is_direct: bool = False) -> int:
    """0 company application system, 1 employer's own site, 2 major board, 3 job-board copy."""
    if apply_system_for(url) != "manual":
        return 0
    host = urlparse(url).netloc.lower()
    if any(s in host for s in COPY_SITES):
        return 3
    if any(s in host for s in MAJOR_BOARDS):
        return 2
    return 1 if is_direct or host else 3


def parse(raw_job: dict) -> Job:
    options = [(o["apply_link"], bool(o.get("is_direct"))) for o in raw_job.get("apply_options") or []]
    options.append((raw_job["job_apply_link"], bool(raw_job.get("job_apply_is_direct"))))
    url = min(options, key=lambda o: link_rank(*o))[0]  # first of the best rank
    company = (raw_job.get("employer_name") or "").replace("This link will open in a new window", "").strip()
    return Job(
        title=raw_job["job_title"],
        company=company,
        location=raw_job.get("job_location") or "",
        url=url,
        description=raw_job.get("job_description") or "",
        source="jsearch",
        apply_system=apply_system_for(url),
    )


def fetch(con) -> list[Job]:
    cfg = CONFIG["jsearch"]
    limit = CONFIG["limits"]["jsearch_monthly_requests"]
    if not os.environ.get("JSEARCH_API_KEY"):
        print("JSearch skipped: JSEARCH_API_KEY isn't set (add it to .env in the project folder).")
        return []
    jobs = []
    with http() as client:
        plan = [(q, cfg["repeat_every_days"]) for q in cfg["queries"]] + \
               [(q, 7) for q in cfg.get("weekly_queries", [])]  # lower-priority directions: once a week
        for query, every in plan:
            if db.searched_recently(con, "jsearch", query, every):
                continue
            used, _ = db.usage_this_month(con, "jsearch")
            if used >= limit:
                print(f"JSearch: monthly limit of {limit} requests reached, skipping the rest.")
                break
            db.log_usage(con, "jsearch", 1, note=query)  # counted even on failure: the API may count it too
            try:
                r = client.get(URL, headers={"X-API-Key": os.environ["JSEARCH_API_KEY"]}, timeout=60, params={
                    "query": query, "country": CONFIG["search"]["country"],
                    "date_posted": cfg["date_posted"], "num_pages": 1,
                })
            except httpx.HTTPError as e:  # one slow search shouldn't lose the others or stop the run
                print(f"JSearch: '{query}' failed: {e!r}")
                continue
            if r.status_code != 200:
                print(f"JSearch: '{query}' failed with HTTP {r.status_code}: {r.text[:200]}")
                continue
            found = [parse(j) for j in r.json()["data"]["jobs"]]
            print(f"JSearch: '{query}': {len(found)} jobs")
            jobs += found
    return jobs
