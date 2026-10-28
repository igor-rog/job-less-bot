"""The whole daily pipeline in one command.

Run: python -m pipeline.run                  (all sources)
     python -m pipeline.run jsearch gmail    (only these sources; the later steps always run)
     python -m pipeline.run --scheduled      (what Task Scheduler runs: never stops to ask you anything)

discover -> score -> tailor -> cover letters -> prepare forms. Every Claude call stops at the monthly
budget. A failing step is reported and the rest still run, so one expired login doesn't cost the whole
day. Each run's new jobs, tokens and cost are recorded (Usage page). Nothing is submitted or sent: that
only happens from the dashboard.
"""
import sys
import traceback

from pipeline import apply, cover_letter, db, discover, score, tailor
from pipeline.sources import gmail


def _totals(con) -> tuple:
    return con.execute("""SELECT (SELECT COUNT(*) FROM jobs),
        (SELECT COALESCE(SUM(units), 0) FROM api_usage WHERE service = 'claude'),
        (SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE service = 'claude'),
        (SELECT COALESCE(SUM(units), 0) FROM api_usage WHERE service = 'jsearch')""").fetchone()


def main(sources: list[str] | None = None, scheduled: bool = False) -> int:
    if scheduled:
        gmail.INTERACTIVE = False  # an expired sign-in skips Gmail instead of waiting for a browser
    con = db.connect()
    before = _totals(con)
    with con:  # an earlier run whose window was closed never finished; say so instead of "running"
        con.execute("UPDATE runs SET finished_at = datetime('now', 'localtime'), failed = 'stopped before finishing' "
                    "WHERE finished_at IS NULL")
        run_id = con.execute("INSERT INTO runs DEFAULT VALUES").lastrowid
    failed = []
    for name, step in [("discover", lambda: discover.run(sources or None)), ("score", score.run),
                       ("tailor", tailor.run), ("cover letters", cover_letter.run), ("prepare forms", apply.run)]:
        print(f"\n=== {name} ===", flush=True)
        try:
            step()
        except (Exception, SystemExit) as e:  # keep going: later steps still have useful work
            failed.append(name)
            print(f"{name} failed: {e!r}")
            traceback.print_exc()
    jobs, tokens, cost, requests = (a - b for a, b in zip(_totals(con), before))
    with con:
        con.execute("UPDATE runs SET finished_at = datetime('now', 'localtime'), new_jobs = ?, claude_tokens = ?, "
                    "claude_cost_usd = ?, jsearch_requests = ?, failed = ? WHERE id = ?",
                    (jobs, tokens, round(cost, 4), requests, ", ".join(failed) or None, run_id))
    print(f"\nDone: {jobs} new jobs, Claude ${cost:.2f} ({tokens} tokens), {requests} JSearch requests."
          + (f" Failed steps: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(main([a for a in args if a != "--scheduled"], scheduled="--scheduled" in args))
