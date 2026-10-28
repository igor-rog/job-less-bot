"""Add jobs by link and prepare them right away (score, tailored resume, cover letter).

Run: python -m pipeline.add_job <link> [<link> ...]
     python -m pipeline.add_job <link> --file description.txt   (Indeed/LinkedIn, or pages that won't load)
     python -m pipeline.add_job <link> --no-prepare             (just add it; the next run prepares it)
"""
import argparse
import sys
from pathlib import Path

from pipeline import cover_letter, db, score, tailor
from pipeline.settings import claude
from pipeline.sources import manual


REMINDER = ("Tip: Indeed and LinkedIn links aren't fetched. Copy the job description into a text file "
            "and add --file description.txt")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Add jobs you found yourself.", epilog=REMINDER)
    parser.add_argument("links", nargs="+")
    parser.add_argument("--file", type=Path, help="text file with the job description (one link only)")
    parser.add_argument("--no-prepare", action="store_true")
    args = parser.parse_args(argv)
    if args.file and len(args.links) != 1:
        parser.error("--file goes with exactly one link")

    print(REMINDER)
    con, client = db.connect(), claude()
    for url in args.links:
        text = args.file.read_text(encoding="utf-8") if args.file else None
        try:
            print(manual.add(con, client, url, text))
        except Exception as e:  # one bad link shouldn't stop the others
            print(f"Couldn't add {url}: {e}")
    if not args.no_prepare:
        score.run()
        tailor.run()
        cover_letter.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
