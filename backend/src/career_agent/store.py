import json
import logging
import re
import sqlite3

from career_agent import normalize
from career_agent.apply import ats
from career_agent.config import SCORING_MODELS, CareerBrief
from career_agent.models import Job, Verdict

logger = logging.getLogger(__name__)


def log(conn, job_id: int | None, type_: str, payload: str | None = None) -> None:
    conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                 (job_id, type_, payload))
    conn.commit()


def upsert_jobs(conn, jobs: list[Job], brief: CareerBrief) -> int:
    """Insert jobs that are new and fresh. Returns how many were inserted."""
    inserted = 0
    for job in jobs:
        if normalize.is_stale(job, brief.staleness_days):
            continue
        try:
            conn.execute(
                "INSERT INTO job (fingerprint, source, external_id, company,"
                " company_normalized, title, title_normalized, location,"
                " is_remote, comp_min, comp_max, posted_at, url, description)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (normalize.fingerprint(job), job.source, job.external_id,
                 job.company, normalize.company(job.company), job.title,
                 normalize.title(job.title), job.location, int(job.is_remote),
                 job.comp_min, job.comp_max, job.posted_at, job.url,
                 job.description))
            inserted += 1
        except sqlite3.IntegrityError:
            continue  # same role, already seen from another board
    conn.commit()
    return inserted


def save_hard_skip(conn, job_id: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO assessment (job_id, stage, verdict, rationale, model,"
        " prompt_version) VALUES (?, 'hard', 'skip', ?, 'hardfilter', 'n/a')",
        (job_id, reason))
    conn.commit()


def save_assessment(conn, job_id: int, v: Verdict, model: str,
                    prompt_version: str) -> None:
    conn.execute(
        "INSERT INTO assessment (job_id, stage, role_fit, credibility,"
        " opportunity, application_quality, eligibility_soft, weighted_score,"
        " verdict, rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?,?,?,?,?,?,?,?,?,?)",
        (job_id, v.role_fit, v.credibility, v.opportunity, v.application_quality,
         v.eligibility_soft, v.weighted, v.verdict, v.rationale, model,
         prompt_version))
    conn.commit()


def unscored_jobs(conn, prompt_version: str,
                  limit: int | None = None) -> list[sqlite3.Row]:
    """Jobs with no assessment at the current prompt version, excluding
    hard-filter skips and merged duplicates. Bumping the version brings
    previously scored jobs back, which is what makes prompt changes measurable.

    limit=None returns the whole pool, which is what callers want: rationing
    rows here would let hard-filtered jobs consume a scoring budget they
    never spend a model call against."""
    sql = ("SELECT j.* FROM job j"
           " WHERE j.merged_into_job_id IS NULL"
           "   AND NOT EXISTS (SELECT 1 FROM assessment a WHERE a.job_id = j.id"
           "                     AND (a.stage = 'hard'"
           "                          OR a.prompt_version = ?))"
           " ORDER BY j.discovered_at DESC")
    params: list = [prompt_version]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def facts(conn) -> list[str]:
    rows = conn.execute("SELECT claim, evidence FROM fact ORDER BY id").fetchall()
    return [f"{r['claim']} (evidence: {r['evidence']})" for r in rows]


def fact_rows(conn) -> list[tuple[int, str]]:
    """Same text format as facts(), paired with each row's id so tailor.py
    can ask the model to cite which facts a bullet draws from, and validate
    that citation against real rows before anything is rendered."""
    rows = conn.execute("SELECT id, claim, evidence FROM fact ORDER BY id").fetchall()
    return [(r["id"], f"{r['claim']} (evidence: {r['evidence']})") for r in rows]


def latest_resume_version(conn, job_id: int) -> str | None:
    row = conn.execute(
        "SELECT version FROM resume WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()
    return row["version"] if row else None


def resume_version_for(conn, job_id: int) -> str:
    """What to record on an application for this job: the most recent
    tailored version if one exists, else the untailored fallback constant."""
    return latest_resume_version(conn, job_id) or ats.RESUME_VERSION


def next_resume_version(conn, job_id: int) -> str:
    n = conn.execute("SELECT COUNT(*) n FROM resume WHERE job_id = ?",
                     (job_id,)).fetchone()["n"]
    return f"tailored-{job_id}-r{n + 1}"


def insert_resume(conn, job_id: int, version: str, path: str,
                  content: str) -> str | None:
    """Insert a new resume row. resume.version is UNIQUE, and two
    near-simultaneous Apply clicks on the same job can both compute the
    same next_resume_version() before either commits -- handled the same
    way upsert_jobs handles the equivalent race on job.fingerprint: attempt
    the insert, and on a collision report back whichever row actually won
    rather than raising."""
    try:
        conn.execute(
            "INSERT INTO resume (version, path, job_id, content)"
            " VALUES (?, ?, ?, ?)", (version, path, job_id, content))
        conn.commit()
        return version
    except sqlite3.IntegrityError:
        return latest_resume_version(conn, job_id)


def qa_normalize(question: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- just enough
    that "Notice period?" and "notice period" collide on the same row."""
    text = re.sub(r"[^\w\s]", "", question.lower())
    return " ".join(text.split())


def qa_lookup(conn, question: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM qa_bank WHERE question_normalized = ?",
        (qa_normalize(question),)).fetchone()


def qa_upsert(conn, question: str, answer: str, is_volatile: bool) -> None:
    """Confirming an existing answer is itself a reconfirmation, so
    last_confirmed_at is set unconditionally on every call, not only on
    first insert."""
    conn.execute(
        "INSERT INTO qa_bank (question_normalized, answer, is_volatile,"
        " last_confirmed_at) VALUES (?, ?, ?, datetime('now'))"
        " ON CONFLICT(question_normalized) DO UPDATE SET"
        "   answer = excluded.answer, is_volatile = excluded.is_volatile,"
        "   last_confirmed_at = excluded.last_confirmed_at",
        (qa_normalize(question), answer, int(is_volatile)))
    conn.commit()


# A memory_key is agent-proposed text that becomes a second question_normalized
# row (see qa_remember below) -- a loose key ("notice period", with a space)
# would normalize to the same string as an unrelated literal question and
# silently merge the two. Snake_case with at least one underscore keeps keys
# and literal questions in visibly different shapes.
_MEMORY_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")


def qa_remember(conn, question: str, answer: str, *, kind: str = "text",
                options=None, memory_key: str | None = None,
                source_job_id: int | None = None,
                is_volatile: bool = False) -> None:
    """Upsert the literal question as answered, and -- when memory_key is
    given AND valid snake_case -- also upsert a second, canonical row keyed
    by the memory_key itself (question_normalized == qa_normalize(memory_key)),
    so qa_by_key() and the PREFERENCES prompt section can find the preference
    regardless of which job's exact wording produced the answer. Only that
    canonical row carries memory_key; the literal per-job row's memory_key
    stays NULL, or _preferences_section would print the same preference
    twice. Confirming an existing answer is itself a reconfirmation, so
    last_confirmed_at is set unconditionally on every call, not only on
    first insert. An invalid memory_key (not ^[a-z][a-z0-9]*(_[a-z0-9]+)+$)
    is dropped -- the literal row is still stored -- and logged, since the
    agent proposes keys and a bad one must not silently collide with an
    unrelated question (see _MEMORY_KEY_RE)."""
    if memory_key and not _MEMORY_KEY_RE.match(memory_key):
        logger.warning("qa_remember: rejected memory_key %r (must be snake_case,"
                       " e.g. 'notice_period') -- storing the literal answer only",
                       memory_key)
        memory_key = None

    options_json = json.dumps(options) if options is not None else None

    def _upsert(question_normalized: str, mem_key: str | None) -> None:
        conn.execute(
            "INSERT INTO qa_bank (question_normalized, answer, is_volatile,"
            " last_confirmed_at, kind, options_json, source_job_id, memory_key)"
            " VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?)"
            " ON CONFLICT(question_normalized) DO UPDATE SET"
            "   answer = excluded.answer, is_volatile = excluded.is_volatile,"
            "   last_confirmed_at = excluded.last_confirmed_at,"
            "   kind = excluded.kind, options_json = excluded.options_json,"
            "   source_job_id = excluded.source_job_id,"
            "   memory_key = excluded.memory_key",
            (question_normalized, answer, int(is_volatile), kind, options_json,
             source_job_id, mem_key))

    _upsert(qa_normalize(question), None)
    if memory_key:
        _upsert(qa_normalize(memory_key), memory_key)
    conn.commit()


def qa_by_key(conn, memory_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM qa_bank WHERE question_normalized = ?",
        (qa_normalize(memory_key),)).fetchone()


def qa_touch(conn, keys: list[str]) -> None:
    """use_count += 1, last_used_at = now for each key (normalized), called
    when a CONFIRM payload reports a memory was actually used. Unknown keys
    are a silent no-op -- there is no row to bump."""
    for key in keys:
        conn.execute(
            "UPDATE qa_bank SET use_count = use_count + 1,"
            " last_used_at = datetime('now') WHERE question_normalized = ?",
            (qa_normalize(key),))
    conn.commit()


def qa_all(conn) -> list:
    """Return every qa_bank row with all fields the prompt builder needs,
    ordered deterministically by question."""
    return conn.execute(
        "SELECT question_normalized, answer, is_volatile, last_confirmed_at,"
        " memory_key, kind, options_json, source_job_id, use_count,"
        " last_used_at FROM qa_bank ORDER BY question_normalized").fetchall()


def _literal_twins(conn, answer: str, source_job_id: int | None):
    """The literal (memory_key IS NULL) rows qa_remember wrote alongside a
    keyed row: same answer text, same source_job_id. `source_job_id IS ?`
    (not `=`) so a NULL source_job_id still matches NULL, the SQLite way."""
    return conn.execute(
        "SELECT id FROM qa_bank WHERE memory_key IS NULL"
        " AND answer = ? AND source_job_id IS ?", (answer, source_job_id))


def qa_update(conn, row_id: int, answer: str, is_volatile: bool) -> bool:
    """Edit one qa_bank row (the /memory drawer's Save). When the row is
    keyed, its literal twins -- identified the same way qa_remember wrote
    them, by the row's PRIOR answer and source_job_id -- are updated too, so
    editing the canonical preference doesn't leave the per-job history rows
    stale (see the "keyed vs literal drift" ruling). Returns False for an
    unknown id."""
    row = conn.execute("SELECT * FROM qa_bank WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return False
    if row["memory_key"]:
        twins = [r["id"] for r in _literal_twins(conn, row["answer"], row["source_job_id"])]
        for twin_id in twins:
            conn.execute(
                "UPDATE qa_bank SET answer = ?, last_confirmed_at = datetime('now')"
                " WHERE id = ?", (answer, twin_id))
    conn.execute(
        "UPDATE qa_bank SET answer = ?, is_volatile = ?,"
        " last_confirmed_at = datetime('now') WHERE id = ?",
        (answer, int(is_volatile), row_id))
    conn.commit()
    return True


def qa_delete(conn, row_id: int) -> bool:
    """Delete one qa_bank row and, for a keyed row, its literal twins
    (same identification as qa_update). Returns False for an unknown id."""
    row = conn.execute("SELECT * FROM qa_bank WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return False
    if row["memory_key"]:
        conn.execute(
            "DELETE FROM qa_bank WHERE memory_key IS NULL"
            " AND answer = ? AND source_job_id IS ?",
            (row["answer"], row["source_job_id"]))
    conn.execute("DELETE FROM qa_bank WHERE id = ?", (row_id,))
    conn.commit()
    return True


def memory_list(conn) -> list[dict]:
    """The /memory drawer's rows: every keyed (preference) row, plus literal
    rows that are NOT a keyed row's twin (same answer + source_job_id) --
    those twins are history, not something to edit or show twice (see the
    "keyed vs literal drift" ruling). A literal row that coincidentally
    shares both an answer and a (possibly NULL) source_job_id with an
    unrelated keyed row would also be hidden by this; accepted as the same
    limitation the ruling already names."""
    rows = conn.execute(
        "SELECT id, question_normalized, answer, is_volatile, last_confirmed_at,"
        " memory_key, kind, options_json, source_job_id, use_count,"
        " last_used_at FROM qa_bank ORDER BY question_normalized").fetchall()
    keyed_twin_keys = {(r["answer"], r["source_job_id"]) for r in rows if r["memory_key"]}

    items = []
    for r in rows:
        is_keyed = bool(r["memory_key"])
        if not is_keyed and (r["answer"], r["source_job_id"]) in keyed_twin_keys:
            continue
        items.append({
            "id": r["id"],
            "label": r["memory_key"] or r["question_normalized"],
            "answer": r["answer"],
            "kind": r["kind"],
            "options": json.loads(r["options_json"]) if r["options_json"] else None,
            "is_volatile": bool(r["is_volatile"]),
            "last_confirmed_at": r["last_confirmed_at"],
            "use_count": r["use_count"],
            "last_used_at": r["last_used_at"],
            "is_preference": is_keyed,
        })
    return items


def get_settings(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM setting WHERE id = 1").fetchone()


def save_settings(conn, scoring_model: str, max_score_per_run: int) -> None:
    if scoring_model not in SCORING_MODELS:
        raise ValueError(f"unknown scoring model: {scoring_model}")
    if max_score_per_run < 0:
        raise ValueError("max_score_per_run cannot be negative")
    conn.execute(
        "UPDATE setting SET scoring_model = ?, max_score_per_run = ?,"
        " updated_at = datetime('now') WHERE id = 1",
        (scoring_model, max_score_per_run))
    conn.commit()


def mark_applied(conn, job_id: int, when: str) -> int:
    """Record that a human applied to this job on the site themselves.

    This is the callback-rate denominator. Nothing else produces it: the
    agent does not submit (v3, and Naukri never), so without this the
    denominator stays zero and no outcome can be attached to anything.

    Promotes an existing draft when there is one so a single application
    attempt stays a single row. A `held_unknown` row is promoted the same
    way: it means "the agent may already have submitted", and this is the
    human resolving that to "yes, it went through" -- inserting a second row
    instead would hit the one_live_application_per_job index, which is what
    made Held a dead end the applications page could not clear. Raises
    sqlite3.IntegrityError via that index if the job already has a
    submitted / in_flight / failed_permanent application.
    """
    draft = conn.execute(
        "SELECT id, status FROM application WHERE job_id = ?"
        " AND status IN ('draft', 'held_unknown')"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()

    if draft is None:
        # answers stays NULL: for a manual application we do not know what
        # was sent, and saying so is better than copying a placeholder.
        cur = conn.execute(
            "INSERT INTO application (job_id, resume_version, status,"
            " submitted_at) VALUES (?, ?, 'submitted', ?)",
            (job_id, resume_version_for(conn, job_id), when))
        app_id = cur.lastrowid
    else:
        app_id = draft["id"]
        if draft["status"] == "draft":
            # answers is nulled rather than kept: the draft's answers are
            # precisely what was NOT sent, so carrying them forward would
            # make the row read as a record of what the human submitted --
            # which a future real Send is meant to rely on. A held_unknown
            # row is the opposite case: its answers are what the agent
            # actually typed into the live form, the only audit trail the
            # submission has, so they stay.
            conn.execute("UPDATE application SET answers = NULL WHERE id = ?",
                         (app_id,))
        conn.execute(
            "UPDATE application SET status = 'submitted', submitted_at = ?"
            " WHERE id = ?", (when, app_id))

    conn.commit()
    log(conn, job_id, "human_marked_applied", when)
    return app_id
