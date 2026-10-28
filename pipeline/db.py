"""SQLite storage. One file (data/pipeline.db), created on first use."""
import sqlite3
from dataclasses import asdict

from pipeline.settings import DATA

DB_PATH = DATA / "pipeline.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE NOT NULL,
    title TEXT, company TEXT, location TEXT, url TEXT, description TEXT,
    source TEXT, apply_system TEXT, date_found TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new -> filtered | low_fit | match
    fit_score INTEGER, fit_reason TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    service TEXT NOT NULL,      -- 'jsearch', 'claude', ...
    units INTEGER NOT NULL,     -- requests for jsearch, tokens for claude
    cost_usd REAL NOT NULL DEFAULT 0,
    note TEXT                   -- e.g. the search query
);
CREATE TABLE IF NOT EXISTS applications (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    tailoring TEXT,             -- JSON: summary_id, summary, bullets [{id, text}], skills
    keywords TEXT,              -- JSON: terms extracted from the posting
    keywords_matched INTEGER, keywords_total INTEGER,
    missing_keywords TEXT,      -- JSON: posting terms not on the resume (shown as gaps, never added)
    resume_path TEXT,
    flag TEXT,                  -- why it needs your attention (e.g. failed the honesty check)
    letter TEXT,                -- JSON: greeting, paragraphs, sign_off, company_detail, accomplishment_ids
    letter_path TEXT, letter_text_path TEXT,
    letter_flag TEXT,           -- banned phrases, honesty notes
    letter_status TEXT,         -- needs_edit until you've made your personal edit
    apply_status TEXT,          -- filled | needs_answers | manual | submitted | failed
    apply_note TEXT,            -- what's left for you (unanswered questions, why it's manual, errors)
    form_answers TEXT,          -- JSON: [{question, answer, status}] as filled on the form
    screenshot_path TEXT, apply_log_path TEXT,
    approved_at TEXT, submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,       -- approved | draft (AI-written, needs review) | needs_input (only you can answer)
    per_job INTEGER NOT NULL DEFAULT 0,   -- 1 = always drafted fresh for each job (e.g. "Why us?")
    job_id INTEGER REFERENCES jobs(id),   -- set for drafts written for a specific job
    source TEXT NOT NULL,       -- you | ai
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS answer_aliases (
    question TEXT PRIMARY KEY,  -- another site's wording (normalized) confirmed to mean the same question
    answer_id INTEGER NOT NULL REFERENCES answers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- company | organization
    source_id TEXT UNIQUE,       -- Google Maps business id, or the organization's name
    name TEXT NOT NULL, email TEXT, phone TEXT, website TEXT, address TEXT,
    rating REAL, review_count INTEGER, notes TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new | no_email | drafted | approved | sent | replied | do_not_contact
    draft TEXT, thread_id TEXT, sent_at TEXT, replied_at TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS runs (           -- one row per daily pipeline run
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT,
    new_jobs INTEGER, claude_tokens INTEGER, claude_cost_usd REAL, jsearch_requests INTEGER,
    failed TEXT                 -- failed steps, comma-separated; NULL = all fine
);
"""


ADDED_COLUMNS = {
    "jobs": [("fit_score", "INTEGER"), ("fit_reason", "TEXT")],
    "applications": [("letter", "TEXT"), ("letter_path", "TEXT"), ("letter_text_path", "TEXT"),
                     ("letter_flag", "TEXT"), ("letter_status", "TEXT"), ("apply_status", "TEXT"),
                     ("apply_note", "TEXT"), ("form_answers", "TEXT"), ("screenshot_path", "TEXT"),
                     ("apply_log_path", "TEXT"), ("approved_at", "TEXT"), ("submitted_at", "TEXT")],
    "outreach": [("reply", "TEXT")],  # the start of their reply, shown on the Outreach page
}


def connect(path=DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # Columns added after the first release: add them to databases created earlier.
    for table, cols in ADDED_COLUMNS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, kind in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
    return con


def save_new_jobs(con, jobs) -> list:
    """Inserts jobs not already in the database; returns only the new ones."""
    new = []
    with con:
        for job in jobs:
            row = asdict(job) | {"dedupe_key": job.dedupe_key()}
            cur = con.execute(
                f"INSERT OR IGNORE INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
            if cur.rowcount:
                new.append(job)
    return new


def set_result(con, job_id: int, status: str, score: int | None, reason: str):
    with con:
        con.execute("UPDATE jobs SET status = ?, fit_score = ?, fit_reason = ? WHERE id = ?",
                    (status, score, reason, job_id))


def log_usage(con, service: str, units: int, cost_usd: float = 0, note: str | None = None):
    with con:
        con.execute("INSERT INTO api_usage (service, units, cost_usd, note) VALUES (?, ?, ?, ?)",
                    (service, units, cost_usd, note))


def usage_this_month(con, service: str) -> tuple[int, float]:
    row = con.execute(
        "SELECT COALESCE(SUM(units), 0), COALESCE(SUM(cost_usd), 0) FROM api_usage "
        "WHERE service = ? AND strftime('%Y-%m', ts) = strftime('%Y-%m', 'now', 'localtime')",
        (service,),
    ).fetchone()
    return row[0], row[1]


def searched_recently(con, service: str, note: str, days: int = 1) -> bool:
    """True if this search ran within the last `days` days (days=1 means today)."""
    return con.execute(
        "SELECT 1 FROM api_usage WHERE service = ? AND note = ? AND date(ts) > date('now', 'localtime', ?)",
        (service, note, f"-{days} days"),
    ).fetchone() is not None
