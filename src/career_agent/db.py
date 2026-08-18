import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id                 INTEGER PRIMARY KEY,
    fingerprint        TEXT NOT NULL UNIQUE,
    source             TEXT NOT NULL,
    external_id        TEXT NOT NULL,
    company            TEXT NOT NULL,
    company_normalized TEXT NOT NULL,
    title              TEXT NOT NULL,
    title_normalized   TEXT NOT NULL,
    location           TEXT,
    is_remote          INTEGER NOT NULL DEFAULT 0,
    comp_min           INTEGER,
    comp_max           INTEGER,
    posted_at          TEXT,
    url                TEXT,
    description        TEXT,
    merged_into_job_id INTEGER REFERENCES job(id),
    discovered_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assessment (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES job(id),
    stage               TEXT NOT NULL CHECK (stage IN ('hard','scored')),
    role_fit            INTEGER,
    credibility         INTEGER,
    opportunity         INTEGER,
    application_quality INTEGER,
    eligibility_soft    INTEGER,
    weighted_score      REAL,
    verdict             TEXT NOT NULL CHECK (verdict IN ('submit','hold','skip')),
    rationale           TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS application (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES job(id),
    resume_version TEXT NOT NULL,
    answers        TEXT,
    status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
                     ('draft','in_flight','submitted','failed',
                      'failed_permanent','held_unknown')),
    started_at     TEXT,
    submitted_at   TEXT,
    confirmation   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_live_application_per_job
    ON application(job_id)
    WHERE status IN ('in_flight','submitted','held_unknown','failed_permanent');

CREATE TABLE IF NOT EXISTS outcome (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES application(id),
    type           TEXT NOT NULL CHECK (type IN
                     ('no_response','rejected','screen','interview','offer')),
    derived        INTEGER NOT NULL DEFAULT 0,
    occurred_at    TEXT NOT NULL DEFAULT (datetime('now')),
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS event (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER REFERENCES job(id),
    type        TEXT NOT NULL,
    payload     TEXT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fact (
    id         INTEGER PRIMARY KEY,
    claim      TEXT NOT NULL,
    evidence   TEXT NOT NULL,
    project    TEXT,
    metric     TEXT,
    confidence TEXT NOT NULL DEFAULT 'high'
);

CREATE TABLE IF NOT EXISTS qa_bank (
    id                  INTEGER PRIMARY KEY,
    question_normalized TEXT NOT NULL UNIQUE,
    answer              TEXT NOT NULL,
    is_volatile         INTEGER NOT NULL DEFAULT 0,
    last_confirmed_at   TEXT
);

CREATE TABLE IF NOT EXISTS resume (
    id         INTEGER PRIMARY KEY,
    version    TEXT NOT NULL UNIQUE,
    path       TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_assessment_job ON assessment(job_id);
CREATE INDEX IF NOT EXISTS idx_event_job ON event(job_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
