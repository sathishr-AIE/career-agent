# Outcome Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user record that they applied to a job themselves, and
record what came back — closing v1's exit gate, which requires that
callback data exist.

**Architecture:** Two writes on existing tables, no schema changes.
`store.mark_applied` promotes a job's `draft` application row to
`submitted` (or inserts one when none exists), creating the callback-rate
denominator. `outcomes.record` inserts a manual `outcome` row. Both are
surfaced as controls on the existing Applications page's "All
Applications" tab.

**Tech Stack:** FastAPI, Jinja2, htmx, sqlite3, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-21-outcome-recording-design.md`

## Global Constraints

- **No schema changes.** Both writes use existing tables and columns. The
  only new value anywhere is one `event.type` string,
  `human_marked_applied`, and `event.type` is free-text by design.
- No new dependencies.
- `no_response` is **never** manually settable — it is derived after 30
  days by `outcomes.derive_no_response`. The four manual types are
  `rejected`, `screen`, `interview`, `offer`.
- Corrections work by **superseding**, not editing. No delete or edit UI.
  `outcomes.effective_outcome` already resolves "latest `occurred_at`
  wins, derived loses ties".
- Date and type form fields are accepted as `str` and parsed in the
  handler — **never** declared as typed `Form` parameters. A typed
  parameter makes FastAPI reject a malformed value with a raw 422 before
  the handler runs, bypassing the friendly error path. This codebase has
  already shipped and fixed that exact bug once.
- A manually-applied row gets `answers = NULL`. Null is the honest value:
  for a manual application the system genuinely does not know what was
  sent, and it must not inherit `_default_filler`'s placeholder string.
- Every DB-writing helper commits before returning, matching the codebase
  convention (`store.log`, `store.save_hard_skip`, etc.).

---

## Task 1: `store.mark_applied`

**Files:**
- Modify: `src/career_agent/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `store.log` (existing, `store.py:8`),
  `ats.RESUME_VERSION` (existing constant, `apply/ats.py:8`).
- Produces: `mark_applied(conn, job_id: int, when: str) -> int` — returns
  the application id. Raises `sqlite3.IntegrityError` when the job
  already has a live application.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`, reusing its existing `conn` fixture,
`_job(**kw)` helper (which builds a `Job` model, not a row), and the
module-level `BRIEF`. Note the fingerprint is derived from the normalized
company and title — `source` and `external_id` do not make a job
distinct — so a second job needs a different `company`.

```python
def _seed_one(conn, company="Acme") -> int:
    store.upsert_jobs(conn, [_job(company=company)], BRIEF)
    return conn.execute("SELECT id FROM job WHERE company = ?",
                        (company,)).fetchone()["id"]


def test_mark_applied_promotes_an_existing_draft(conn):
    """The normal path: 'Open & track' left a draft, and the user then
    applied on the site. Promote that row rather than inserting a second,
    so one application attempt stays one row."""
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " answers) VALUES (?, 'base-v1', 'draft', '{\"note\": \"x\"}')",
                 (job_id,))
    conn.commit()

    app_id = store.mark_applied(conn, job_id, "2026-08-20")

    rows = conn.execute("SELECT * FROM application WHERE job_id = ?",
                        (job_id,)).fetchall()
    assert len(rows) == 1, "promoted, not duplicated"
    assert rows[0]["id"] == app_id
    assert rows[0]["status"] == "submitted"
    assert rows[0]["submitted_at"] == "2026-08-20"
    assert rows[0]["answers"] == '{"note": "x"}', "draft's record preserved"


def test_mark_applied_inserts_when_there_is_no_draft(conn):
    """Applying straight from the job board without tracking it first."""
    job_id = _seed_one(conn)

    app_id = store.mark_applied(conn, job_id, "2026-08-19")

    row = conn.execute("SELECT * FROM application WHERE id = ?",
                       (app_id,)).fetchone()
    assert row["status"] == "submitted"
    assert row["submitted_at"] == "2026-08-19"
    assert row["answers"] is None, (
        "a manual application's contents are genuinely unknown; NULL says so "
        "rather than inheriting the stub filler's placeholder")
    assert row["resume_version"] == ats_apply.RESUME_VERSION


def test_mark_applied_logs_the_human_decision(conn):
    job_id = _seed_one(conn)
    store.mark_applied(conn, job_id, "2026-08-20")
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_marked_applied" in types


def test_mark_applied_refuses_a_job_that_already_has_one(conn):
    """The partial unique index one_live_application_per_job makes double
    marking structurally impossible. Confirm it actually fires."""
    job_id = _seed_one(conn)
    store.mark_applied(conn, job_id, "2026-08-20")
    with pytest.raises(sqlite3.IntegrityError):
        store.mark_applied(conn, job_id, "2026-08-21")
```

Add `import sqlite3` and `import pytest` to the file's imports if absent,
and `from career_agent.apply import ats as ats_apply`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -k mark_applied -v`
Expected: FAIL — `AttributeError: module 'career_agent.store' has no attribute 'mark_applied'`.

- [ ] **Step 3: Implement**

Add to `src/career_agent/store.py`:

```python
def mark_applied(conn, job_id: int, when: str) -> int:
    """Record that a human applied to this job on the site themselves.

    This is the callback-rate denominator. Nothing else produces it: the
    agent does not submit (v3, and Naukri never), so without this the
    denominator stays zero and no outcome can be attached to anything.

    Promotes an existing draft when there is one so a single application
    attempt stays a single row. Raises sqlite3.IntegrityError via the
    one_live_application_per_job index if the job already has a live
    application.
    """
    draft = conn.execute(
        "SELECT id FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()

    if draft is None:
        # answers stays NULL: for a manual application we do not know what
        # was sent, and saying so is better than copying a placeholder.
        cur = conn.execute(
            "INSERT INTO application (job_id, resume_version, status,"
            " submitted_at) VALUES (?, ?, 'submitted', ?)",
            (job_id, ats.RESUME_VERSION, when))
        app_id = cur.lastrowid
    else:
        app_id = draft["id"]
        conn.execute(
            "UPDATE application SET status = 'submitted', submitted_at = ?"
            " WHERE id = ?", (when, app_id))

    conn.commit()
    log(conn, job_id, "human_marked_applied", when)
    return app_id
```

Add `from career_agent.apply import ats` to `store.py`'s imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -v`
Expected: PASS, including the pre-existing tests.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/store.py tests/test_store.py
git commit -m "feat: add store.mark_applied for manually-applied jobs"
```

---

## Task 2: `outcomes.record`

**Files:**
- Modify: `src/career_agent/outcomes.py`
- Test: `tests/test_outcomes.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `MANUAL_TYPES` (a tuple), and
  `record(conn, application_id: int, type_: str, occurred_at: str, notes: str | None = None) -> int`
  returning the outcome id. Raises `ValueError` for `no_response` or an
  unknown type.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_outcomes.py`. That file already has a `conn` fixture
and an `_app(conn, days_ago)` helper that inserts a submitted
application — reuse both.

```python
def test_record_writes_a_manual_outcome(conn):
    app_id = _app(conn, 3)
    out_id = outcomes.record(conn, app_id, "screen", "2026-08-20")
    row = conn.execute("SELECT * FROM outcome WHERE id = ?",
                       (out_id,)).fetchone()
    assert row["type"] == "screen"
    assert row["derived"] == 0, "hand-entered, not inferred"
    assert row["occurred_at"] == "2026-08-20"


def test_record_stores_notes_when_given(conn):
    app_id = _app(conn, 3)
    out_id = outcomes.record(conn, app_id, "interview", "2026-08-20",
                             notes="30 min with the hiring manager")
    row = conn.execute("SELECT notes FROM outcome WHERE id = ?",
                       (out_id,)).fetchone()
    assert row["notes"] == "30 min with the hiring manager"


def test_record_refuses_no_response(conn):
    """Derived, never entered. The schema CHECK would accept it, so the
    write path has to be the thing that refuses."""
    app_id = _app(conn, 3)
    with pytest.raises(ValueError, match="derived"):
        outcomes.record(conn, app_id, "no_response", "2026-08-20")
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 0


def test_record_refuses_an_unknown_type(conn):
    app_id = _app(conn, 3)
    with pytest.raises(ValueError):
        outcomes.record(conn, app_id, "ghosted", "2026-08-20")


def test_a_later_manual_outcome_supersedes_an_earlier_one(conn):
    """Corrections work by recording again, not editing."""
    app_id = _app(conn, 30)
    outcomes.record(conn, app_id, "screen", "2026-08-10")
    outcomes.record(conn, app_id, "rejected", "2026-08-20")
    assert outcomes.effective_outcome(conn, app_id) == "rejected"


def test_a_manual_outcome_beats_a_derived_no_response(conn):
    """The spec's day-40 case: a reply arrives after we assumed silence.
    The derived row is not deleted; it simply loses."""
    app_id = _app(conn, 40)
    outcomes.derive_no_response(conn, after_days=30)
    assert outcomes.effective_outcome(conn, app_id) == "no_response"

    outcomes.record(conn, app_id, "screen", "2026-08-21")

    assert outcomes.effective_outcome(conn, app_id) == "screen"
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 2, \
        "history intact"


def test_callback_data_can_now_exist(conn):
    """The whole point of this change. The v1 -> v2 gate requires callback
    data, and today the denominator cannot leave zero because nothing
    produces a submitted application. Fails against the old code."""
    from career_agent import store
    job_id = conn.execute(
        "SELECT id FROM job LIMIT 1").fetchone()["id"]
    app_id = store.mark_applied(conn, job_id, "2026-08-20")
    outcomes.record(conn, app_id, "screen", "2026-08-21")
    assert outcomes.callback_rate(conn) == (1, 1)
```

Add `import pytest` to the imports if absent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_outcomes.py -k "record or callback_data" -v`
Expected: FAIL — `AttributeError: module 'career_agent.outcomes' has no attribute 'record'`.

- [ ] **Step 3: Implement**

Add to `src/career_agent/outcomes.py`, below `CALLBACK_TYPES`:

```python
# no_response is deliberately absent: nobody writes to say they are
# ignoring you, so it is derived after 30 days rather than entered. Left
# manual, the table would fill with rejections and screens and stay silent
# on the majority case, inflating callback rate by shrinking its
# denominator to whatever you remembered to annotate.
MANUAL_TYPES = ("rejected", "screen", "interview", "offer")
```

and the writer:

```python
def record(conn: sqlite3.Connection, application_id: int, type_: str,
           occurred_at: str, notes: str | None = None) -> int:
    """Hand-enter an outcome. Corrections supersede rather than edit:
    effective_outcome takes the latest occurred_at, so recording again
    overrides without deleting the history."""
    if type_ == "no_response":
        raise ValueError(
            "no_response is derived after 30 days, not entered by hand")
    if type_ not in MANUAL_TYPES:
        raise ValueError(
            f"unknown outcome type {type_!r}; expected one of {MANUAL_TYPES}")

    cur = conn.execute(
        "INSERT INTO outcome (application_id, type, derived, occurred_at,"
        " notes) VALUES (?, ?, 0, ?, ?)",
        (application_id, type_, occurred_at, notes))
    conn.commit()
    return cur.lastrowid
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_outcomes.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/outcomes.py tests/test_outcomes.py
git commit -m "feat: add outcomes.record for hand-entered outcomes"
```

---

## Task 3: The two routes

**Files:**
- Modify: `src/career_agent/web/app.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `store.mark_applied` (Task 1), `outcomes.record` and
  `outcomes.MANUAL_TYPES` (Task 2).
- Produces: routes `POST /applied/{job_id}` and
  `POST /outcome/{application_id}`; helper
  `_parse_date(raw: str, field: str, errors: dict) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`. The file's `client` fixture already seeds two
jobs (job 1 `submit`, job 2 `skip`) and monkeypatches `web.DB_PATH`.

```python
def test_marking_applied_creates_the_denominator(client):
    r = client.post("/applied/1", data={"when": "2026-08-20"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    row = conn.execute(
        "SELECT status, submitted_at FROM application WHERE job_id = 1"
    ).fetchone()
    assert row["status"] == "submitted"
    assert row["submitted_at"] == "2026-08-20"


def test_marking_applied_defaults_to_today(client):
    client.post("/applied/1", data={"when": ""})
    conn = db.connect(web.DB_PATH)
    today = conn.execute("SELECT date('now') d").fetchone()["d"]
    row = conn.execute(
        "SELECT submitted_at FROM application WHERE job_id = 1").fetchone()
    assert row["submitted_at"] == today


def test_marking_applied_twice_is_refused_readably(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    r = client.post("/applied/1", data={"when": "2026-08-21"})
    assert r.status_code == 200, "a readable message, not a 500"
    assert "already" in r.text.lower()


def test_a_malformed_date_is_an_error_not_a_422(client):
    r = client.post("/applied/1", data={"when": "last tuesday"})
    assert r.status_code == 200, "friendly error, not FastAPI's raw 422"
    assert "date" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert conn.execute(
        "SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


def test_recording_an_outcome_persists_it(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.post(f"/outcome/{app_id}",
                    data={"type": "screen", "occurred_at": "2026-08-21",
                          "notes": "recruiter call"})
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    row = conn.execute("SELECT type, derived, notes FROM outcome").fetchone()
    assert row["type"] == "screen"
    assert row["derived"] == 0
    assert row["notes"] == "recruiter call"


def test_recording_no_response_by_hand_is_refused(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.post(f"/outcome/{app_id}",
                    data={"type": "no_response", "occurred_at": "2026-08-21"})
    assert r.status_code == 200
    assert "derived" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 0


def test_an_outcome_on_an_unsubmitted_application_is_refused(client):
    """The UI does not offer this, so it guards a forged request."""
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (1, 'base-v1', 'draft')")
    conn.commit()
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.post(f"/outcome/{app_id}",
                    data={"type": "screen", "occurred_at": "2026-08-21"})
    assert r.status_code == 200
    assert "submitted" in r.text.lower()
    conn = db.connect(web.DB_PATH)
    assert conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -k "applied or outcome" -v`
Expected: FAIL — 404s, since neither route exists.

- [ ] **Step 3: Implement**

Add to `src/career_agent/web/app.py`. Add `from career_agent import outcomes`
to the imports and `import sqlite3` if absent.

```python
def _parse_date(raw: str, field: str, errors: dict) -> str | None:
    """Accept an ISO date, defaulting to today when blank. Parsed here, not
    declared as a typed Form parameter: a typed parameter makes FastAPI
    reject a bad value with a raw 422 before this handler runs, which skips
    the friendly error page entirely."""
    raw = (raw or "").strip()
    if not raw:
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        errors[field] = "must be a date like 2026-08-20"
        return None


@app.post("/applied/{job_id}", response_class=HTMLResponse)
def mark_applied(job_id: int, when: str = Form("")):
    conn = _conn()
    errors: dict[str, str] = {}
    day = _parse_date(when, "when", errors)
    if errors:
        return HTMLResponse(
            f'<span class="denied">{escape(errors["when"])}</span>')

    try:
        store.mark_applied(conn, job_id, day)
    except sqlite3.IntegrityError:
        return HTMLResponse('<span class="denied">This job already has a'
                            ' live application.</span>')
    return HTMLResponse('<span class="done">Marked applied</span>')


@app.post("/outcome/{application_id}", response_class=HTMLResponse)
def record_outcome(application_id: int, type: str = Form(""),
                   occurred_at: str = Form(""), notes: str = Form("")):
    conn = _conn()
    row = conn.execute("SELECT status FROM application WHERE id = ?",
                       (application_id,)).fetchone()
    if row is None or row["status"] != "submitted":
        return HTMLResponse('<span class="denied">Mark the job applied'
                            ' before recording what came back.</span>')

    errors: dict[str, str] = {}
    day = _parse_date(occurred_at, "occurred_at", errors)
    if errors:
        return HTMLResponse(
            f'<span class="denied">{escape(errors["occurred_at"])}</span>')

    try:
        outcomes.record(conn, application_id, type, day,
                        notes=notes.strip() or None)
    except ValueError as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')
    return HTMLResponse('<span class="done">Recorded</span>')
```

Add `import datetime as dt` to the imports.

Note the parameter is named `type` to match the form field name; it
shadows the builtin only inside this function, which is the smaller evil
against renaming the form field to something the template must then
remember.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -v`
Expected: PASS, all tests including the pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py tests/test_web.py
git commit -m "feat: add mark-applied and record-outcome routes"
```

---

## Task 4: The controls on the Applications page

**Files:**
- Modify: `src/career_agent/web/app.py` (the `/applications` route's context)
- Modify: `src/career_agent/web/templates/applications.html`
- Modify: `src/career_agent/web/templates/base.html` (CSS only)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: the routes from Task 3; `outcomes.effective_outcome` and
  `outcomes.MANUAL_TYPES` (existing / Task 2).
- Produces: the rendered controls. No new Python interfaces.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_all_applications_offers_mark_applied_for_an_untracked_job(client):
    r = client.get("/applications")
    assert 'hx-post="/applied/1"' in r.text
    assert "Mark applied" in r.text


def test_all_applications_offers_outcome_controls_once_submitted(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]

    r = client.get("/applications")
    assert f'hx-post="/outcome/{app_id}"' in r.text
    assert "Awaiting response" in r.text, "no outcome recorded yet"


def test_the_outcome_form_does_not_offer_no_response(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    r = client.get("/applications")
    outcome_form = r.text.split('hx-post="/outcome/')[1]
    assert 'value="screen"' in outcome_form
    assert 'value="no_response"' not in outcome_form, (
        "derived, never entered")


def test_a_recorded_outcome_is_shown(client):
    client.post("/applied/1", data={"when": "2026-08-20"})
    conn = db.connect(web.DB_PATH)
    app_id = conn.execute("SELECT id FROM application").fetchone()["id"]
    client.post(f"/outcome/{app_id}",
                data={"type": "interview", "occurred_at": "2026-08-21"})

    r = client.get("/applications")
    assert "Interview" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -k "all_applications or outcome_form or recorded_outcome" -v`
Expected: FAIL — the markup does not exist.

- [ ] **Step 3: Implement**

In `src/career_agent/web/app.py`'s `/applications` route, build a
per-job map of application id and current outcome. Add this just before
the `TemplateResponse` call and pass it in the context as `applied`:

```python
    # application id + current effective outcome per job, for the outcome
    # controls. One effective_outcome call per submitted row, matching what
    # overview.recent_outcomes already does and bounded by the page size.
    applied = {}
    for r in conn.execute(
            "SELECT id, job_id FROM application WHERE status = 'submitted'"):
        applied[r["job_id"]] = {
            "application_id": r["id"],
            "outcome": outcomes.effective_outcome(conn, r["id"]),
        }
```

Add `"applied": applied,` and `"manual_types": outcomes.MANUAL_TYPES,`
to the context dict.

In `applications.html`, replace the action cell of the **All
Applications** tab (the `{% if j["terminal_status"] ... %}` block) with:

```html
        {% set tracked = applied.get(j["id"]) %}
        {% if tracked %}
          <div class="outcome-cell">
            <span class="{{ 'done' if tracked['outcome'] else 'rationale' }}">
              {{ outcome_labels.get(tracked["outcome"], "Awaiting response") }}
            </span>
            <form hx-post="/outcome/{{ tracked['application_id'] }}"
                  hx-swap="outerHTML" class="outcome-form">
              <select name="type">
                {% for t in manual_types %}
                <option value="{{ t }}">{{ outcome_labels[t] }}</option>
                {% endfor %}
              </select>
              <input type="date" name="occurred_at" value="{{ today }}">
              <input type="text" name="notes" placeholder="notes (optional)">
              <button class="btn" type="submit">Record</button>
            </form>
          </div>
        {% elif j["terminal_status"] == "held_unknown" %}
          <span class="denied">Held — confirm manually, then clear it</span>
        {% elif j["terminal_status"] == "failed_permanent" %}
          <span class="denied">Failed permanently — see the event log</span>
        {% else %}
          <form hx-post="/applied/{{ j['id'] }}" hx-swap="outerHTML"
                class="outcome-form">
            <input type="date" name="when" value="{{ today }}">
            <button class="btn" type="submit"
                    title="You applied on the site yourself. This records it so callbacks can be tracked.">Mark applied</button>
          </form>
          <button hx-post="/dismiss/{{ j['id'] }}" hx-swap="outerHTML">Dismiss</button>
        {% endif %}
```

The `Send` button is removed here per the spec — it only ever reports
that submission is not implemented, and beside a real "Mark applied"
control it invites exactly the confusion this work exists to remove.

Add `outcome_labels` and `today` to the same context dict in `app.py`:

```python
                 "outcome_labels": overview.CALLBACK_LABELS,
                 "today": dt.date.today().isoformat(),
```

`overview.CALLBACK_LABELS` already maps every outcome type to a display
label (`screen` → "Response", `interview` → "Interview", and so on) and
is already imported for the Overview page.

Add to `base.html`'s `<style>` block:

```css
  .outcome-cell{display:grid;gap:6px}
  .outcome-form{display:flex;gap:5px;align-items:center;flex-wrap:wrap}
  .outcome-form select,.outcome-form input{border:1px solid var(--line);
       border-radius:7px;padding:5px 7px;font:inherit;font-size:11px}
  .outcome-form input[type=text]{min-width:120px}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS. The full suite takes ~3 minutes.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/applications.html src/career_agent/web/templates/base.html tests/test_web.py
git commit -m "feat: add mark-applied and outcome controls to Applications"
```

---

## Task 5: Manual verification

- [ ] **Step 1: Work against a copy of the real database**

```bash
mkdir -p data && cp <path-to-real>/data/career.db data/career.db
```

Never point the server at the user's live database for this.

- [ ] **Step 2: Start the dashboard**

`.venv/Scripts/python.exe -m uvicorn career_agent.web.app:app --port 8040`

- [ ] **Step 3: Mark a job applied**

On `/applications`, All Applications tab, find one of the two
`submit`-verdict jobs (Bluvin Solutions or Altimetrik). Click "Mark
applied" with today's date. Confirm the row switches to showing "Awaiting
response" plus the outcome form.

- [ ] **Step 4: Record an outcome**

Choose "Response", leave the date, add a note, click Record. Confirm the
label changes.

- [ ] **Step 5: Confirm the gate metric moved**

Open `/`. The Responses KPI and the Outcome Summary's callback rate must
be non-zero. This is the whole point: before this change
`callback_rate` returned `(0, 0)` and could not leave it.

- [ ] **Step 6: Clean up**

Stop the server, `rm -f data/career.db`, and confirm `git status` is
clean.
