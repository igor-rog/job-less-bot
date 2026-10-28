"""Runs every job source, dedupes, and stores only jobs not seen before.

Run: python -m pipeline.discover            (all sources)
     python -m pipeline.discover jsearch    (just one)
"""
import sys

from pipeline import db
from pipeline.sources import gmail, jsearch, watchlist

SOURCES = {"jsearch": jsearch.fetch, "gmail": gmail.fetch, "watchlist": watchlist.fetch}


def run(names=None) -> list:
    con = db.connect()
    new, seen, failed = [], 0, []
    for name in names or SOURCES:
        try:  # each source is saved as soon as it returns, so a later failure can't lose its jobs
            jobs = SOURCES[name](con)
        except Exception as e:
            failed.append(name)
            print(f"{name} failed: {e!r}")
            continue
        saved = db.save_new_jobs(con, jobs)
        print(f"{name}: {len(jobs)} jobs, {len(saved)} new")
        new += saved
        seen += len(jobs) - len(saved)
    used, _ = db.usage_this_month(con, "jsearch")
    print(f"{len(new)} new jobs saved ({seen} already seen). JSearch this month: {used} requests.")
    if failed:
        raise RuntimeError(f"these sources failed: {', '.join(failed)}")  # run.py reports it; the rest is saved
    return new


if __name__ == "__main__":
    run(sys.argv[1:])
