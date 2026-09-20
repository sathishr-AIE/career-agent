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

CREATE TABLE IF NOT EXISTS run_state (
    kind           TEXT PRIMARY KEY CHECK (kind IN ('pipeline','apply')),
    status         TEXT NOT NULL DEFAULT 'idle'
                    CHECK (status IN
                     ('idle','running','paused','stopped','error')),
    mode           TEXT CHECK (mode IN ('auto','manual')),
    current_job_id INTEGER REFERENCES job(id),
    started_at     TEXT,
    last_error     TEXT,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS setting (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    scoring_model     TEXT NOT NULL DEFAULT 'claude-sonnet-5',
    apply_model       TEXT NOT NULL DEFAULT 'claude-sonnet-5',
    max_score_per_run INTEGER NOT NULL DEFAULT 25
                        CHECK (max_score_per_run >= 0),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS conversation (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('home','job')),
    job_id     INTEGER UNIQUE REFERENCES job(id),
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS message (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversation(id),
    role            TEXT NOT NULL CHECK (role IN ('user','agent','system','prompt')),
    content         TEXT NOT NULL,
    payload         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS message_conv_id ON message(conversation_id, id);

CREATE TABLE IF NOT EXISTS agent_prompt (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER REFERENCES job(id),  -- NULL: a Home confirmation card
    conversation_id INTEGER NOT NULL REFERENCES conversation(id),
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','answered','expired')),
    answer          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at     TEXT
);

CREATE TABLE IF NOT EXISTS apply_checkpoint (
    job_id         INTEGER PRIMARY KEY REFERENCES job(id),
    session_id     TEXT NOT NULL,
    nonce          TEXT NOT NULL,
    step           TEXT NOT NULL DEFAULT 'start',
    answers        TEXT NOT NULL DEFAULT '{}',
    form_url       TEXT,
    open_prompt_id INTEGER,
    status         TEXT NOT NULL CHECK (status IN ('running','waiting','resumable','done')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS site_credential (
    id            INTEGER PRIMARY KEY,
    domain        TEXT NOT NULL UNIQUE,
    login_url     TEXT,
    email         TEXT NOT NULL,
    password_enc  TEXT NOT NULL,
    created_by    TEXT NOT NULL CHECK (created_by IN ('agent','user')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at  TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers work through a writer's transaction. Without it the
    # per-request init_schema write (every route, including the 3s status
    # poll) contends with the pipeline's writes from its worker thread and
    # sporadically raises "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_AGENT_PROMPT_REBUILD = (
    "CREATE TABLE agent_prompt_new (id INTEGER PRIMARY KEY,"
    " job_id INTEGER REFERENCES job(id),"
    " conversation_id INTEGER NOT NULL REFERENCES conversation(id),"
    " kind TEXT NOT NULL, payload TEXT NOT NULL,"
    " status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','expired')),"
    " answer TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), answered_at TEXT)",
    "INSERT INTO agent_prompt_new SELECT id, job_id, conversation_id, kind, payload,"
    " status, answer, created_at, answered_at FROM agent_prompt",
    "DROP TABLE agent_prompt",
    "ALTER TABLE agent_prompt_new RENAME TO agent_prompt",
)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO run_state (kind, mode)"
                 " VALUES ('apply', 'manual')")
    conn.execute("INSERT OR IGNORE INTO run_state (kind) VALUES ('pipeline')")
    conn.execute("INSERT OR IGNORE INTO setting (id) VALUES (1)")
    _add_column_if_missing(conn, "job", "priority", "INTEGER")
    # Set by Skip / Dismiss (actions.queue_skip, actions.dismiss): the job leaves
    # the queue (worker.QUEUE_WHERE) until queue_restore or queue_retry clears it.
    _add_column_if_missing(conn, "job", "dismissed_at", "TEXT")
    # Pipeline run progress. run_state already carries columns meaningful
    # to one kind only (mode and current_job_id are apply-only), so these
    # follow that precedent and keep the status endpoint a single-row read.
    _add_column_if_missing(conn, "run_state", "stage", "TEXT")
    for counter in ("found", "duplicates", "passed", "scored", "shortlisted"):
        _add_column_if_missing(conn, "run_state", counter,
                               "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "resume", "job_id", "INTEGER REFERENCES job(id)")
    _add_column_if_missing(conn, "resume", "content", "TEXT")
    _add_column_if_missing(conn, "application", "failure_reason", "TEXT")
    _add_column_if_missing(conn, "application", "transcript_path", "TEXT")
    # S4 personalized memory: keyed preferences layered onto the existing
    # literal-question qa_bank rows -- see store.qa_remember/qa_by_key.
    _add_column_if_missing(conn, "qa_bank", "memory_key", "TEXT")
    _add_column_if_missing(conn, "qa_bank", "kind", "TEXT")
    _add_column_if_missing(conn, "qa_bank", "options_json", "TEXT")
    _add_column_if_missing(conn, "qa_bank", "source_job_id", "INTEGER")
    _add_column_if_missing(conn, "qa_bank", "use_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "qa_bank", "last_used_at", "TEXT")
    # Explicit twin link (fix round 1, T13 review): a literal row's twin_key
    # names the memory_key of the keyed row it was written alongside, so
    # qa_update/qa_delete/memory_list identify twins by that link instead of
    # by (answer, source_job_id) coincidence -- see store.qa_remember.
    _add_column_if_missing(conn, "qa_bank", "twin_key", "TEXT")
    # Resume bookkeeping (apply/checkpoint.py): what a resume must match,
    # whether a DECISION approve went out, the resume cap, the worker's one auto-resume.
    _add_column_if_missing(conn, "setting", "apply_model",
                           "TEXT NOT NULL DEFAULT 'claude-sonnet-5'")
    # The model this session was started on: a --resume must not switch
    # models mid-session, exactly as it must not switch mode/can_submit.
    # NULL means an older row that predates the pin; submit falls back to
    # the setting.
    _add_column_if_missing(conn, "apply_checkpoint", "model", "TEXT")
    _add_column_if_missing(conn, "apply_checkpoint", "mode", "TEXT")
    _add_column_if_missing(conn, "apply_checkpoint", "can_submit", "INTEGER")
    # Human notes from the job's chat (web/actions.job_message), pinned here
    # so a --resume's CONTINUE line still carries them once the live run's
    # own in-memory queue (apply/runner.AgentRun.notes) is gone.
    _add_column_if_missing(conn, "apply_checkpoint", "notes", "TEXT NOT NULL DEFAULT '[]'")
    for col in ("approve_sent", "resume_count", "auto_resumed"):
        _add_column_if_missing(conn, "apply_checkpoint", col, "INTEGER NOT NULL DEFAULT 0")
    # A Home confirmation card has no job. SQLite can't drop NOT NULL
    # in place, so an older DB's agent_prompt is rebuilt once (nothing references it).
    if any(r["name"] == "job_id" and r["notnull"]
           for r in conn.execute("PRAGMA table_info(agent_prompt)")):
        conn.commit()
        # Off for the copy: an orphan row (a deleted job) must not fail the
        # migration and leave every request's init_schema on a locked DB.
        # PRAGMA foreign_keys is a no-op inside a transaction, hence before BEGIN.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            for stmt in _AGENT_PROMPT_REBUILD:
                conn.execute(stmt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str,
                            column: str, coltype: str) -> None:
    """CREATE TABLE IF NOT EXISTS can't add a column to a table that already
    exists. init_schema runs on every request (see web/app.py's _conn), so
    this has to be a no-op after the first time it succeeds."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
