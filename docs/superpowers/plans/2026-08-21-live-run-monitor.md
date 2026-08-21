# Live Run Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a 15–35 minute pipeline run observable while it happens —
stages, progress, counters, and a streaming activity feed — from a
dismissible modal on the Overview page.

**Architecture:** `run.run_once` gains an optional `progress` callback
(the injected-dependency pattern `gate.score(..., ask)` already uses), so
the CLI module stays free of web imports. `pipeline.run_background`
supplies a callback that writes six additive columns on the
`kind='pipeline'` `run_state` row, opening its own sqlite connection
inside the worker thread. The Overview page's existing 3-second poller
renders the monitor from those columns plus `pipeline_progress` events.

**Tech Stack:** FastAPI, Jinja2, htmx, sqlite3, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-21-live-run-monitor-design.md`

## Global Constraints

- No new dependencies. htmx polling only.
- `run_once` must keep working with `progress` omitted — that is the
  `career-agent run` CLI path, and it must behave exactly as today.
- The progress callback **must not** close over a connection created on
  the main thread. `run_once` executes via
  `asyncio.to_thread(asyncio.run, ...)`, and sqlite3 connections default
  to `check_same_thread=True`. The callback opens its connection lazily,
  on first call, inside the worker thread.
- Schema additions use `db._add_column_if_missing` (already in `db.py`,
  already used for `job.priority`) — additive, idempotent, safe against
  the populated production database, no migration step.
- **The Applications activity log must be filtered to exclude
  `pipeline_%` event types.** Roughly ten `pipeline_progress` rows per run
  would otherwise flood the unfiltered `LIMIT 10` query at
  `web/app.py`'s `_run_status_context` and push out the apply events it
  exists to show.
- **The modal shell must live outside `#pipeline-status-poller`.** Whether
  the modal is open is client state; the poller replaces its target every
  3 seconds. Inside the polled region, every swap would slam the modal
  shut or reopen one the user closed. Same hazard and same fix as the
  Auto/Manual toggle on the Applications page.
- Per-source progress *within* a source is out of scope (discovery is one
  blocking actor call per query). Each source's final count is shown as it
  completes.

---

## Task 1: Progress columns on `run_state`

**Files:**
- Modify: `src/career_agent/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: six nullable/defaulted columns on `run_state` — `stage`
  (TEXT), and `found`, `duplicates`, `passed`, `scored`, `shortlisted`
  (INTEGER NOT NULL DEFAULT 0).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
def test_run_state_has_progress_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(run_state)")}
    assert {"stage", "found", "duplicates", "passed", "scored",
            "shortlisted"} <= cols


def test_progress_counters_default_to_zero(conn):
    row = conn.execute(
        "SELECT stage, found, duplicates, passed, scored, shortlisted"
        " FROM run_state WHERE kind = 'pipeline'").fetchone()
    assert row["stage"] is None
    assert (row["found"], row["duplicates"], row["passed"],
            row["scored"], row["shortlisted"]) == (0, 0, 0, 0, 0)


def test_progress_columns_are_added_idempotently(conn):
    db.init_schema(conn)
    db.init_schema(conn)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(run_state)")]
    assert cols.count("stage") == 1
    assert cols.count("found") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -k progress -v`
Expected: FAIL — `no such column: stage`.

- [ ] **Step 3: Implement**

In `src/career_agent/db.py`'s `init_schema`, beside the existing
`_add_column_if_missing(conn, "job", "priority", "INTEGER")` call:

```python
    # Pipeline run progress. run_state already carries columns meaningful
    # to one kind only (mode and current_job_id are apply-only), so these
    # follow that precedent and keep the status endpoint a single-row read.
    _add_column_if_missing(conn, "run_state", "stage", "TEXT")
    for counter in ("found", "duplicates", "passed", "scored", "shortlisted"):
        _add_column_if_missing(conn, "run_state", counter,
                               "INTEGER NOT NULL DEFAULT 0")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: PASS, including the pre-existing tests.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/db.py tests/test_db.py
git commit -m "feat: add pipeline progress columns to run_state"
```

---

## Task 2: `run_once` emits progress

**Files:**
- Modify: `src/career_agent/run.py`
- Test: `tests/test_run.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `run_once(args, progress=None)`. `progress` is called with
  keyword arguments only, any subset of: `stage` (one of `"discover"`,
  `"clean"`, `"filter"`, `"score"`, `"ready"`), `found`, `duplicates`,
  `passed`, `scored`, `shortlisted` (ints), and `message` (a str for the
  activity feed).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_run.py`. Reuse the file's existing module-level
helpers exactly as its other tests do: `_prepare(tmp_path, monkeypatch)`
returns `(db_path, brief_path, conn)` and already stubs
`discovery.run_discovery` to return `[]`; `_seed_job(conn, fp, location)`
seeds one job; `VERDICT` is a `submit`-verdict `Verdict`; and each test
defines a local `fake_score` and monkeypatches
`"career_agent.gate.score"`.

```python
async def test_run_once_reports_stages_in_order(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)

    seen = []

    def progress(**kw):
        if "stage" in kw:
            seen.append(kw["stage"])

    await run_once(_Args(db_path, max_score=5, brief=brief_path),
                   progress=progress)

    assert seen == ["discover", "clean", "filter", "score", "ready"]


async def test_run_once_reports_counters_matching_what_it_did(
        tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")               # passes the filter
    _seed_job(conn, "near1", "Chennai")               # passes the filter
    _seed_job(conn, "far0", "San Francisco, CA")      # fails it
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)

    latest = {}

    def progress(**kw):
        latest.update(kw)

    await run_once(_Args(db_path, max_score=5, brief=brief_path),
                   progress=progress)

    assert latest["passed"] == 2
    assert latest["scored"] == 2
    assert latest["shortlisted"] == 2, "VERDICT's verdict is 'submit'"
    assert latest["stage"] == "ready"


async def test_run_once_works_without_a_progress_callback(
        tmp_path, monkeypatch):
    """The CLI path. Omitting progress must change nothing."""
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)

    await run_once(_Args(db_path, max_score=5, brief=brief_path))

    conn = db.connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) n FROM assessment WHERE stage='scored'"
    ).fetchone()["n"] == 1
```

Note `found` and `duplicates` are both 0 in these tests: `_prepare` stubs
discovery to return `[]`, which is what keeps the tests fast and focused
on the scoring loop. Task 6's real run is what exercises those two.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_run.py -k "reports_stages or reports_counters" -v`
Expected: FAIL — `run_once() got an unexpected keyword argument 'progress'`.

- [ ] **Step 3: Implement**

In `src/career_agent/run.py`, change the signature and add the emission
points. The existing loop already computes everything except
`shortlisted`, which needs one new counter:

```python
async def run_once(args, progress=None) -> None:
    # Injected rather than writing run_state directly: run.py is the CLI
    # module and must not depend on the web layer. Same shape as the `ask`
    # callable gate.score takes.
    progress = progress or (lambda **kw: None)
```

Then, at the existing boundaries:

```python
    progress(stage="discover",
             message="Starting discovery across LinkedIn, Naukri, and ATS boards.")

    jobs = discovery.run_discovery(...)          # unchanged

    progress(stage="clean", found=len(jobs),
             message=f"Discovery returned {len(jobs)} listings.")

    new_count = store.upsert_jobs(conn, jobs, brief)
    log.info(...)                                 # unchanged
    progress(stage="filter", duplicates=len(jobs) - new_count,
             message=f"{len(jobs) - new_count} duplicate or stale listings removed.")
```

In the scoring loop, add the `shortlisted` counter and per-iteration
reporting. `passed` counts hard-filter survivors:

```python
    scored = skipped = passed = shortlisted = 0
    announced_scoring = False
    for row in store.unscored_jobs(conn, gate.PROMPT_VERSION):
        job = _row_to_job(row)

        reason = hardfilter.check(job, brief)
        if reason:
            store.save_hard_skip(conn, row["id"], reason)
            skipped += 1
            continue

        passed += 1
        if not announced_scoring:
            progress(stage="score",
                     message="Scoring hard-filter survivors against your brief.")
            announced_scoring = True

        if scored >= max_score:
            progress(passed=passed)
            continue  # budget spent; this survivor carries to the next run

        verdict = await gate.score(job, brief, store.facts(conn), ask)
        store.save_assessment(conn, row["id"], verdict, scoring_model,
                              gate.PROMPT_VERSION)
        scored += 1
        if verdict.verdict in ("submit", "hold"):
            shortlisted += 1
        progress(passed=passed, scored=scored, shortlisted=shortlisted)

    log.info("hard-filtered %d, scored %d", skipped, scored)
    progress(stage="ready", passed=passed, scored=scored,
             shortlisted=shortlisted,
             message=f"Run complete — {shortlisted} job(s) ready for review.")
```

Keep every existing line: the `continue` that sweeps the whole pool, the
`log.info` calls, and `derive_no_response` at the end are all unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_run.py -v`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/run.py tests/test_run.py
git commit -m "feat: report progress from run_once via an injected callback"
```

---

## Task 3: The pipeline writes progress, and resets it per run

**Files:**
- Modify: `src/career_agent/web/pipeline.py`
- Modify: `src/career_agent/web/app.py` (the run-now endpoint's reset)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `run_once(args, progress=...)` (Task 2), the columns from
  Task 1, `worker.set_run_state`, `store.log`.
- Produces: `pipeline._progress_writer(conn_factory) -> Callable[..., None]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pipeline.py`. It already has a `conn_factory` fixture.

```python
async def test_progress_is_written_to_the_pipeline_row(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args, progress=None):
        progress(stage="score", found=40, scored=3, shortlisted=2)

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    row = worker.get_run_state(conn, "pipeline")
    assert row["stage"] == "score"
    assert row["found"] == 40
    assert row["scored"] == 3
    assert row["shortlisted"] == 2


async def test_progress_does_not_touch_the_apply_row(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args, progress=None):
        progress(stage="score", found=40)

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    assert worker.get_run_state(conn, "apply")["stage"] is None
    assert worker.get_run_state(conn, "apply")["found"] == 0


async def test_a_message_becomes_an_activity_event(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args, progress=None):
        progress(stage="clean", message="Discovery returned 40 listings.")

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    row = conn.execute(
        "SELECT type, payload FROM event WHERE type = 'pipeline_progress'"
    ).fetchone()
    assert row is not None
    assert row["payload"] == "Discovery returned 40 listings."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `run_once()` is called without `progress` today, so
`progress(...)` raises `TypeError: 'NoneType' object is not callable`.

- [ ] **Step 3: Implement**

In `src/career_agent/web/pipeline.py`:

```python
PROGRESS_FIELDS = ("stage", "found", "duplicates", "passed", "scored",
                   "shortlisted")


def _progress_writer(conn_factory):
    """Build the callback run_once reports through.

    The connection is opened lazily, on first call, because this callback
    runs on the worker thread asyncio.to_thread gives run_once -- and a
    sqlite3 connection created on the main thread cannot be used there
    (check_same_thread). WAL is on, so these writes do not block the
    dashboard's readers, which is the whole point.
    """
    held = {}

    def progress(**kw):
        conn = held.get("conn")
        if conn is None:
            conn = held["conn"] = conn_factory()

        fields = {k: v for k, v in kw.items() if k in PROGRESS_FIELDS}
        if fields:
            worker.set_run_state(conn, "pipeline", **fields)
        if kw.get("message"):
            store.log(conn, None, "pipeline_progress", kw["message"])

    return progress
```

and pass it through:

```python
        await asyncio.to_thread(
            asyncio.run,
            run_module.run_once(args, progress=_progress_writer(conn_factory)))
```

In `src/career_agent/web/app.py`'s `pipeline_run_now`, reset the counters
when a run starts — not when it ends, so a finished run's final numbers
stay visible until the next one begins. Extend the existing
`set_run_state` call:

```python
    worker.set_run_state(conn, "pipeline", status="running", last_error=None,
                         stage=None, found=0, duplicates=0, passed=0,
                         scored=0, shortlisted=0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py tests/test_web.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/pipeline.py src/career_agent/web/app.py tests/test_pipeline.py
git commit -m "feat: write run progress to the pipeline run_state row"
```

---

## Task 4: Stop pipeline events flooding the Applications activity log

**Files:**
- Modify: `src/career_agent/web/app.py:_run_status_context`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no new interfaces; changes one query.

This is small but must not be skipped: without it, a single run's
`pipeline_progress` rows push every apply event out of the Applications
page's `LIMIT 10` activity log.

- [ ] **Step 1: Write the failing test**

```python
def test_the_apply_activity_log_excludes_pipeline_events(client):
    conn = db.connect(web.DB_PATH)
    for i in range(12):
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (NULL, 'pipeline_progress', ?)", (f"step {i}",))
    conn.execute("INSERT INTO event (job_id, type) VALUES (1, 'human_applied')")
    conn.commit()

    r = client.get("/run/status")

    assert "human_applied" in r.text, (
        "a dozen pipeline rows must not push the apply events out of a "
        "LIMIT 10 log")
    assert "pipeline_progress" not in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -k activity_log_excludes -v`
Expected: FAIL — `human_applied` is absent; the twelve newer pipeline rows
fill the whole limit.

- [ ] **Step 3: Implement**

In `_run_status_context`, filter the `recent_events` query:

```python
    recent_events = conn.execute(
        "SELECT type, payload, occurred_at FROM event"
        # the pipeline's own feed lives on the Overview page; an apply
        # activity log that shows discovery progress is showing the wrong
        # thing, and at ~10 rows a run it would show nothing else
        " WHERE type NOT LIKE 'pipeline_%'"
        " ORDER BY id DESC LIMIT 10").fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py tests/test_web.py
git commit -m "fix: keep pipeline events out of the apply activity log"
```

---

## Task 5: The monitor fragment and modal

**Files:**
- Modify: `src/career_agent/web/app.py` (the `/pipeline/status` context)
- Modify: `src/career_agent/web/templates/_pipeline_status.html`
- Modify: `src/career_agent/web/templates/overview.html`
- Modify: `src/career_agent/web/templates/base.html` (CSS)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: the columns from Task 1, the events from Task 3.
- Produces: the rendered monitor. No new Python interfaces.

- [ ] **Step 1: Write the failing tests**

```python
def test_pipeline_status_renders_the_stage_track(client):
    r = client.get("/pipeline/status")
    for label in ("Discover", "Clean", "Filter", "Score", "Ready"):
        assert label in r.text


def test_pipeline_status_renders_the_counters(client):
    conn = db.connect(web.DB_PATH)
    worker.set_run_state(conn, "pipeline", stage="score", found=91,
                         duplicates=7, passed=27, scored=14, shortlisted=6)
    r = client.get("/pipeline/status")
    assert "Jobs Found" in r.text and "91" in r.text
    assert "Passed Filter" in r.text and "27" in r.text
    assert "Shortlisted" in r.text and "6" in r.text


def test_pipeline_status_renders_the_activity_feed(client):
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO event (job_id, type, payload)"
                 " VALUES (NULL, 'pipeline_progress', 'Discovery returned 40.')")
    conn.commit()
    r = client.get("/pipeline/status")
    assert "Discovery returned 40." in r.text


def test_the_modal_shell_is_outside_the_poller(client):
    """Open/closed is client state and the poller replaces its target every
    3s. Inside the polled region, every swap would slam the modal shut or
    reopen one the user closed -- the bug already fixed once for the
    Auto/Manual toggle."""
    r = client.get("/")
    before = r.text.split('id="pipeline-status-poller"')[0]
    assert 'id="runOverlay"' in before, "modal shell precedes the poller"


def test_the_no_auto_apply_note_is_present(client):
    r = client.get("/pipeline/status")
    assert "No automatic applications" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -k "pipeline_status_renders or modal_shell or auto_apply_note" -v`
Expected: FAIL — none of this markup exists.

- [ ] **Step 3: Implement**

In `app.py`'s `pipeline_status` route, widen the context:

```python
@app.get("/pipeline/status", response_class=HTMLResponse)
def pipeline_status(request: Request):
    conn = _conn()
    state = worker.get_run_state(conn, "pipeline")
    feed = conn.execute(
        "SELECT payload, occurred_at FROM event"
        " WHERE type = 'pipeline_progress'"
        " ORDER BY id DESC LIMIT 12").fetchall()
    settings = store.get_settings(conn)
    return templates.TemplateResponse(
        request=request, name="_pipeline_status.html",
        context={"pipeline_state": state,
                 "feed": list(reversed(feed)),   # oldest first, newest last
                 "stages": STAGES,
                 "max_score": settings["max_score_per_run"]})
```

Add the stage list near `LIST_SQL`:

```python
# order matters: the template renders anything before the current stage as
# done and the current one as active
STAGES = (("discover", "Discover"), ("clean", "Clean"), ("filter", "Filter"),
          ("score", "Score"), ("ready", "Ready"))
```

Replace `_pipeline_status.html` with the monitor body. Keep the existing
Run Now button and pill at the top — the Overview page still needs them
when no run is going:

```html
{% set s = pipeline_state %}
{% set stage_keys = stages | map(attribute=0) | list %}
{% set idx = stage_keys.index(s["stage"]) if s["stage"] in stage_keys else -1 %}
{% set pct = 0 if idx < 0 else ((idx + 1) * 100 // stages | length) %}

<div id="pipeline-status">
  {% if s["status"] in ("idle", "error") %}
    <button class="btn primary" hx-post="/pipeline/run-now"
            hx-target="#pipeline-status" hx-swap="outerHTML"
            onclick="openRunMonitor()">▶ Run Now</button>
  {% else %}
    <button class="btn" onclick="openRunMonitor()">◌ Running… (view)</button>
  {% endif %}
  <span class="pill status-{{ s["status"] }}">{{ s["status"] | capitalize }}</span>
  <span class="run-meta">Last update: {{ s["updated_at"] }}</span>
  {% if s["last_error"] %}
    <span class="denied">{{ s["last_error"] }}</span>
  {% endif %}

  <div id="monitor-body" data-status="{{ s['status'] }}"
       data-started="{{ s['started_at'] or '' }}">
    <div class="pipeline-track">
      {% for key, label in stages %}
      <div class="stage {{ 'done' if loop.index0 < idx else ('active' if loop.index0 == idx else '') }}">
        <div class="stage-dot">{{ loop.index }}</div>
        <div class="stage-label">{{ label }}</div>
      </div>
      {% endfor %}
    </div>

    <div class="progress-line"><div class="progress-bar" style="width:{{ pct }}%"></div></div>
    <div class="progress-meta">
      <span>{{ stages[idx][1] if idx >= 0 else "Not started" }}</span>
      <span>{{ pct }}%</span>
    </div>

    <div class="live-counters">
      <div class="live-counter"><div class="counter-top">Jobs Found</div>
        <div class="counter-num">{{ s["found"] }}</div></div>
      <div class="live-counter"><div class="counter-top">Passed Filter</div>
        <div class="counter-num">{{ s["passed"] }}</div></div>
      <div class="live-counter"><div class="counter-top">AI Scored</div>
        <div class="counter-num">{{ s["scored"] }} / {{ max_score }}</div></div>
      <div class="live-counter"><div class="counter-top">Shortlisted</div>
        <div class="counter-num">{{ s["shortlisted"] }}</div></div>
    </div>

    <h3>Live activity</h3>
    <div class="activity">
      {% for e in feed %}
      <div class="activity-item">
        <span class="activity-text">{{ e["payload"] }}</span>
        <span class="activity-time">{{ e["occurred_at"] }}</span>
      </div>
      {% else %}
      <div class="rationale">No activity yet.</div>
      {% endfor %}
    </div>

    <div class="summary-row"><span>Duplicates removed</span><b>{{ s["duplicates"] }}</b></div>
    <div class="live-note"><strong>No automatic applications.</strong> This run
      discovers, filters, deduplicates, and scores jobs. Applying stays a
      deliberate human action from the Applications flow.</div>
  </div>
</div>
```

In `overview.html`, put the modal shell **before** the poller div and
move the poller inside it, so the swap only replaces the inner content:

```html
<!-- Shell outside the polled region on purpose: open/closed is client
     state, and the 3s swap would otherwise slam it shut (or reopen one
     the user closed) on every tick. Same fix as the Applications page's
     Auto/Manual toggle. -->
<div class="live-overlay" id="runOverlay" aria-hidden="true">
  <div class="live-modal" role="dialog" aria-modal="true">
    <div class="live-inner">
      <div class="live-head">
        <h2 class="live-title">Career Agent is running</h2>
        <button class="live-close" onclick="closeRunMonitor()"
                aria-label="Close">×</button>
      </div>
      <div id="pipeline-status-poller" hx-get="/pipeline/status"
           hx-trigger="load, every 3s" hx-swap="innerHTML">
        {% include "_pipeline_status.html" %}
      </div>
      <div class="live-footer">
        <span class="live-footer-meta" id="elapsedTime"></span>
        <button class="btn" onclick="closeRunMonitor()">Continue working</button>
      </div>
    </div>
  </div>
</div>

<script>
function openRunMonitor(){document.getElementById('runOverlay').classList.add('open')}
function closeRunMonitor(){
  document.getElementById('runOverlay').classList.remove('open');
  const body = document.getElementById('monitor-body');
  if (body && body.dataset.status === 'running')
    document.getElementById('runToast').classList.add('open');
}
// Elapsed ticks client-side so it counts smoothly instead of jumping every
// poll. started_at is UTC, matching datetime('now').
setInterval(function(){
  const body = document.getElementById('monitor-body');
  const el = document.getElementById('elapsedTime');
  if (!body || !el) return;
  if (body.dataset.status !== 'running' || !body.dataset.started) {
    el.textContent = ''; return;
  }
  const started = new Date(body.dataset.started.replace(' ', 'T') + 'Z');
  const sec = Math.max(0, Math.floor((Date.now() - started) / 1000));
  el.textContent = 'Elapsed ' + String(Math.floor(sec / 60)).padStart(2, '0')
                 + ':' + String(sec % 60).padStart(2, '0');
}, 1000);
</script>
<div class="run-toast" id="runToast" onclick="openRunMonitor()">
  <span class="toast-dot"></span><span>Career Agent is still running…</span>
</div>
```

Add the prototype's monitor CSS to `base.html`'s `<style>` block —
`.live-overlay`, `.live-overlay.open`, `.live-modal`, `.live-inner`,
`.live-head`, `.live-title`, `.live-close`, `.pipeline-track`, `.stage`,
`.stage-dot`, `.stage-label`, `.progress-line`, `.progress-bar`,
`.progress-meta`, `.live-counters`, `.live-counter`, `.counter-top`,
`.counter-num`, `.activity`, `.activity-item`, `.activity-text`,
`.activity-time`, `.summary-row`, `.live-note`, `.live-footer`,
`.live-footer-meta`, `.run-toast`, `.run-toast.open` — copying the rules
from `preview (2).html` lines 102–125. Adapt the colour literals to the
existing custom properties (`var(--purple)`, `var(--line)`,
`var(--muted)`) where they match, and keep the
`@media (prefers-reduced-motion: reduce)` block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS. The full suite takes ~3 minutes.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/ tests/test_web.py
git commit -m "feat: add the live run monitor to the Overview page"
```

---

## Task 6: Manual verification against a real run

A fake `run_once` cannot prove the emission points sit in the right
places — only a real run can.

- [ ] **Step 1: Work against a copy of the real database**

```bash
mkdir -p data && cp <path-to-real>/data/career.db data/career.db
```

- [ ] **Step 2: Start the dashboard and open `/`**

`.venv/Scripts/python.exe -m uvicorn career_agent.web.app:app --port 8041`

- [ ] **Step 3: Trigger Run Now and watch**

Confirm the modal opens, the stage advances Discover → Clean → Filter →
Score → Ready, the counters climb, and the activity feed fills. Discovery
alone takes 10–20 minutes, so expect a long dwell on Discover — that is
the behaviour being made visible, not a hang.

- [ ] **Step 4: Dismiss mid-run**

Click "Continue working". The modal closes, the toast appears, the run
keeps going. Click the toast; the monitor reopens against the same run
with current numbers.

- [ ] **Step 5: Cross-check the numbers**

When the run finishes, compare the monitor's counters against the run's
own log lines — `discovered N, M new after dedupe` and `hard-filtered X,
scored Y`. Found must equal N, duplicates N−M, passed X's complement
among examined survivors, scored Y.

- [ ] **Step 6: Confirm the apply log is not polluted**

Open `/applications`. Its activity log must show apply events, not
discovery progress.

- [ ] **Step 7: Clean up**

Stop the server, `rm -f data/career.db`, confirm `git status` is clean.
