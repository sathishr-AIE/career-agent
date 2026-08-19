# Applications Page + Apply Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn today's read-only, per-job dashboard into a live app shell with
a working Start/Pause/Resume/Stop apply-worker that walks the scored-job
queue, auto or manual mode, under a new `/applications` page.

**Architecture:** A background `asyncio` task (`worker.apply_worker_loop`)
advances one candidate at a time through the existing `ats_apply.submit()`
function, gated by a relocated `guard()` and tracked in a new single-row-per-kind
`run_state` table. FastAPI control endpoints flip `run_state` and each
immediately runs one tick so the UI reflects progress without waiting for the
next loop iteration; the frontend polls `GET /run/status` (htmx) for the rest.

**Tech Stack:** FastAPI, Jinja2, htmx (already in `index.html`), sqlite3,
pytest + pytest-asyncio (`asyncio_mode = "auto"`, already configured).

**Spec:** `docs/superpowers/specs/2026-08-19-application-dashboard-design.md`
(Revision 2) — this plan covers the "apply worker" and "Applications page"
sections plus the shared nav shell. The Overview/pipeline-runner sections are
a separate plan: `docs/superpowers/plans/2026-08-19-overview-pipeline.md`.

## Global Constraints

- No new dependencies. htmx polling only — no SSE/WebSocket (spec Non-goals).
- `application.priority` from the spec's first draft was wrong and is now
  `job.priority` (spec Revision 2 fix, 2026-08-19) — every task below uses
  `job.priority`.
- Existing routes `/apply/{id}`, `/override/{id}`, `/send/{id}`,
  `/dismiss/{id}` keep their exact current behavior and URLs — unchanged.
- `run_state` has exactly one row per `kind` (`'apply'` used here,
  `'pipeline'` seeded but only used by the sibling plan).
- Every DB-writing helper commits before returning, matching the existing
  codebase convention (`store.log`, `ats_apply.submit`, etc. all call
  `conn.commit()` themselves).

---

## Task 1: `run_state` table and `job.priority` column

**Files:**
- Modify: `src/career_agent/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `run_state` table (columns `kind, status, mode, current_job_id,
  started_at, last_error, updated_at`), seeded with two rows
  (`kind='apply'`, `kind='pipeline'`) every time `init_schema` runs.
  `job.priority` (nullable INTEGER).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
def test_run_state_seeded_with_apply_and_pipeline_rows(conn):
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM run_state")}
    assert kinds == {"apply", "pipeline"}


def test_run_state_apply_row_starts_idle_manual(conn):
    row = conn.execute(
        "SELECT status, mode FROM run_state WHERE kind = 'apply'").fetchone()
    assert row["status"] == "idle"
    assert row["mode"] == "manual"


def test_init_schema_is_idempotent_for_run_state(conn):
    db.init_schema(conn)  # called a second time by the fixture's next call
    count = conn.execute("SELECT COUNT(*) n FROM run_state").fetchone()["n"]
    assert count == 2


def test_job_has_priority_column(conn):
    j = _job(conn)
    conn.execute("UPDATE job SET priority = 3 WHERE id = ?", (j,))
    row = conn.execute("SELECT priority FROM job WHERE id = ?", (j,)).fetchone()
    assert row["priority"] == 3


def test_job_priority_defaults_to_null(conn):
    j = _job(conn)
    row = conn.execute("SELECT priority FROM job WHERE id = ?", (j,)).fetchone()
    assert row["priority"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -k "run_state or priority" -v`
Expected: FAIL — `no such table: run_state` / `no such column: priority`.

- [ ] **Step 3: Implement**

In `src/career_agent/db.py`, add to the `SCHEMA` string (after the `event`
table, before `fact`):

```sql
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
```

Replace `init_schema` with:

```python
def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO run_state (kind, mode)"
                 " VALUES ('apply', 'manual')")
    conn.execute("INSERT OR IGNORE INTO run_state (kind) VALUES ('pipeline')")
    _add_column_if_missing(conn, "job", "priority", "INTEGER")
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str,
                            column: str, coltype: str) -> None:
    """CREATE TABLE IF NOT EXISTS can't add a column to a table that already
    exists. init_schema runs on every request (see web/app.py's _conn), so
    this has to be a no-op after the first time it succeeds."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS, all tests including the pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/db.py tests/test_db.py
git commit -m "feat: add run_state table and job.priority column"
```

---

## Task 2: `worker.py` — run-state helpers and the queue candidate query

**Files:**
- Create: `src/career_agent/web/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `db.connect`, `db.init_schema` (for the test fixture only).
- Produces: `get_run_state(conn, kind: str) -> sqlite3.Row`,
  `set_run_state(conn, kind: str, **fields) -> None`,
  `next_candidate(conn) -> sqlite3.Row | None` (columns `job_id, company,
  title, weighted_score`), `queue_count(conn) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_worker.py`:

```python
import pytest

from career_agent import db
from career_agent.web import worker


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(conn, fp, priority=None, score=80, verdict="submit"):
    job_id = conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized, priority)"
        " VALUES (?, 'ats', ?, 'Acme', 'acme', 'AI Engineer',"
        " 'aiengineer', ?)", (fp, fp, priority)).lastrowid
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v1')",
        (job_id, score, verdict))
    conn.commit()
    return job_id


def test_get_run_state_returns_seeded_apply_row(conn):
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "idle"
    assert state["mode"] == "manual"


def test_set_run_state_updates_given_fields_only(conn):
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["mode"] == "auto"
    # pipeline row untouched
    assert worker.get_run_state(conn, "pipeline")["status"] == "idle"


def test_next_candidate_orders_by_score_when_no_priority(conn):
    _job(conn, "low", score=50)
    high = _job(conn, "high", score=90)
    assert worker.next_candidate(conn)["job_id"] == high


def test_next_candidate_priority_overrides_score(conn):
    _job(conn, "high-score", score=99, priority=None)
    low_score_high_priority = _job(conn, "prioritized", score=1, priority=0)
    assert worker.next_candidate(conn)["job_id"] == low_score_high_priority


def test_next_candidate_excludes_skip_verdict(conn):
    _job(conn, "skipped", verdict="skip")
    assert worker.next_candidate(conn) is None


def test_next_candidate_excludes_jobs_with_a_live_application(conn):
    job_id = _job(conn, "applied")
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'submitted')", (job_id,))
    conn.commit()
    assert worker.next_candidate(conn) is None


def test_next_candidate_includes_a_failed_job(conn):
    job_id = _job(conn, "failed-once")
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'failed')", (job_id,))
    conn.commit()
    assert worker.next_candidate(conn)["job_id"] == job_id


def test_queue_count_matches_number_of_selectable_candidates(conn):
    _job(conn, "one")
    _job(conn, "two")
    _job(conn, "skip-me", verdict="skip")
    assert worker.queue_count(conn) == 2


def test_next_candidate_excludes_merged_jobs(conn):
    survivor = _job(conn, "survivor")
    dupe = _job(conn, "dupe")
    conn.execute("UPDATE job SET merged_into_job_id = ? WHERE id = ?",
                 (survivor, dupe))
    conn.commit()
    ids = set()
    row = worker.next_candidate(conn)
    while row:
        ids.add(row["job_id"])
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'submitted')", (row["job_id"],))
        conn.commit()
        row = worker.next_candidate(conn)
    assert dupe not in ids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'career_agent.web.worker'`.

- [ ] **Step 3: Implement**

Create `src/career_agent/web/worker.py`:

```python
import sqlite3

CANDIDATE_SQL = """
SELECT j.id AS job_id, j.company, j.title, a.weighted_score
  FROM job j
  JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL
   AND a.verdict IN ('submit','hold')
   AND NOT EXISTS (
       SELECT 1 FROM application ap
        WHERE ap.job_id = j.id
          AND ap.status IN ('in_flight','submitted','held_unknown',
                             'failed_permanent')
   )
 ORDER BY j.priority ASC NULLS LAST, a.weighted_score DESC
 LIMIT 1
"""


def get_run_state(conn: sqlite3.Connection, kind: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM run_state WHERE kind = ?", (kind,)).fetchone()


def set_run_state(conn: sqlite3.Connection, kind: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE run_state SET {cols}, updated_at = datetime('now')"
        " WHERE kind = ?", (*fields.values(), kind))
    conn.commit()


def next_candidate(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(CANDIDATE_SQL).fetchone()


def queue_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE j.merged_into_job_id IS NULL"
        "   AND a.verdict IN ('submit','hold')"
        "   AND NOT EXISTS (SELECT 1 FROM application ap"
        "                    WHERE ap.job_id = j.id"
        "                      AND ap.status IN ('in_flight','submitted',"
        "                                        'held_unknown','failed_permanent'))"
    ).fetchone()["n"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/worker.py tests/test_worker.py
git commit -m "feat: add run-state helpers and queue candidate query"
```

---

## Task 3: Relocate `_guard` from `app.py` into `worker.py`

**Files:**
- Modify: `src/career_agent/web/app.py`
- Modify: `src/career_agent/web/worker.py`
- Test: `tests/test_worker.py`, `tests/test_web.py` (no URL/behavior changes,
  regression only)

**Interfaces:**
- Produces: `worker.guard(conn, job_id: int, allow_skip: bool,
  brief_path: Path) -> str | None` — same behavior as today's `_guard`,
  parameterized on `brief_path` instead of a module-level constant so
  `worker.py` has no FastAPI/app-module dependency (avoids a circular
  import between `app.py` and `worker.py`, since Task 5 has `app.py`
  import `worker`).

This is a pure relocation — the apply-worker tick (Task 4) needs the same
gating logic `/apply`, `/override`, and `/send` already use, and it has to
live somewhere both `app.py` and `worker.py` can import without a cycle.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_worker.py`:

```python
def test_guard_blocks_a_skip_without_override(conn, tmp_path):
    job_id = _job(conn, "skip-me", verdict="skip")
    brief_path = tmp_path / "career_brief.toml"
    brief_path.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n')
    denial = worker.guard(conn, job_id, allow_skip=False, brief_path=brief_path)
    assert denial is not None
    assert "override" in denial.lower()


def test_guard_allows_a_skip_with_override(conn, tmp_path):
    job_id = _job(conn, "skip-me-2", verdict="skip")
    brief_path = tmp_path / "career_brief.toml"
    brief_path.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n')
    assert worker.guard(conn, job_id, allow_skip=True,
                         brief_path=brief_path) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -k guard -v`
Expected: FAIL — `AttributeError: module 'career_agent.web.worker' has no
attribute 'guard'`.

- [ ] **Step 3: Implement**

Add to `src/career_agent/web/worker.py` (needs new imports at the top:
`from pathlib import Path` and
`from career_agent.config import load_brief`):

```python
def guard(conn: sqlite3.Connection, job_id: int, allow_skip: bool,
          brief_path: Path) -> str | None:
    """Dashboard-side guardrail. The partial unique index is the real
    guarantee; this exists to produce a readable message."""
    a = conn.execute("SELECT verdict FROM assessment WHERE job_id = ?"
                     " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if a is None:
        return "This job has not been scored yet."
    if a["verdict"] == "skip" and not allow_skip:
        return "The gate skipped this one. Use Apply anyway to override."

    brief = load_brief(brief_path)
    used = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    if used >= brief.daily_cap:
        return f"Daily cap of {brief.daily_cap} reached."

    paused = conn.execute(
        "SELECT payload FROM event WHERE type='pause'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    if paused and paused["payload"] == "on":
        return "The agent is paused."
    return None
```

In `src/career_agent/web/app.py`:
1. Delete the module-level `_guard` function (lines 66-88).
2. Add `from career_agent.web import worker` to the imports.
3. Replace every call site `_guard(conn, job_id, allow_skip=...)` with
   `worker.guard(conn, job_id, allow_skip=..., brief_path=BRIEF_PATH)` — this
   is in `_do_apply` (one call) and `send` (one call).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py tests/test_web.py -v`
Expected: PASS — all of `test_web.py`'s existing tests still pass
unchanged, proving the relocation didn't alter behavior.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/worker.py tests/test_worker.py
git commit -m "refactor: move _guard into worker.py as guard()"
```

---

## Task 4: `apply_tick` — one step of the worker loop

**Files:**
- Modify: `src/career_agent/web/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `worker.get_run_state`, `worker.set_run_state`,
  `worker.next_candidate`, `worker.guard` (all Task 2/3),
  `career_agent.apply.ats.submit` (existing, `apply/ats.py:46`),
  `career_agent.store.log` (existing, `store.py:8`).
- Produces: `async def apply_tick(conn, brief_path: Path) -> None` — the
  function the control endpoints (Task 5) and the background loop (Task 6)
  both call.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_worker.py` (needs `import json` and
`from career_agent import store` at the top; `brief_path` fixture factored
out since several tests need it):

```python
@pytest.fixture
def brief_path(tmp_path):
    p = tmp_path / "career_brief.toml"
    p.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 5\n')
    return p


async def test_tick_does_nothing_when_not_running(conn, brief_path):
    job_id = _job(conn, "idle-test")
    await worker.apply_tick(conn, brief_path)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_tick_with_empty_queue_goes_idle_and_logs_completion(conn, brief_path):
    worker.set_run_state(conn, "apply", status="running", mode="auto")
    await worker.apply_tick(conn, brief_path)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "idle"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_completed" in types


async def test_tick_auto_mode_drafts_and_sends(conn, brief_path, monkeypatch):
    job_id = _job(conn, "auto-me")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    calls = []

    async def fake_submit(conn, job_id, dry_run, filler=None):
        calls.append(dry_run)
        status = "draft" if dry_run else "submitted"
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', ?)", (job_id, status))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": status}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path)

    assert calls == [True, False]
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


async def test_tick_manual_mode_stops_after_draft(conn, brief_path, monkeypatch):
    job_id = _job(conn, "manual-me")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    calls = []

    async def fake_submit(conn, job_id, dry_run, filler=None):
        calls.append(dry_run)
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)
    await worker.apply_tick(conn, brief_path)

    assert calls == [True]
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["current_job_id"] == job_id


async def test_tick_is_a_noop_while_awaiting_manual_review(conn, brief_path, monkeypatch):
    job_id = _job(conn, "already-drafted")
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=job_id)

    async def fail_if_called(*a, **kw):
        raise AssertionError("submit should not be called again")

    monkeypatch.setattr(worker.ats_apply, "submit", fail_if_called)
    await worker.apply_tick(conn, brief_path)  # must not raise


async def test_tick_autopauses_on_daily_cap(conn, brief_path):
    p = brief_path.parent / "career_brief.toml"
    p.write_text(
        'target_titles = ["AI Engineer"]\n'
        'search_locations = ["Chennai"]\n'
        'daily_cap = 0\n')
    _job(conn, "capped")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    await worker.apply_tick(conn, p)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "paused"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "run_autopaused" in types


async def test_tick_skips_a_denied_job_and_stays_running(conn, brief_path):
    _job(conn, "gate-skipped", verdict="skip")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    await worker.apply_tick(conn, brief_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["current_job_id"] is None
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "job_skipped" in types


async def test_tick_errors_on_unhandled_submit_exception(conn, brief_path, monkeypatch):
    _job(conn, "boom")
    worker.set_run_state(conn, "apply", status="running", mode="auto")

    async def boom(*a, **kw):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(worker.ats_apply, "submit", boom)
    await worker.apply_tick(conn, brief_path)

    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "error"
    assert "browser crashed" in state["last_error"]
    assert state["current_job_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -k test_tick -v`
Expected: FAIL — `AttributeError: module 'career_agent.web.worker' has no
attribute 'apply_tick'` (and `ats_apply`).

- [ ] **Step 3: Implement**

Add imports to the top of `src/career_agent/web/worker.py`:

```python
from career_agent import store
from career_agent.apply import ats as ats_apply
```

Add to `src/career_agent/web/worker.py`:

```python
async def apply_tick(conn: sqlite3.Connection, brief_path) -> None:
    """One step of the apply worker: pick a candidate, gate it, draft it,
    and in auto mode send it. Called by the control endpoints (for
    immediate feedback) and by the background loop (to keep going
    unattended). A no-op unless the apply run is 'running' and not
    already blocked on a manual-mode draft awaiting review."""
    state = get_run_state(conn, "apply")
    if state["status"] != "running":
        return
    if state["current_job_id"] is not None:
        return

    candidate = next_candidate(conn)
    if candidate is None:
        set_run_state(conn, "apply", status="idle", current_job_id=None)
        store.log(conn, None, "run_completed")
        return

    job_id = candidate["job_id"]
    set_run_state(conn, "apply", current_job_id=job_id)

    denial = guard(conn, job_id, allow_skip=True, brief_path=brief_path)
    if denial:
        if "cap" in denial.lower():
            set_run_state(conn, "apply", status="paused", current_job_id=None)
            store.log(conn, job_id, "run_autopaused", denial)
        else:
            store.log(conn, job_id, "job_skipped", denial)
            set_run_state(conn, "apply", current_job_id=None)
        return

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    if not result["ok"]:
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
        set_run_state(conn, "apply", current_job_id=None)
        return

    if state["mode"] == "manual":
        return  # stays 'running' with current_job_id set: awaiting review

    try:
        await ats_apply.submit(conn, job_id, dry_run=False)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return
    set_run_state(conn, "apply", current_job_id=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/worker.py tests/test_worker.py
git commit -m "feat: add apply_tick, the apply worker's single-step function"
```

---

## Task 5: Control and queue endpoints

**Files:**
- Modify: `src/career_agent/web/app.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `worker.get_run_state`, `worker.set_run_state`,
  `worker.apply_tick` (Tasks 2-4).
- Produces: routes `POST /run/start`, `POST /run/pause`, `POST /run/resume`,
  `POST /run/stop`, `POST /queue/{job_id}/retry`, `POST /queue/{job_id}/skip`,
  `POST /queue/{job_id}/priority`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_run_start_sets_status_running_and_ticks_once(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'draft')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    r = client.post("/run/start", data={"mode": "manual"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "running"
    assert state["mode"] == "manual"
    assert state["current_job_id"] == 1  # job 1 is the higher-scored fixture row


def test_run_pause_sets_status_paused(client):
    client.post("/run/start", data={"mode": "manual"})
    r = client.post("/run/pause")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["status"] == "paused"


def test_run_resume_sets_status_running(client):
    client.post("/run/start", data={"mode": "manual"})
    client.post("/run/pause")
    r = client.post("/run/resume")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["status"] == "running"


def test_run_stop_clears_current_job(client):
    client.post("/run/start", data={"mode": "manual"})
    r = client.post("/run/stop")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    state = worker.get_run_state(conn, "apply")
    assert state["status"] == "stopped"
    assert state["current_job_id"] is None


def test_queue_skip_clears_current_job_and_logs(client):
    client.post("/run/start", data={"mode": "manual"})
    r = client.post("/queue/1/skip")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "job_skipped" in types


def test_queue_retry_only_accepts_failed_status(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'submitted')")
    conn.commit()
    r = client.post("/queue/1/retry")
    assert r.status_code == 200
    assert "failed" in r.text.lower()


def test_queue_retry_on_a_failed_job_bumps_priority_to_front(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'v1', 'failed')")
    conn.execute("UPDATE job SET priority = 5 WHERE id = 2")
    conn.commit()
    r = client.post("/queue/1/retry")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    row = conn.execute("SELECT priority FROM job WHERE id = 1").fetchone()
    assert row["priority"] < 5


def test_queue_priority_swaps_with_neighbor(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("UPDATE job SET priority = 0 WHERE id = 1")
    conn.execute("UPDATE job SET priority = 1 WHERE id = 2")
    conn.commit()
    r = client.post("/queue/2/priority", data={"direction": "up"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    p1 = conn.execute("SELECT priority FROM job WHERE id = 1").fetchone()["priority"]
    p2 = conn.execute("SELECT priority FROM job WHERE id = 2").fetchone()["priority"]
    assert p2 < p1
```

Add `from career_agent.web import worker` to `tests/test_web.py`'s imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k "run_ or queue_" -v`
Expected: FAIL — 404s, since none of these routes exist yet.

- [ ] **Step 3: Implement**

Add to `src/career_agent/web/app.py` (add `from fastapi import Form` to the
`fastapi` import line, keep everything else):

```python
@app.post("/run/start")
async def run_start(mode: str = Form(...)):
    conn = _conn()
    worker.set_run_state(conn, "apply", status="running", mode=mode)
    conn.execute("UPDATE run_state SET started_at = datetime('now')"
                 " WHERE kind = 'apply'")
    conn.commit()
    store.log(conn, None, "run_started", mode)
    await worker.apply_tick(conn, BRIEF_PATH)
    return HTMLResponse("ok")


@app.post("/run/pause")
def run_pause():
    conn = _conn()
    worker.set_run_state(conn, "apply", status="paused")
    store.log(conn, None, "run_paused")
    return HTMLResponse("ok")


@app.post("/run/resume")
async def run_resume():
    conn = _conn()
    worker.set_run_state(conn, "apply", status="running")
    store.log(conn, None, "run_resumed")
    await worker.apply_tick(conn, BRIEF_PATH)
    return HTMLResponse("ok")


@app.post("/run/stop")
def run_stop():
    conn = _conn()
    worker.set_run_state(conn, "apply", status="stopped", current_job_id=None)
    store.log(conn, None, "run_stopped")
    return HTMLResponse("ok")


@app.post("/queue/{job_id}/skip")
async def queue_skip(job_id: int):
    conn = _conn()
    store.log(conn, job_id, "job_skipped", "skipped by user")
    state = worker.get_run_state(conn, "apply")
    if state["current_job_id"] == job_id:
        worker.set_run_state(conn, "apply", current_job_id=None)
        await worker.apply_tick(conn, BRIEF_PATH)
    return HTMLResponse("ok")


@app.post("/queue/{job_id}/retry")
def queue_retry(job_id: int):
    conn = _conn()
    app_row = conn.execute(
        "SELECT status FROM application WHERE job_id = ?"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if app_row is None or app_row["status"] != "failed":
        return HTMLResponse(
            '<span class="denied">Only a failed application can be'
            ' retried.</span>')
    lowest = conn.execute(
        "SELECT MIN(priority) p FROM job").fetchone()["p"]
    new_priority = (lowest - 1) if lowest is not None else 0
    conn.execute("UPDATE job SET priority = ? WHERE id = ?",
                 (new_priority, job_id))
    conn.commit()
    store.log(conn, job_id, "job_skipped", "retry requested; requeued")
    return HTMLResponse('<span class="done">Requeued</span>')


@app.post("/queue/{job_id}/priority")
def queue_priority(job_id: int, direction: str = Form(...)):
    conn = _conn()
    order = conn.execute(
        "SELECT id, priority FROM job WHERE merged_into_job_id IS NULL"
        " ORDER BY priority ASC NULLS LAST, id ASC").fetchall()
    ids = [r["id"] for r in order]
    if job_id not in ids:
        return HTMLResponse('<span class="denied">Not in the queue.</span>')
    pos = ids.index(job_id)
    neighbor_pos = pos - 1 if direction == "up" else pos + 1
    if not (0 <= neighbor_pos < len(ids)):
        return HTMLResponse("ok")  # already at the edge; nothing to swap
    for i, row in enumerate(order):
        conn.execute("UPDATE job SET priority = ? WHERE id = ?", (i, row["id"]))
    conn.commit()
    a, b = ids[pos], ids[neighbor_pos]
    pa = conn.execute("SELECT priority FROM job WHERE id = ?", (a,)).fetchone()["priority"]
    pb = conn.execute("SELECT priority FROM job WHERE id = ?", (b,)).fetchone()["priority"]
    conn.execute("UPDATE job SET priority = ? WHERE id = ?", (pb, a))
    conn.execute("UPDATE job SET priority = ? WHERE id = ?", (pa, b))
    conn.commit()
    return HTMLResponse("ok")
```

Also add `from career_agent.web import worker` and `import Form` to
`app.py`'s imports at the top (`from fastapi import FastAPI, Form,
Request`).

Note on `run_start`'s `started_at`: `set_run_state`'s generic
`col = ?` binding can't express a SQL function call as a bound parameter,
so `started_at` is set with a second literal `UPDATE` right after — keep
both lines.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests in the file including the pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py tests/test_web.py
git commit -m "feat: add apply-run control and queue endpoints"
```

---

## Task 6: Background loop + `GET /run/status` fragment

**Files:**
- Modify: `src/career_agent/web/app.py`
- Create: `src/career_agent/web/templates/_run_status.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /run/status` (HTML fragment for htmx polling), a lifespan
  handler that starts `worker.apply_worker_loop` on app startup.
- Consumes: `worker.apply_tick`, `worker.get_run_state`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_run_status_shows_idle_start_button(client):
    r = client.get("/run/status")
    assert r.status_code == 200
    assert "Start" in r.text
    assert 'hx-post="/run/start"' in r.text


def test_run_status_shows_pause_button_and_current_job_when_running(client):
    client.post("/run/start", data={"mode": "manual"})
    r = client.get("/run/status")
    assert "Pause" in r.text
    assert "AI Engineer" in r.text  # current job's title, from the fixture


def test_run_status_shows_stats(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None):
        conn.execute("INSERT INTO application (job_id, resume_version,"
                     " status) VALUES (?, 'v1', 'submitted')", (job_id,))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "submitted"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/run/start", data={"mode": "auto"})
    r = client.get("/run/status")
    assert r.status_code == 200
    assert "Total Applied" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k run_status -v`
Expected: FAIL — 404 on `GET /run/status`.

- [ ] **Step 3: Implement**

Create `src/career_agent/web/templates/_run_status.html`:

```html
{% set s = run_state %}
<div id="run-status">
  {% if s["status"] in ("idle", "stopped", "error") %}
    <button class="btn primary" hx-post="/run/start"
            hx-vals='{"mode": "{{ s["mode"] or "manual" }}"}'
            hx-target="#run-status" hx-swap="outerHTML">▶ Start</button>
  {% elif s["status"] == "running" %}
    <button class="btn" hx-post="/run/pause"
            hx-target="#run-status" hx-swap="outerHTML">⏸ Pause</button>
    <button class="btn" hx-post="/run/stop"
            hx-target="#run-status" hx-swap="outerHTML">⏹ Stop</button>
  {% elif s["status"] == "paused" %}
    <button class="btn primary" hx-post="/run/resume"
            hx-target="#run-status" hx-swap="outerHTML">▶ Resume</button>
    <button class="btn" hx-post="/run/stop"
            hx-target="#run-status" hx-swap="outerHTML">⏹ Stop</button>
  {% endif %}
  <span class="pill status-{{ s["status"] }}">{{ s["status"] | capitalize }}</span>
  {% if s["last_error"] %}
    <span class="denied">{{ s["last_error"] }}</span>
  {% endif %}

  <div class="kpis">
    <div class="card kpi"><h3>Total Applied</h3><div class="num">{{ stats.total_applied }}</div></div>
    <div class="card kpi"><h3>Queued</h3><div class="num">{{ stats.queued }}</div></div>
    <div class="card kpi"><h3>In Progress</h3><div class="num">{{ stats.in_progress }}</div></div>
    <div class="card kpi"><h3>Successful</h3><div class="num">{{ stats.successful }}</div></div>
    <div class="card kpi"><h3>Failed/Skipped</h3><div class="num">{{ stats.failed_skipped }}</div></div>
  </div>

  {% if current_job %}
  <div class="card now-processing">
    <b>{{ current_job["title"] }}</b> at {{ current_job["company"] }}
    {% if s["mode"] == "manual" %}
      <span class="rationale">Draft ready — review and send.</span>
      <button class="btn" hx-post="/send/{{ current_job['job_id'] }}"
              hx-target="#run-status" hx-swap="outerHTML">Send</button>
      <button class="btn" hx-post="/queue/{{ current_job['job_id'] }}/skip"
              hx-target="#run-status" hx-swap="outerHTML">Skip</button>
    {% else %}
      <span class="rationale">Applying…</span>
    {% endif %}
  </div>
  {% endif %}

  <div class="activity-log">
    {% for e in recent_events %}
      <div class="log-line">{{ e["occurred_at"] }} — {{ e["type"] }}
        {% if e["payload"] %}: {{ e["payload"] }}{% endif %}</div>
    {% endfor %}
  </div>
</div>
```

Add to `src/career_agent/web/app.py`:

```python
def _run_status_context(conn) -> dict:
    state = worker.get_run_state(conn, "apply")
    current_job = None
    if state["current_job_id"]:
        current_job = conn.execute(
            "SELECT j.id AS job_id, j.company, j.title FROM job j"
            " WHERE j.id = ?", (state["current_job_id"],)).fetchone()
    stats = {
        "total_applied": conn.execute(
            "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        ).fetchone()["n"],
        "queued": worker.queue_count(conn),
        "in_progress": 1 if state["current_job_id"] else 0,
        "successful": conn.execute(
            "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        ).fetchone()["n"],
        "failed_skipped": conn.execute(
            "SELECT COUNT(*) n FROM application WHERE status = 'failed_permanent'"
        ).fetchone()["n"],
    }
    recent_events = conn.execute(
        "SELECT type, payload, occurred_at FROM event"
        " ORDER BY id DESC LIMIT 10").fetchall()
    return {"run_state": state, "current_job": current_job, "stats": stats,
            "recent_events": recent_events}


@app.get("/run/status", response_class=HTMLResponse)
def run_status(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="_run_status.html",
        context=_run_status_context(conn))
```

Add the background loop and wire it via FastAPI's lifespan. Replace the
`app = FastAPI(title="Career Agent")` line with:

```python
import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(worker.apply_worker_loop(_conn, BRIEF_PATH))
    yield
    task.cancel()


app = FastAPI(title="Career Agent", lifespan=lifespan)
```

Add to `src/career_agent/web/worker.py`:

```python
import asyncio


async def apply_worker_loop(conn_factory, brief_path) -> None:
    """Keeps the apply run advancing without anyone polling — the piece
    that makes Start actually mean 'walk away'. conn_factory is a
    zero-arg callable (web/app.py's _conn) so each iteration gets a
    fresh connection, matching the rest of the app's per-call pattern."""
    while True:
        conn = conn_factory()
        state = get_run_state(conn, "apply")
        if state["status"] == "running" and state["current_job_id"] is None:
            await apply_tick(conn, brief_path)
        else:
            await asyncio.sleep(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS. (The lifespan-started loop does not interfere with these
tests: `TestClient(web.app)` — used without a `with` block, as the
existing fixture already does — does not trigger ASGI lifespan startup, so
no stray background task runs during the test suite. This is intentional:
production `uvicorn.run(...)` always runs lifespan; tests exercise
`apply_tick` directly instead, which Task 4 already covers.)

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/worker.py src/career_agent/web/templates/_run_status.html tests/test_web.py
git commit -m "feat: add the background apply-worker loop and /run/status fragment"
```

---

## Task 7: Move the job list to `/applications`; `/` becomes a placeholder redirect

**Files:**
- Modify: `src/career_agent/web/app.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /applications` (today's `index()` view, same template
  context, same query params `?show=skipped`), `GET /` (307 redirect to
  `/applications` until the sibling Overview-page plan replaces it).

The Overview page (`GET /`) is a separate plan
(`docs/superpowers/plans/2026-08-19-overview-pipeline.md`). Until it lands,
`/` needs to resolve to *something* rather than 404 — a redirect is the
smallest thing that keeps this plan shippable on its own, and the redirect
handler is exactly what that plan's first task deletes and replaces.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, change every `client.get("/")` /
`client.get("/?show=skipped")` call to `client.get("/applications")` /
`client.get("/applications?show=skipped")` in these existing tests:
`test_index_shows_submit_and_hold_with_rationale`,
`test_index_hides_skips_by_default`, `test_skipped_view_shows_them`,
`test_index_offers_send_once_a_draft_exists`,
`test_index_shows_held_unknown_and_hides_apply_button`,
`test_index_shows_failed_permanent_and_hides_apply_button`,
`test_index_shows_no_schedule_banner_by_default`,
`test_index_hides_banner_when_a_schedule_is_installed`.

Add a new test:

```python
def test_root_redirects_to_applications(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/applications"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -v`
Expected: the newly-edited tests still PASS against the current code
(`/applications` doesn't exist yet, so they should now FAIL with 404), and
`test_root_redirects_to_applications` FAILs (still 200, not a redirect).

- [ ] **Step 3: Implement**

In `src/career_agent/web/app.py`, rename the route decorator on the
existing `index` view from `@app.get("/", response_class=HTMLResponse)` to
`@app.get("/applications", response_class=HTMLResponse)` — the function
body, its `templates.TemplateResponse(... name="index.html" ...)` call, and
every other line stay exactly as they are (Task 8 renames the template
file itself).

Add a new root route above it:

```python
from fastapi.responses import RedirectResponse


@app.get("/")
def root():
    return RedirectResponse("/applications", status_code=307)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py tests/test_web.py
git commit -m "refactor: move the job list to /applications, redirect / to it"
```

---

## Task 8: App shell (`base.html`) and the Applications page UI

**Files:**
- Create: `src/career_agent/web/templates/base.html`
- Modify: `src/career_agent/web/templates/index.html` (rename to
  `applications.html`)
- Modify: `src/career_agent/web/app.py` (template name in the
  `/applications` route)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: the rendered Applications page — top control bar (via
  `_run_status.html`, Task 6), stats row, Now Processing banner, Queue /
  All Applications / Skipped tabs, activity log, toast container.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_applications_page_has_nav_and_tabs(client):
    r = client.get("/applications")
    assert 'href="/"' in r.text  # Dashboard nav link
    assert 'href="/applications"' in r.text
    assert "Queue" in r.text and "Skipped" in r.text


def test_applications_page_embeds_run_status_polling(client):
    r = client.get("/applications")
    assert 'hx-get="/run/status"' in r.text
    assert "every 2s" in r.text


def test_applications_page_shows_career_brief_panel(client):
    r = client.get("/applications")
    assert "Career Brief" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k applications_page -v`
Expected: FAIL — the current `index.html` has none of this markup.

- [ ] **Step 3: Implement**

Create `src/career_agent/web/templates/base.html`:

```html
<!doctype html>
<meta charset="utf-8">
<title>Career Agent</title>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<style>
  :root{
    --bg:#f5f7fb; --panel:rgba(255,255,255,.86); --line:rgba(145,157,184,.22);
    --text:#121827; --muted:#687187; --purple:#6845f5; --blue:#3b82f6;
    --green:#16a36a; --orange:#ea8d0b; --red:#df4e6b;
    --shadow:0 10px 28px rgba(44,52,80,.08); --radius:14px;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       background:var(--bg);color:var(--text);font-size:14px}
  .app{display:grid;grid-template-columns:220px 1fr;min-height:100vh}
  .sidebar{background:var(--panel);border-right:1px solid var(--line);
           padding:16px 12px;display:flex;flex-direction:column;gap:12px;
           position:sticky;top:0;height:100vh}
  .brand{font-weight:800;font-size:16px;padding:4px 8px 12px}
  .nav{display:flex;flex-direction:column;gap:4px}
  .nav a{display:block;text-decoration:none;color:#273049;padding:9px 11px;
         border-radius:10px;font-weight:600}
  .nav a:hover{background:rgba(104,69,245,.08)}
  .nav a.active{background:linear-gradient(100deg,#6340ef,#7450ff);color:#fff}
  .card{background:var(--panel);border:1px solid var(--line);
        border-radius:var(--radius);box-shadow:var(--shadow);padding:14px}
  .brief-head{display:flex;justify-content:space-between;font-weight:800;
              font-size:12px;margin-bottom:10px}
  .kv{display:grid;gap:8px;font-size:12px}
  .kv span{display:block;color:#8b93a5;font-size:10px}
  .main{padding:20px;min-width:0}
  .btn{border:1px solid var(--line);background:#fff;color:#263047;
       border-radius:8px;padding:8px 12px;font-size:12px;font-weight:700;
       cursor:pointer}
  .btn.primary{background:linear-gradient(110deg,#5e3de9,#7655ff);
               border-color:#6444ec;color:#fff}
  .pill{border-radius:999px;padding:5px 10px;font-weight:800;font-size:10px;
        display:inline-block}
  .status-idle,.status-stopped{background:#eef0f4;color:#5c667b}
  .status-running{background:#dcf8e8;color:#15934e}
  .status-paused{background:#fff0d7;color:#d77e00}
  .status-error{background:#fde2e6;color:#b42318}
  .kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:14px 0}
  .kpi h3{font-size:11px;margin:0 0 6px;color:var(--muted)}
  .kpi .num{font-size:24px;font-weight:800}
  .now-processing{margin:10px 0}
  .activity-log{max-height:260px;overflow:auto;margin-top:14px;font-size:11px;color:#555}
  .log-line{padding:4px 0;border-bottom:1px solid var(--line)}
  .denied{color:var(--red)}.done{color:var(--green)}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--line)}
  .tabs{display:flex;gap:8px;margin:12px 0}
  .tabs button{border:1px solid var(--line);background:#fff;border-radius:999px;
               padding:6px 14px;font-size:12px;font-weight:700;cursor:pointer}
  .tabs button.active{background:var(--purple);color:#fff;border-color:var(--purple)}
  .tab-panel{display:none}.tab-panel.active{display:block}
  #toasts{position:fixed;top:16px;right:16px;display:flex;flex-direction:column;
          gap:8px;z-index:50}
  .toast{background:#fff;border:1px solid var(--line);border-radius:10px;
         padding:10px 14px;box-shadow:var(--shadow);font-size:12px}
  @media (max-width:760px){
    .app{display:block}
    .sidebar{height:auto;position:relative}
    .kpis{grid-template-columns:1fr 1fr}
  }
</style>
<div class="app">
  <aside class="sidebar">
    <div class="brand">✦ Career Agent</div>
    <nav class="nav">
      <a href="/" {% if active_nav == "dashboard" %}class="active"{% endif %}>Dashboard</a>
      <a href="#">Discoveries</a>
      <a href="#">Shortlisted</a>
      <a href="/applications" {% if active_nav == "applications" %}class="active"{% endif %}>Applications</a>
      <a href="#">Pipeline</a>
      <a href="#">Calendar</a>
      <a href="#">Outcomes</a>
      <a href="#">Settings</a>
    </nav>
    <div class="card">
      <div class="brief-head"><span>Today's Progress</span></div>
      <div>{{ today_submitted }} / {{ daily_cap }} applications submitted today</div>
    </div>
    <div class="card">
      <div class="brief-head">
        <span>Career Brief</span>
      </div>
      <div class="kv">
        <div><span>Target Titles</span><b>{{ brief.target_titles | join(", ") }}</b></div>
        <div><span>Locations</span><b>{{ brief.locations | join(", ") or "Any" }}</b></div>
        <div><span>Remote OK</span><b>{{ "Yes" if brief.remote_ok else "No" }}</b></div>
        <div><span>Daily Cap</span><b>{{ brief.daily_cap }}</b></div>
        <div><span>Gate Threshold</span><b>{{ brief.gate_threshold }}</b></div>
      </div>
    </div>
  </aside>
  <main class="main">
    {% block content %}{% endblock %}
  </main>
</div>
<div id="toasts"></div>
```

Rename `src/career_agent/web/templates/index.html` to
`src/career_agent/web/templates/applications.html` and replace its
contents with:

```html
{% extends "base.html" %}
{% block content %}
<h1>Applications</h1>

<div hx-get="/run/status" hx-trigger="load, every 2s" hx-swap="outerHTML">
  <div id="run-status">Loading…</div>
</div>

{% if not scheduled %}
<p class="rationale">
  No scheduled run is installed, so nothing runs unless you start it.
  Run <code>career-agent run</code>, or install the daily task with
  <code>scripts\install-scheduler.ps1</code>.
</p>
{% endif %}

<div class="tabs">
  <button class="active" onclick="showTab('queue')">Queue</button>
  <button onclick="showTab('all')">All Applications</button>
  <button onclick="showTab('skipped')">Skipped</button>
</div>

<div id="tab-queue" class="tab-panel active">
  {% set queue_rows = jobs | selectattr("verdict", "in", ["submit", "hold"]) | rejectattr("terminal_status") | list %}
  {% if not queue_rows %}
  <p class="rationale">Nothing to apply to — run discovery, or check Skipped.</p>
  {% else %}
  <table>
    <tr><th>#</th><th>Score</th><th>Role</th><th>Source</th><th>Verdict</th><th></th></tr>
    {% for j in queue_rows %}
    <tr>
      <td>{{ loop.index }}</td>
      <td>{{ (j["score"] or 0) | round | int }}</td>
      <td><a href="{{ j['url'] }}">{{ j["title"] }}</a> at {{ j["company"] }}</td>
      <td>{{ j["source"] }}</td>
      <td class="{{ j['verdict'] }}">{{ j["verdict"] }}</td>
      <td>
        <button hx-post="/queue/{{ j['id'] }}/priority" hx-vals='{"direction":"up"}'>▲</button>
        <button hx-post="/queue/{{ j['id'] }}/priority" hx-vals='{"direction":"down"}'>▼</button>
        <button hx-post="/queue/{{ j['id'] }}/skip">Skip</button>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}
</div>

<div id="tab-all" class="tab-panel">
  <table>
    <tr><th>Score</th><th>Role</th><th>Source</th><th>Verdict</th><th></th></tr>
    {% for j in jobs %}
    <tr>
      <td>{{ (j["score"] or 0) | round | int }}</td>
      <td>
        <a href="{{ j['url'] }}">{{ j["title"] }}</a> at {{ j["company"] }}<br>
        <span class="rationale">{{ j["rationale"] }}</span>
      </td>
      <td>{{ j["source"] }}</td>
      <td class="{{ j['verdict'] }}">{{ j["verdict"] }}</td>
      <td>
        {% if j["terminal_status"] in ("in_flight", "submitted") %}
          <span class="done">Applied</span>
        {% elif j["terminal_status"] == "held_unknown" %}
          <span class="denied">Held — confirm manually, then clear it</span>
        {% elif j["terminal_status"] == "failed_permanent" %}
          <span class="denied">Failed permanently — see the event log</span>
        {% elif j["has_draft"] %}
          <button hx-post="/send/{{ j['id'] }}" hx-swap="outerHTML">Send</button>
        {% elif j["verdict"] == "skip" %}
          <button hx-post="/override/{{ j['id'] }}" hx-swap="outerHTML">Apply anyway</button>
        {% else %}
          <button hx-post="/apply/{{ j['id'] }}" hx-swap="outerHTML">Apply</button>
          <button hx-post="/dismiss/{{ j['id'] }}" hx-swap="outerHTML">Dismiss</button>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
</div>

<div id="tab-skipped" class="tab-panel">
  <p class="rationale">
    These were skipped by the gate. Applying anyway is recorded as an override,
    which is how the gate gets caught being too strict.
  </p>
  <table>
    <tr><th>Score</th><th>Role</th><th>Source</th><th></th></tr>
    {% for j in skipped_jobs %}
    <tr>
      <td>{{ (j["score"] or 0) | round | int }}</td>
      <td><a href="{{ j['url'] }}">{{ j["title"] }}</a> at {{ j["company"] }}<br>
        <span class="rationale">{{ j["rationale"] }}</span></td>
      <td>{{ j["source"] }}</td>
      <td><button hx-post="/override/{{ j['id'] }}" hx-swap="outerHTML">Apply anyway</button></td>
    </tr>
    {% endfor %}
  </table>
</div>

<script>
function showTab(name) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
}

let lastEventCount = null;
document.body.addEventListener('htmx:afterSettle', function(evt) {
  if (evt.target.id !== 'run-status') return;
  const lines = evt.target.querySelectorAll('.log-line').length;
  if (lastEventCount !== null && lines > lastEventCount) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = evt.target.querySelector('.log-line').textContent;
    document.getElementById('toasts').appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
  }
  lastEventCount = lines;
});
</script>
{% endblock %}
```

In `src/career_agent/web/app.py`, update the `/applications` route's
`TemplateResponse` call to `name="applications.html"`, and its `context`
dict to include the values `base.html` and this template now need:

```python
@app.get("/applications", response_class=HTMLResponse)
def applications(request: Request, show: str = "queue"):
    conn = _conn()
    verdicts = ["skip"] if show == "skipped" else ["submit", "hold"]
    sql = LIST_SQL.format(placeholders=",".join("?" * len(verdicts)))
    rows = conn.execute(sql, verdicts).fetchall()
    all_rows = conn.execute(
        LIST_SQL.format(placeholders="?,?,?"), ["submit", "hold", "skip"]
    ).fetchall()
    brief = load_brief(BRIEF_PATH)
    today_submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    return templates.TemplateResponse(
        request=request, name="applications.html",
        context={"jobs": rows if show != "skipped" else all_rows,
                 "skipped_jobs": [r for r in all_rows if r["verdict"] == "skip"],
                 "show": show, "scheduled": scheduled_task_installed(),
                 "active_nav": "applications", "brief": brief,
                 "daily_cap": brief.daily_cap,
                 "today_submitted": today_submitted})
```

This replaces the old `index` function's body entirely (same route name
change already applied in Task 7 — this step supersedes that function's
context dict and template name).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/base.html src/career_agent/web/templates/applications.html
git rm src/career_agent/web/templates/index.html
git commit -m "feat: app shell and full Applications page UI"
```

---

## Task 9: Manual verification

- [ ] **Step 1: Start the dashboard**

Use the `run` skill (or `career-agent serve`) to start the dashboard, and
open `http://localhost:8000/`.

- [ ] **Step 2: Confirm the redirect and nav**

Confirm `/` redirects to `/applications`, the sidebar renders with the
Career Brief panel showing real values from `career_brief.toml`, and the
Today's Progress line shows `0 / 5` (or whatever today's real submitted
count and `daily_cap` are).

- [ ] **Step 3: Drive a manual-mode run**

With at least one `submit`/`hold`-verdict job in the queue (seed one via
`sqlite3 data/career.db` if the database is empty), click Start in manual
mode. Confirm the Now Processing banner appears with Send/Skip buttons,
the stats row updates, and the activity log gains lines — all without a
page reload (htmx polling every 2s).

- [ ] **Step 4: Drive pause/resume/stop**

Click Pause mid-run, confirm the button becomes Resume and the status pill
turns amber; Resume; then Stop, and confirm the banner clears and the
button returns to Start.

- [ ] **Step 5: Confirm the worker survives closing the tab**

Start a run in **auto** mode (requires `ats_apply.submit`'s real Playwright
path or a temporarily monkeypatched stub — if using the real path, expect
it to actually attempt browser automation), close the browser tab, wait a
few seconds, reopen `/applications`, and confirm the run progressed (stats
changed) without the tab having been open. This is the behavior the
backend-worker architecture decision was for — confirm it's real, not just
described.

- [ ] **Step 6: Report results**

Note any visual issues or behavior gaps found — fix inline if small, or
flag as a follow-up if they require design decisions beyond this plan's
scope.
