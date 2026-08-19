# Overview Page + Pipeline Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the temporary `/` redirect with a real pipeline-analytics
Overview page (KPIs, funnel, source performance, score distribution, recent
discoveries/outcomes) and a Run Now button that triggers discover+score
in the background.

**Architecture:** A new `overview.py` module holds pure, directly-testable
query functions (no FastAPI dependency) that turn existing schema data into
the numbers the prototype mocked. A new `pipeline.py` module wraps the
existing `run.run_once` — completely unchanged — in `run_state` tracking, so
Run Now is a thin FastAPI endpoint plus one background `asyncio.create_task`,
mirroring the apply-worker's control-endpoint pattern from the sibling plan.

**Tech Stack:** FastAPI, Jinja2, htmx, sqlite3, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-19-application-dashboard-design.md`
(Revision 2) — this plan covers the "Overview page" and "The pipeline
runner" sections.

**Depends on:** `docs/superpowers/plans/2026-08-19-applications-apply-worker.md`
must land first — this plan reuses its `run_state`/`job.priority` schema
(Task 1), `worker.get_run_state`/`set_run_state` (Task 2), `base.html`'s
shared shell (Task 8), and replaces that plan's temporary `GET /` redirect
(Task 7) with the real Overview route.

## Global Constraints

- No new dependencies.
- `run.run_once` is reused exactly as-is, via a duck-typed args object
  (`db`, `brief`, `boards`, `max_score` attributes) — see
  `tests/test_run.py`'s `_Args` for the precedent. No changes to `run.py`.
- The pipeline run has no pause/resume — it's one unattended sweep. Only
  `status` transitions used: `idle` → `running` → `idle` (success) or
  `error` (failure).
- Every DB-writing helper commits before returning, matching the existing
  codebase convention.

---

## Task 1: `overview.py` — KPIs with 7-day sparklines

**Files:**
- Create: `src/career_agent/web/overview.py`
- Test: `tests/test_overview.py`

**Interfaces:**
- Consumes: `career_agent.outcomes.callback_rate` (existing,
  `outcomes.py:29`).
- Produces: `kpis(conn) -> dict` with keys `discovered, after_hard_filter,
  shortlisted, applied, responses, sparklines` (the last a dict of
  5 SVG-ready `points` strings, one per KPI, keyed the same way).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_overview.py`:

```python
import pytest

from career_agent import db
from career_agent.web import overview


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(conn, fp, source="linkedin", days_ago=0):
    return conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized, discovered_at)"
        " VALUES (?, ?, ?, 'Acme', 'acme', 'AI Engineer', 'aiengineer',"
        " datetime('now', ?))",
        (fp, source, fp, f"-{days_ago} days")).lastrowid


def _scored(conn, job_id, score=80, verdict="submit"):
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v1')",
        (job_id, score, verdict))


def _hard_skip(conn, job_id):
    conn.execute(
        "INSERT INTO assessment (job_id, stage, verdict, rationale, model,"
        " prompt_version) VALUES (?, 'hard', 'skip', 'reason', 'hardfilter', 'n/a')",
        (job_id,))


def test_kpis_discovered_counts_only_today(conn):
    _job(conn, "today", days_ago=0)
    _job(conn, "yesterday", days_ago=1)
    conn.commit()
    assert overview.kpis(conn)["discovered"] == 1


def test_kpis_after_hard_filter_excludes_hard_skips(conn):
    j1 = _job(conn, "survives")
    _scored(conn, j1)
    j2 = _job(conn, "fails-hard")
    _hard_skip(conn, j2)
    conn.commit()
    assert overview.kpis(conn)["after_hard_filter"] == 1


def test_kpis_shortlisted_excludes_scored_skips(conn):
    j1 = _job(conn, "submit-me")
    _scored(conn, j1, verdict="submit")
    j2 = _job(conn, "skip-me")
    _scored(conn, j2, verdict="skip")
    conn.commit()
    assert overview.kpis(conn)["shortlisted"] == 1


def test_kpis_applied_counts_submitted_today(conn):
    j = _job(conn, "applied-today")
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
                 (j,))
    conn.commit()
    assert overview.kpis(conn)["applied"] == 1


def test_kpis_responses_matches_callback_rate_numerator(conn):
    j = _job(conn, "with-response")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type) VALUES (?, 'screen')",
                 (app_id,))
    conn.commit()
    assert overview.kpis(conn)["responses"] == 1


def test_kpis_sparklines_have_seven_points_per_metric(conn):
    _job(conn, "a")
    conn.commit()
    sparks = overview.kpis(conn)["sparklines"]
    assert set(sparks) == {"discovered", "after_hard_filter", "shortlisted",
                           "applied", "responses"}
    for points in sparks.values():
        assert len(points.split()) == 7


def test_sparkline_points_flat_line_when_all_zero():
    assert overview.sparkline_points([0, 0, 0]) == "0,24 63,24"


def test_sparkline_points_scales_to_a_64_by_28_box():
    points = overview.sparkline_points([0, 5, 10])
    xs = [float(p.split(",")[0]) for p in points.split()]
    ys = [float(p.split(",")[1]) for p in points.split()]
    assert xs[0] == 0.0 and xs[-1] == 63.0
    assert min(ys) == 4.0 and max(ys) == 24.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_overview.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'career_agent.web.overview'`.

- [ ] **Step 3: Implement**

Create `src/career_agent/web/overview.py`:

```python
import datetime as dt
import sqlite3

from career_agent import outcomes

CALLBACK_LABELS = {"screen": "Response", "interview": "Interview",
                    "offer": "Offer", "rejected": "Rejected",
                    "no_response": "No Response"}


def _daily_counts(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    return {r["day"]: r["n"] for r in conn.execute(sql)}


def sparkline_values(conn: sqlite3.Connection, count_sql: str) -> list[int]:
    """count_sql must SELECT (day, n) grouped by an ISO date `day` column,
    covering at least the last 7 days. Zero-filled for days with no rows."""
    counts = _daily_counts(conn, count_sql)
    today = dt.date.today()
    return [counts.get(str(today - dt.timedelta(days=i)), 0)
            for i in range(6, -1, -1)]


def sparkline_points(values: list[int]) -> str:
    """SVG polyline points, scaled into a 64x28 box (matches the prototype's
    .spark svg viewBox="0 0 64 28")."""
    if not values or max(values) == 0:
        return "0,24 63,24"
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = 63 / (len(values) - 1) if len(values) > 1 else 0
    points = []
    for i, v in enumerate(values):
        x = round(i * step, 1)
        y = round(24 - ((v - lo) / span) * 20, 1)
        points.append(f"{x},{y}")
    return " ".join(points)


def kpis(conn: sqlite3.Connection) -> dict:
    discovered = conn.execute(
        "SELECT COUNT(*) n FROM job"
        " WHERE date(discovered_at) = date('now')").fetchone()["n"]

    after_hard_filter = conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE date(j.discovered_at) = date('now')"
        "   AND a.stage = 'scored'").fetchone()["n"]

    shortlisted = conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE date(j.discovered_at) = date('now') AND a.stage = 'scored'"
        "   AND a.verdict IN ('submit','hold')").fetchone()["n"]

    applied = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        "   AND date(submitted_at) = date('now')").fetchone()["n"]

    responses, _total_submitted = outcomes.callback_rate(conn)

    sparklines = {
        "discovered": sparkline_points(sparkline_values(conn,
            "SELECT date(discovered_at) day, COUNT(*) n FROM job"
            " WHERE discovered_at >= date('now', '-6 days') GROUP BY day")),
        "after_hard_filter": sparkline_points(sparkline_values(conn,
            "SELECT date(j.discovered_at) day, COUNT(*) n FROM job j"
            " JOIN assessment a ON a.job_id = j.id WHERE a.stage = 'scored'"
            "   AND j.discovered_at >= date('now', '-6 days') GROUP BY day")),
        "shortlisted": sparkline_points(sparkline_values(conn,
            "SELECT date(j.discovered_at) day, COUNT(*) n FROM job j"
            " JOIN assessment a ON a.job_id = j.id WHERE a.stage = 'scored'"
            "   AND a.verdict IN ('submit','hold')"
            "   AND j.discovered_at >= date('now', '-6 days') GROUP BY day")),
        "applied": sparkline_points(sparkline_values(conn,
            "SELECT date(submitted_at) day, COUNT(*) n FROM application"
            " WHERE status = 'submitted'"
            "   AND submitted_at >= date('now', '-6 days') GROUP BY day")),
        "responses": sparkline_points(sparkline_values(conn,
            "SELECT date(occurred_at) day, COUNT(*) n FROM outcome"
            " WHERE type IN ('screen','interview','offer')"
            "   AND occurred_at >= date('now', '-6 days') GROUP BY day")),
    }

    return {"discovered": discovered, "after_hard_filter": after_hard_filter,
            "shortlisted": shortlisted, "applied": applied,
            "responses": responses, "sparklines": sparklines}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_overview.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/overview.py tests/test_overview.py
git commit -m "feat: add Overview KPI queries with 7-day sparklines"
```

---

## Task 2: `overview.py` — outcome summary and score distribution

**Files:**
- Modify: `src/career_agent/web/overview.py`
- Test: `tests/test_overview.py`

**Interfaces:**
- Consumes: `outcomes.effective_outcome`, `outcomes.CALLBACK_TYPES`
  (existing, `outcomes.py:20`, `outcomes.py:3`).
- Produces: `outcome_summary(conn) -> dict` (keys `applied, responses,
  interviews, offers, callback_rate, interview_rate`),
  `score_distribution(conn) -> dict` (keys `total, high, good, fair, low`
  — the four bucket keys are percentages, not counts).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_overview.py`:

```python
def test_outcome_summary_counts_this_month(conn):
    j = _job(conn, "this-month")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted',"
        " datetime('now', 'start of month', '+1 day'))", (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type)"
                 " VALUES (?, 'interview')", (app_id,))
    conn.commit()
    summary = overview.outcome_summary(conn)
    assert summary["applied"] == 1
    assert summary["responses"] == 1
    assert summary["interviews"] == 1
    assert summary["offers"] == 0
    assert summary["callback_rate"] == 100.0
    assert summary["interview_rate"] == 100.0


def test_outcome_summary_uses_effective_outcome_not_raw_rows(conn):
    """A derived no_response should not count as a response once a real
    outcome supersedes it — outcome_summary must go through
    effective_outcome(), not just check whether any callback-type row
    exists."""
    j = _job(conn, "superseded")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type, derived,"
                 " occurred_at) VALUES (?, 'screen', 0, datetime('now', '-1 day'))",
                 (app_id,))
    conn.execute("INSERT INTO outcome (application_id, type, derived,"
                 " occurred_at) VALUES (?, 'rejected', 0, datetime('now'))",
                 (app_id,))
    conn.commit()
    summary = overview.outcome_summary(conn)
    assert summary["responses"] == 0  # latest outcome is 'rejected', not a callback


def test_outcome_summary_zero_applications_avoids_division_by_zero(conn):
    summary = overview.outcome_summary(conn)
    assert summary == {"applied": 0, "responses": 0, "interviews": 0,
                        "offers": 0, "callback_rate": 0.0,
                        "interview_rate": 0.0}


def test_score_distribution_buckets(conn):
    j1 = _job(conn, "high")
    _scored(conn, j1, score=90)
    j2 = _job(conn, "good")
    _scored(conn, j2, score=65)
    j3 = _job(conn, "fair")
    _scored(conn, j3, score=45)
    j4 = _job(conn, "low")
    _scored(conn, j4, score=10)
    conn.commit()
    dist = overview.score_distribution(conn)
    assert dist["total"] == 4
    assert dist["high"] == 25.0
    assert dist["good"] == 25.0
    assert dist["fair"] == 25.0
    assert dist["low"] == 25.0


def test_score_distribution_excludes_hard_skips(conn):
    j = _job(conn, "hard-skip")
    _hard_skip(conn, j)
    conn.commit()
    assert overview.score_distribution(conn)["total"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_overview.py -k "outcome_summary or score_distribution" -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

Add to `src/career_agent/web/overview.py`:

```python
def outcome_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id FROM application WHERE status = 'submitted'"
        "   AND date(submitted_at) >= date('now', 'start of month')").fetchall()
    applied = len(rows)
    effective = [outcomes.effective_outcome(conn, r["id"]) for r in rows]
    responses = sum(1 for o in effective if o in outcomes.CALLBACK_TYPES)
    interviews = sum(1 for o in effective if o in ("interview", "offer"))
    offers = sum(1 for o in effective if o == "offer")
    callback_rate = round(100 * responses / applied, 1) if applied else 0.0
    interview_rate = round(100 * interviews / applied, 1) if applied else 0.0
    return {"applied": applied, "responses": responses, "interviews": interviews,
            "offers": offers, "callback_rate": callback_rate,
            "interview_rate": interview_rate}


def score_distribution(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT weighted_score FROM assessment WHERE stage = 'scored'"
        "   AND weighted_score IS NOT NULL").fetchall()
    total = len(rows)
    if total == 0:
        return {"total": 0, "high": 0, "good": 0, "fair": 0, "low": 0}
    buckets = {"high": 0, "good": 0, "fair": 0, "low": 0}
    for r in rows:
        s = r["weighted_score"]
        if s >= 80:
            buckets["high"] += 1
        elif s >= 60:
            buckets["good"] += 1
        elif s >= 40:
            buckets["fair"] += 1
        else:
            buckets["low"] += 1
    return {"total": total,
            **{k: round(100 * v / total, 1) for k, v in buckets.items()}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_overview.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/overview.py tests/test_overview.py
git commit -m "feat: add outcome summary and score distribution queries"
```

---

## Task 3: `overview.py` — source performance, recent discoveries, recent outcomes

**Files:**
- Modify: `src/career_agent/web/overview.py`
- Test: `tests/test_overview.py`

**Interfaces:**
- Produces: `source_performance(conn) -> list[dict]` (keys `source,
  discovered, pass_rate, shortlist_rate, applied, response_rate`),
  `recent_discoveries(conn, limit=10) -> list[dict]` (job fields plus
  `gate` — one of `"pass"`, `"review"`, `"fail"`, `"pending"`),
  `recent_outcomes(conn, limit=10) -> list[dict]` (keys `title, company,
  label, submitted_at`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_overview.py`:

```python
def test_source_performance_groups_by_source(conn):
    j1 = _job(conn, "li-1", source="linkedin")
    _scored(conn, j1, verdict="submit")
    j2 = _job(conn, "nk-1", source="naukri")
    _hard_skip(conn, j2)
    conn.commit()
    rows = {r["source"]: r for r in overview.source_performance(conn)}
    assert rows["linkedin"]["discovered"] == 1
    assert rows["linkedin"]["pass_rate"] == 100.0
    assert rows["naukri"]["pass_rate"] == 0.0


def test_source_performance_response_rate(conn):
    j = _job(conn, "li-2", source="linkedin")
    _scored(conn, j, verdict="submit")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type)"
                 " VALUES (?, 'screen')", (app_id,))
    conn.commit()
    rows = {r["source"]: r for r in overview.source_performance(conn)}
    assert rows["linkedin"]["applied"] == 1
    assert rows["linkedin"]["response_rate"] == 100.0


def test_recent_discoveries_gate_labels(conn):
    j1 = _job(conn, "passed")
    _scored(conn, j1, verdict="submit")
    j2 = _job(conn, "reviewed")
    _scored(conn, j2, verdict="skip")
    j3 = _job(conn, "failed")
    _hard_skip(conn, j3)
    conn.commit()
    gates = {d["title"] + d["source"] + str(d["id"]): d["gate"]
             for d in overview.recent_discoveries(conn)}
    by_id = {d["id"]: d["gate"] for d in overview.recent_discoveries(conn)}
    assert by_id[j1] == "pass"
    assert by_id[j2] == "review"
    assert by_id[j3] == "fail"


def test_recent_discoveries_respects_limit(conn):
    for i in range(15):
        _job(conn, f"j{i}")
    conn.commit()
    assert len(overview.recent_discoveries(conn, limit=5)) == 5


def test_recent_outcomes_labels_applied_when_no_outcome_yet(conn):
    j = _job(conn, "just-applied")
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
                 (j,))
    conn.commit()
    outcomes_list = overview.recent_outcomes(conn)
    assert outcomes_list[0]["label"] == "Applied"


def test_recent_outcomes_labels_interview(conn):
    j = _job(conn, "interviewing")
    app_id = conn.execute(
        "INSERT INTO application (job_id, resume_version, status,"
        " submitted_at) VALUES (?, 'v1', 'submitted', datetime('now'))",
        (j,)).lastrowid
    conn.execute("INSERT INTO outcome (application_id, type)"
                 " VALUES (?, 'interview')", (app_id,))
    conn.commit()
    outcomes_list = overview.recent_outcomes(conn)
    assert outcomes_list[0]["label"] == "Interview"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_overview.py -k "source_performance or recent_" -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

Add to `src/career_agent/web/overview.py`:

```python
def source_performance(conn: sqlite3.Connection) -> list[dict]:
    # ponytail: response rate here treats "any callback-type outcome row
    # exists" as a response, skipping the derived-vs-manual tie-break
    # outcomes.effective_outcome() applies. Fine for a per-source
    # aggregate; upgrade to effective_outcome() per application if this
    # ever needs to match outcome_summary()'s numbers exactly.
    rows = conn.execute(
        "SELECT j.source,"
        "       COUNT(DISTINCT j.id) discovered,"
        "       COUNT(DISTINCT CASE WHEN a.stage='scored' THEN j.id END)"
        "         after_hard_filter,"
        "       COUNT(DISTINCT CASE WHEN a.stage='scored' AND a.verdict IN"
        "             ('submit','hold') THEN j.id END) shortlisted,"
        "       COUNT(DISTINCT ap.id) applied,"
        "       COUNT(DISTINCT CASE WHEN o.type IN"
        "             ('screen','interview','offer') THEN ap.id END) responded"
        "  FROM job j"
        "  LEFT JOIN assessment a ON a.job_id = j.id"
        "  LEFT JOIN application ap ON ap.job_id = j.id AND ap.status = 'submitted'"
        "  LEFT JOIN outcome o ON o.application_id = ap.id"
        " WHERE j.merged_into_job_id IS NULL"
        " GROUP BY j.source"
        " ORDER BY discovered DESC").fetchall()
    result = []
    for r in rows:
        pass_rate = (round(100 * r["after_hard_filter"] / r["discovered"], 1)
                     if r["discovered"] else 0.0)
        shortlist_rate = (round(100 * r["shortlisted"] / r["after_hard_filter"], 1)
                          if r["after_hard_filter"] else 0.0)
        response_rate = (round(100 * r["responded"] / r["applied"], 1)
                         if r["applied"] else 0.0)
        result.append({"source": r["source"], "discovered": r["discovered"],
                        "pass_rate": pass_rate, "shortlist_rate": shortlist_rate,
                        "applied": r["applied"], "response_rate": response_rate})
    return result


def recent_discoveries(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT j.id, j.title, j.company, j.source, j.location, j.url,"
        "       a.stage, a.verdict, a.weighted_score"
        "  FROM job j LEFT JOIN assessment a ON a.job_id = j.id"
        " WHERE j.merged_into_job_id IS NULL"
        " ORDER BY j.discovered_at DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for r in rows:
        if r["stage"] == "hard":
            gate = "fail"
        elif r["stage"] == "scored" and r["verdict"] == "skip":
            gate = "review"
        elif r["stage"] == "scored":
            gate = "pass"
        else:
            gate = "pending"
        result.append({**dict(r), "gate": gate})
    return result


def recent_outcomes(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT ap.id AS application_id, j.title, j.company, ap.submitted_at"
        "  FROM application ap JOIN job j ON j.id = ap.job_id"
        " WHERE ap.status = 'submitted'"
        " ORDER BY ap.submitted_at DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for r in rows:
        effective = outcomes.effective_outcome(conn, r["application_id"])
        label = CALLBACK_LABELS.get(effective, "Applied")
        result.append({"title": r["title"], "company": r["company"],
                        "label": label, "submitted_at": r["submitted_at"]})
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_overview.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/overview.py tests/test_overview.py
git commit -m "feat: add source performance, recent discoveries and outcomes queries"
```

---

## Task 4: `pipeline.py` — the background pipeline runner

**Files:**
- Create: `src/career_agent/web/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `career_agent.run.run_once` (existing, `run.py:50`, called with
  a duck-typed args object — see `tests/test_run.py`'s `_Args`),
  `worker.set_run_state` (sibling plan, `worker.py`), `store.log`.
- Produces: `async def run_background(conn_factory, db_path, brief_path,
  boards_path="ats_boards.toml", max_score=25) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline.py`:

```python
import pytest

from career_agent import db
from career_agent.web import pipeline, worker


@pytest.fixture
def conn_factory(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.init_schema(c)

    def factory():
        return db.connect(path)
    return factory


async def test_run_background_sets_idle_and_logs_completion_on_success(
        conn_factory, monkeypatch, tmp_path):
    async def fake_run_once(args):
        pass

    monkeypatch.setattr(pipeline.run_module, "run_once", fake_run_once)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    state = worker.get_run_state(conn, "apply")  # sanity: apply row untouched
    assert state["status"] == "idle"
    pstate = worker.get_run_state(conn, "pipeline")
    assert pstate["status"] == "idle"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "pipeline_completed" in types


async def test_run_background_sets_error_and_logs_on_failure(
        conn_factory, monkeypatch, tmp_path):
    async def boom(args):
        raise RuntimeError("apify token missing")

    monkeypatch.setattr(pipeline.run_module, "run_once", boom)
    await pipeline.run_background(conn_factory, tmp_path / "t.db",
                                  tmp_path / "career_brief.toml")

    conn = conn_factory()
    pstate = worker.get_run_state(conn, "pipeline")
    assert pstate["status"] == "error"
    assert "apify token missing" in pstate["last_error"]
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "pipeline_error" in types


async def test_run_background_passes_paths_and_defaults_through(
        conn_factory, monkeypatch, tmp_path):
    seen = {}

    async def spy(args):
        seen["db"] = args.db
        seen["brief"] = args.brief
        seen["boards"] = args.boards
        seen["max_score"] = args.max_score

    monkeypatch.setattr(pipeline.run_module, "run_once", spy)
    db_path = tmp_path / "t.db"
    brief_path = tmp_path / "career_brief.toml"
    await pipeline.run_background(conn_factory, db_path, brief_path)

    assert seen["db"] == str(db_path)
    assert seen["brief"] == str(brief_path)
    assert seen["boards"] == "ats_boards.toml"
    assert seen["max_score"] == 25
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'career_agent.web.pipeline'`.

- [ ] **Step 3: Implement**

Create `src/career_agent/web/pipeline.py`:

```python
from types import SimpleNamespace

from career_agent import run as run_module
from career_agent import store
from career_agent.web import worker


async def run_background(conn_factory, db_path, brief_path,
                         boards_path: str = "ats_boards.toml",
                         max_score: int = 25) -> None:
    """Runs the existing discover+hard-filter+score pipeline
    (run.run_once, unchanged) in the background, tracking progress in the
    'pipeline' row of run_state. The caller (the /pipeline/run-now
    endpoint) is responsible for flipping status to 'running' and logging
    'pipeline_started' synchronously before scheduling this — this
    function only handles the outcome, success or failure."""
    args = SimpleNamespace(db=str(db_path), brief=str(brief_path),
                           boards=boards_path, max_score=max_score)
    try:
        await run_module.run_once(args)
    except Exception as exc:
        conn = conn_factory()
        worker.set_run_state(conn, "pipeline", status="error", last_error=str(exc))
        store.log(conn, None, "pipeline_error", str(exc))
        return

    conn = conn_factory()
    worker.set_run_state(conn, "pipeline", status="idle", last_error=None)
    store.log(conn, None, "pipeline_completed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pipeline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/pipeline.py tests/test_pipeline.py
git commit -m "feat: add the background pipeline runner"
```

---

## Task 5: `POST /pipeline/run-now` and `GET /pipeline/status`

**Files:**
- Modify: `src/career_agent/web/app.py`
- Create: `src/career_agent/web/templates/_pipeline_status.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `pipeline.run_background`, `worker.get_run_state`,
  `worker.set_run_state` (Task 4, sibling plan).
- Produces: routes `POST /pipeline/run-now`, `GET /pipeline/status`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py` (add `from career_agent.web import pipeline` to
the imports):

```python
def test_pipeline_run_now_flips_status_and_logs_synchronously(client, monkeypatch):
    async def never_finishes(args):
        import asyncio
        await asyncio.sleep(3600)

    monkeypatch.setattr(pipeline.run_module, "run_once", never_finishes)
    r = client.post("/pipeline/run-now")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    assert worker.get_run_state(conn, "pipeline")["status"] == "running"
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "pipeline_started" in types


def test_pipeline_run_now_refuses_a_second_concurrent_run(client, monkeypatch):
    async def never_finishes(args):
        import asyncio
        await asyncio.sleep(3600)

    monkeypatch.setattr(pipeline.run_module, "run_once", never_finishes)
    client.post("/pipeline/run-now")
    r = client.post("/pipeline/run-now")
    assert "already" in r.text.lower()


def test_pipeline_status_shows_run_now_button_when_idle(client):
    r = client.get("/pipeline/status")
    assert r.status_code == 200
    assert 'hx-post="/pipeline/run-now"' in r.text
    assert "Run Now" in r.text


def test_pipeline_status_shows_running_state(client, monkeypatch):
    async def never_finishes(args):
        import asyncio
        await asyncio.sleep(3600)

    monkeypatch.setattr(pipeline.run_module, "run_once", never_finishes)
    client.post("/pipeline/run-now")
    r = client.get("/pipeline/status")
    assert "Running" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k pipeline -v`
Expected: FAIL — 404s.

- [ ] **Step 3: Implement**

Create `src/career_agent/web/templates/_pipeline_status.html`:

```html
<div id="pipeline-status">
  {% if pipeline_state["status"] in ("idle", "error") %}
    <button class="btn primary" hx-post="/pipeline/run-now"
            hx-target="#pipeline-status" hx-swap="outerHTML">▶ Run Now</button>
  {% else %}
    <button class="btn" disabled>◌ Running…</button>
  {% endif %}
  <span class="pill status-{{ pipeline_state["status"] }}">
    {{ pipeline_state["status"] | capitalize }}</span>
  <span class="run-meta">Last update: {{ pipeline_state["updated_at"] }}</span>
  {% if pipeline_state["last_error"] %}
    <span class="denied">{{ pipeline_state["last_error"] }}</span>
  {% endif %}
</div>
```

Add to `src/career_agent/web/app.py` (add `import asyncio` if Task 6 of
the sibling plan hasn't already, and
`from career_agent.web import pipeline` to the imports):

```python
@app.post("/pipeline/run-now")
def pipeline_run_now():
    conn = _conn()
    state = worker.get_run_state(conn, "pipeline")
    if state["status"] not in ("idle", "error"):
        return HTMLResponse(
            '<span class="denied">A pipeline run is already in progress.</span>')
    worker.set_run_state(conn, "pipeline", status="running", last_error=None)
    store.log(conn, None, "pipeline_started")
    asyncio.create_task(
        pipeline.run_background(_conn, DB_PATH, BRIEF_PATH))
    return HTMLResponse("ok")


@app.get("/pipeline/status", response_class=HTMLResponse)
def pipeline_status(request: Request):
    conn = _conn()
    return templates.TemplateResponse(
        request=request, name="_pipeline_status.html",
        context={"pipeline_state": worker.get_run_state(conn, "pipeline")})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/_pipeline_status.html tests/test_web.py
git commit -m "feat: add pipeline run-now endpoint and status fragment"
```

---

## Task 6: The Overview page — replaces the `/` redirect

**Files:**
- Modify: `src/career_agent/web/app.py`
- Create: `src/career_agent/web/templates/overview.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3 (`overview.kpis`,
  `overview.outcome_summary`, `overview.score_distribution`,
  `overview.source_performance`, `overview.recent_discoveries`,
  `overview.recent_outcomes`), `worker.get_run_state`.
- Produces: `GET /` rendering the real Overview page (replaces the sibling
  plan's temporary redirect).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py` (add `from career_agent.web import overview` to
the imports):

```python
def test_root_renders_overview_not_a_redirect(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_overview_shows_kpi_cards(client):
    r = client.get("/")
    assert "Discovered" in r.text
    assert "Shortlisted" in r.text


def test_overview_shows_recent_discoveries_from_fixture(client):
    r = client.get("/")
    assert "AI Engineer" in r.text  # job 1 from the client fixture
    assert "Acme" in r.text


def test_overview_ready_to_apply_cta_links_to_applications(client):
    r = client.get("/")
    assert 'href="/applications"' in r.text


def test_overview_embeds_pipeline_status_polling(client):
    r = client.get("/")
    assert 'hx-get="/pipeline/status"' in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k overview -v`
Expected: FAIL — `test_root_renders_overview_not_a_redirect` gets a 307,
the others 404/missing content, since `/` is still the sibling plan's
redirect and `overview.html` doesn't exist.

- [ ] **Step 3: Implement**

In `src/career_agent/web/app.py`, delete the placeholder `root()` redirect
function the sibling plan added (`@app.get("/") def root(): return
RedirectResponse(...)`) and the now-unused `RedirectResponse` import if
nothing else uses it, replacing it with:

```python
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    brief = load_brief(BRIEF_PATH)
    today_submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    kpi_data = overview.kpis(conn)
    return templates.TemplateResponse(
        request=request, name="overview.html",
        context={"active_nav": "dashboard", "brief": brief,
                 "daily_cap": brief.daily_cap,
                 "today_submitted": today_submitted,
                 "kpis": kpi_data,
                 "outcome_summary": overview.outcome_summary(conn),
                 "source_performance": overview.source_performance(conn),
                 "score_distribution": overview.score_distribution(conn),
                 "recent_discoveries": overview.recent_discoveries(conn),
                 "recent_outcomes": overview.recent_outcomes(conn),
                 "shortlisted_count": kpi_data["shortlisted"],
                 "pipeline_state": worker.get_run_state(conn, "pipeline")})
```

Add `from career_agent.web import overview` to `app.py`'s imports.

Create `src/career_agent/web/templates/overview.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Dashboard</h1>
<p class="rationale">Your daily job discovery and application command center.</p>

<div hx-get="/pipeline/status" hx-trigger="load, every 3s" hx-swap="outerHTML">
  <div id="pipeline-status">Loading…</div>
</div>

<div class="kpis">
  <div class="card kpi">
    <h3>Discovered</h3><div class="num">{{ kpis.discovered }}</div>
    <svg viewBox="0 0 64 28" width="64" height="28">
      <polyline points="{{ kpis.sparklines.discovered }}" fill="none" stroke="#5b36e8" stroke-width="2"/>
    </svg>
  </div>
  <div class="card kpi">
    <h3>After Hard Filter</h3><div class="num">{{ kpis.after_hard_filter }}</div>
    <svg viewBox="0 0 64 28" width="64" height="28">
      <polyline points="{{ kpis.sparklines.after_hard_filter }}" fill="none" stroke="#3f78df" stroke-width="2"/>
    </svg>
  </div>
  <div class="card kpi">
    <h3>Shortlisted</h3><div class="num">{{ kpis.shortlisted }}</div>
    <svg viewBox="0 0 64 28" width="64" height="28">
      <polyline points="{{ kpis.sparklines.shortlisted }}" fill="none" stroke="#22a665" stroke-width="2"/>
    </svg>
  </div>
  <div class="card kpi">
    <h3>Applied</h3><div class="num">{{ kpis.applied }}</div>
    <svg viewBox="0 0 64 28" width="64" height="28">
      <polyline points="{{ kpis.sparklines.applied }}" fill="none" stroke="#ee9412" stroke-width="2"/>
    </svg>
  </div>
  <div class="card kpi">
    <h3>Responses</h3><div class="num">{{ kpis.responses }}</div>
    <svg viewBox="0 0 64 28" width="64" height="28">
      <polyline points="{{ kpis.sparklines.responses }}" fill="none" stroke="#27a49a" stroke-width="2"/>
    </svg>
  </div>
</div>

<div class="card panel">
  <div class="panel-head"><h2>Pipeline Overview</h2></div>
  {% set stages = [("Discovered", kpis.discovered), ("After Hard Filter", kpis.after_hard_filter),
                    ("Shortlisted", kpis.shortlisted), ("Applied", kpis.applied),
                    ("Responses", kpis.responses)] %}
  {% set widest = kpis.discovered or 1 %}
  <div class="funnel">
    {% for label, value in stages %}
    <div style="width: {{ (value / widest * 100) | round | int }}%; min-width: 8%;">
      {{ label }}: {{ value }}</div>
    {% endfor %}
  </div>
</div>

<div class="card panel">
  <div class="panel-head"><h2>Recent Discoveries</h2></div>
  <table>
    <tr><th>Job Title</th><th>Company</th><th>Source</th><th>Score</th><th>Location</th><th>Gate</th></tr>
    {% for d in recent_discoveries %}
    <tr>
      <td><b>{{ d["title"] }}</b></td>
      <td>{{ d["company"] }}</td>
      <td>{{ d["source"] }}</td>
      <td>{{ (d["weighted_score"] or 0) | round | int if d["weighted_score"] else "—" }}</td>
      <td>{{ d["location"] or "—" }}</td>
      <td class="gate-{{ d['gate'] }}">{{ d["gate"] | capitalize }}</td>
    </tr>
    {% endfor %}
  </table>
</div>

<div class="bottom-grid" style="display:grid;grid-template-columns:1.7fr .85fr;gap:11px;margin-top:11px;">
  <div class="card panel">
    <div class="panel-head"><h2>Source Performance</h2></div>
    <table>
      <tr><th>Source</th><th>Discovered</th><th>Pass Rate</th><th>Shortlist Rate</th><th>Applied</th><th>Response Rate</th></tr>
      {% for s in source_performance %}
      <tr>
        <td><b>{{ s["source"] }}</b></td>
        <td>{{ s["discovered"] }}</td>
        <td>{{ s["pass_rate"] }}%</td>
        <td>{{ s["shortlist_rate"] }}%</td>
        <td>{{ s["applied"] }}</td>
        <td>{{ s["response_rate"] }}%</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  <div class="card panel">
    <div class="panel-head"><h2>Score Distribution</h2></div>
    {% if score_distribution.total == 0 %}
      <p class="rationale">No scored jobs yet.</p>
    {% else %}
      <div>80–100 (High): {{ score_distribution.high }}%</div>
      <div>60–79 (Good): {{ score_distribution.good }}%</div>
      <div>40–59 (Fair): {{ score_distribution.fair }}%</div>
      <div>0–39 (Low): {{ score_distribution.low }}%</div>
    {% endif %}
  </div>
</div>

<div class="bottom-grid" style="display:grid;grid-template-columns:1.7fr .85fr;gap:11px;margin-top:11px;">
  <div class="card panel">
    <div class="panel-head"><h2>Recent Outcomes</h2></div>
    {% if not recent_outcomes %}
      <p class="rationale">No applications submitted yet.</p>
    {% else %}
      {% for o in recent_outcomes %}
      <div class="recent-item" style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line);">
        <div><b>{{ o["title"] }}</b><br><span class="rationale">{{ o["company"] }}</span></div>
        <span class="pill">{{ o["label"] }}</span>
      </div>
      {% endfor %}
    {% endif %}
  </div>
  <div class="card panel">
    <div class="panel-head"><h2>Outcome Summary <span style="font-weight:600;color:var(--muted)">(This Month)</span></h2></div>
    <div>Applied: {{ outcome_summary.applied }}</div>
    <div>Responses: {{ outcome_summary.responses }}</div>
    <div>Interviews: {{ outcome_summary.interviews }}</div>
    <div>Offers: {{ outcome_summary.offers }}</div>
    <div>Callback Rate: {{ outcome_summary.callback_rate }}%</div>
    <div>Interview Rate: {{ outcome_summary.interview_rate }}%</div>
  </div>
</div>

<div class="card" style="margin-top:11px;padding:16px;display:flex;justify-content:space-between;align-items:center;">
  <div>
    <h3 style="margin:0 0 4px">Ready to apply?</h3>
    <p style="margin:0;color:var(--muted)">You have <b>{{ shortlisted_count }} shortlisted jobs</b>.
      Review and apply from the Applications page.</p>
  </div>
  <a class="btn primary" href="/applications">View Shortlisted Jobs →</a>
</div>
{% endblock %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests including every pre-existing one from the
sibling plan.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/overview.html tests/test_web.py
git commit -m "feat: add the Overview page, replacing the / redirect"
```

---

## Task 7: Manual verification

- [ ] **Step 1: Start the dashboard**

Use the `run` skill (or `career-agent serve`) and open
`http://localhost:8000/`.

- [ ] **Step 2: Confirm the Overview page renders real data**

Confirm the KPI cards, funnel, source performance table, score
distribution, recent discoveries, and recent outcomes all show real
numbers from `data/career.db` (not mocked values) — compare a couple
against a manual `sqlite3 data/career.db "SELECT ..."` query to sanity
check.

- [ ] **Step 3: Drive Run Now**

Click Run Now. Confirm the button becomes disabled with "Running…", the
status pill updates, and — once discovery/scoring actually completes (or
errors, if `APIFY_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN` aren't configured in
this environment) — the button returns to "Run Now" or shows the error
message, and the KPI numbers reflect any newly-discovered jobs after a
page refresh.

- [ ] **Step 4: Confirm the CTA and nav round-trip**

Click "View Shortlisted Jobs" and confirm it lands on `/applications`
showing the same shortlisted count the Overview page reported. Click
"Dashboard" in the sidebar from there and confirm it returns to `/`.

- [ ] **Step 5: Report results**

Note any visual issues or numbers that look wrong given the real database
contents — fix inline if small, or flag as a follow-up if they require
design decisions beyond this plan's scope.
