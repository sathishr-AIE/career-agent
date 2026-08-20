# Settings Screen + Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the scoring model an explicit choice (Sonnet or Haiku), make
the agent's configuration editable from a Settings screen, and fix the
scoring budget so `max_score` caps model calls rather than rows examined.

**Architecture:** Operational knobs (scoring model, jobs per run) live in a
new single-row `setting` table read by `run.run_once`; career-brief fields
keep living in `career_brief.toml` and are written back comment-preserved
via `tomlkit`. One Settings page posts both, validating everything through
the existing `CareerBrief` pydantic model before writing anything.

**Tech Stack:** FastAPI, Jinja2, htmx, sqlite3, pydantic, tomlkit (new),
pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-20-settings-and-model-selection-design.md`

## Global Constraints

- **`career_brief` must never become a table.** `tests/test_db.py::test_no_career_brief_table` enforces the v1 decision and must keep passing.
- `tomlkit` is the only new dependency. Nothing else is added.
- The gate prompt, its five dimensions, weights, and verdict rules are unchanged. `gate.py` is not modified by any task in this plan.
- Settings changes never trigger a re-score. `gate.PROMPT_VERSION` remains the only re-score trigger.
- Allowed model ids are validated in Python against `config.SCORING_MODELS`, never by a SQL CHECK constraint.
- Validation order on save: validate brief AND settings first; then write the TOML; then the DB row. A failure at any point must leave both stores untouched.
- **Tests that POST to `/settings` MUST monkeypatch `web.BRIEF_PATH` to a tmp file.** Without it a test overwrites the repository's real `career_brief.toml`.

---

## Task 1: `setting` table, model constants, and store helpers

**Files:**
- Modify: `src/career_agent/db.py`
- Modify: `src/career_agent/config.py`
- Modify: `src/career_agent/store.py`
- Test: `tests/test_db.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `config.SCORING_MODELS` (tuple), `config.DEFAULT_SCORING_MODEL`, `config.MODEL_LABELS` (dict for the UI); `store.get_settings(conn) -> sqlite3.Row`; `store.save_settings(conn, scoring_model: str, max_score_per_run: int) -> None`; the `setting` table seeded with one row.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
def test_setting_row_is_seeded(conn):
    row = conn.execute("SELECT * FROM setting").fetchone()
    assert row["scoring_model"] == "claude-sonnet-5"
    assert row["max_score_per_run"] == 25


def test_setting_seeding_does_not_clobber_a_saved_choice(conn):
    """init_schema runs on every web request; re-seeding must not reset
    the user's model choice back to the default."""
    conn.execute("UPDATE setting SET scoring_model = 'claude-haiku-4-5-20251001',"
                 " max_score_per_run = 50")
    conn.commit()
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) n FROM setting").fetchone()["n"] == 1
    row = conn.execute("SELECT * FROM setting").fetchone()
    assert row["scoring_model"] == "claude-haiku-4-5-20251001"
    assert row["max_score_per_run"] == 50
```

Add to `tests/test_store.py`:

```python
def test_settings_default_to_sonnet_and_25(conn):
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-sonnet-5"
    assert s["max_score_per_run"] == 25


def test_save_settings_round_trips(conn):
    store.save_settings(conn, "claude-haiku-4-5-20251001", 50)
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-haiku-4-5-20251001"
    assert s["max_score_per_run"] == 50


def test_save_settings_rejects_an_unknown_model(conn):
    with pytest.raises(ValueError, match="unknown scoring model"):
        store.save_settings(conn, "gpt-4", 25)
    assert store.get_settings(conn)["scoring_model"] == "claude-sonnet-5"


def test_save_settings_rejects_a_negative_cap(conn):
    with pytest.raises(ValueError):
        store.save_settings(conn, "claude-sonnet-5", -1)
    assert store.get_settings(conn)["max_score_per_run"] == 25


def test_save_settings_allows_zero_cap(conn):
    """0 is meaningful: discovery and the hard filter run, scoring does not."""
    store.save_settings(conn, "claude-sonnet-5", 0)
    assert store.get_settings(conn)["max_score_per_run"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py tests/test_store.py -k "setting" -v`
Expected: FAIL — `no such table: setting`, and `AttributeError: module 'career_agent.store' has no attribute 'get_settings'`.

- [ ] **Step 3: Implement**

In `src/career_agent/db.py`, add to the `SCHEMA` string (after the
`run_state` table, before `fact`):

```sql
CREATE TABLE IF NOT EXISTS setting (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    scoring_model     TEXT NOT NULL DEFAULT 'claude-sonnet-5',
    max_score_per_run INTEGER NOT NULL DEFAULT 25
                        CHECK (max_score_per_run >= 0),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
```

In `init_schema`, add one line beside the existing `run_state` seeds:

```python
    conn.execute("INSERT OR IGNORE INTO setting (id) VALUES (1)")
```

In `src/career_agent/config.py`, add at module level (after the imports,
before `CareerBrief`):

```python
# Operational, not part of the career brief. Validated in Python rather than
# by a CHECK constraint so retiring or adding a model is a constant edit, not
# a schema migration.
SCORING_MODELS = ("claude-sonnet-5", "claude-haiku-4-5-20251001")
DEFAULT_SCORING_MODEL = SCORING_MODELS[0]

MODEL_LABELS = {
    "claude-sonnet-5": "Sonnet — better judgement (default)",
    "claude-haiku-4-5-20251001": "Haiku — faster, lighter on rate limits",
}
```

In `src/career_agent/store.py`, change the existing config import to
`from career_agent.config import SCORING_MODELS, CareerBrief` and append:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py tests/test_store.py -v`
Expected: PASS, including the pre-existing `test_no_career_brief_table`.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/db.py src/career_agent/config.py src/career_agent/store.py tests/test_db.py tests/test_store.py
git commit -m "feat: add the setting table and scoring-model constants"
```

---

## Task 2: `config.save_brief` — comment-preserving TOML write-back

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/career_agent/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `config.CareerBrief`, `config.load_brief` (both existing).
- Produces: `config.save_brief(path: Path, brief: CareerBrief) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py` (add `from pathlib import Path` and
`from career_agent.config import CareerBrief, save_brief` to the imports):

```python
COMMENTED_BRIEF = """\
# Single source of truth for search and filter settings.
target_titles = ["AI Engineer"]

# What we ASK each source for. Adding a city multiplies daily Actor runs.
search_locations = ["Chennai"]
locations = ["Chennai", "Remote"]
salary_floor_inr = 1200000
daily_cap = 5
"""


def test_save_brief_preserves_comments(tmp_path):
    p = tmp_path / "brief.toml"
    p.write_text(COMMENTED_BRIEF, encoding="utf-8")
    brief = load_brief(p)
    brief.daily_cap = 9
    save_brief(p, brief)
    text = p.read_text(encoding="utf-8")
    assert "# Single source of truth for search and filter settings." in text
    assert "# What we ASK each source for." in text
    assert load_brief(p).daily_cap == 9


def test_save_brief_round_trips_list_fields(tmp_path):
    p = tmp_path / "brief.toml"
    p.write_text(COMMENTED_BRIEF, encoding="utf-8")
    brief = load_brief(p)
    brief.target_titles = ["AI Engineer", "ML Engineer"]
    brief.excluded_companies = ["BadCo"]
    save_brief(p, brief)
    reloaded = load_brief(p)
    assert reloaded.target_titles == ["AI Engineer", "ML Engineer"]
    assert reloaded.excluded_companies == ["BadCo"]


def test_save_brief_omits_an_unset_salary_floor(tmp_path):
    """TOML has no null. An unset floor must be ABSENT, not 0 -- 0 would
    mean 'reject nothing', which is a different statement."""
    p = tmp_path / "brief.toml"
    p.write_text(COMMENTED_BRIEF, encoding="utf-8")
    brief = load_brief(p)
    brief.salary_floor_inr = None
    save_brief(p, brief)
    assert "salary_floor_inr" not in p.read_text(encoding="utf-8")
    assert load_brief(p).salary_floor_inr is None


def test_save_brief_creates_a_file_that_does_not_exist(tmp_path):
    p = tmp_path / "new.toml"
    save_brief(p, CareerBrief(target_titles=["AI Engineer"],
                              search_locations=["Chennai"]))
    assert load_brief(p).target_titles == ["AI Engineer"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -k save_brief -v`
Expected: FAIL — `ImportError: cannot import name 'save_brief'`.

- [ ] **Step 3: Implement**

Add `"tomlkit"` to the `dependencies` list in `pyproject.toml`, then
install it: `uv pip install -e ".[dev]" --python .venv`.

In `src/career_agent/config.py`, add `import tomlkit` beside `import
tomllib` and append:

```python
def save_brief(path: Path, brief: CareerBrief) -> None:
    """Write the brief back preserving comments, key order, and formatting.
    The file is version controlled and its comments explain non-obvious
    consequences ("adding a city multiplies daily Actor runs"), so a
    round-trip write is the only acceptable kind."""
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    for field, value in brief.model_dump().items():
        if value is None:
            # TOML has no null; absent is how "unset" is spelled, and
            # load_brief will fall back to the pydantic default.
            doc.pop(field, None)
        else:
            doc[field] = value

    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/career_agent/config.py tests/test_config.py
git commit -m "feat: add comment-preserving save_brief via tomlkit"
```

---

## Task 3: the scoring budget fix

**Files:**
- Modify: `src/career_agent/store.py`
- Modify: `src/career_agent/run.py`
- Test: `tests/test_run.py`

**Interfaces:**
- Produces: `store.unscored_jobs(conn, prompt_version, limit: int | None = None)` — `limit=None` returns the whole pool.
- `run.run_once` hard-filters the entire pool every run and caps only the model calls.

This is the bug that makes the `max_score_per_run` setting worth having:
`--max-score` is documented as "cap scored jobs per run" but is implemented
as a `LIMIT` on rows *pulled*, so hard-filtered jobs consume the budget and
a run can score zero.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_run.py` (imports needed: `from career_agent import db`,
`from career_agent.models import Verdict`):

First extend the existing `_Args` class so a test can point at its own
brief instead of the repository's:

```python
class _Args:
    def __init__(self, db_path, max_score, brief="career_brief.toml"):
        self.db = str(db_path)
        self.brief = brief
        self.boards = "ats_boards.toml"
        self.max_score = max_score
```

Then add:

```python
TEST_BRIEF = """\
target_titles = ["AI Engineer"]
title_families = ["ai engineer"]
search_locations = ["Chennai"]
locations = ["Chennai"]
remote_ok = false
daily_cap = 5
gate_threshold = 72
staleness_days = 3650
"""

VERDICT = Verdict(role_fit=80, credibility=80, opportunity=80,
                  application_quality=80, eligibility_soft=80,
                  verdict="submit", rationale="ok")


def _seed_job(conn, fp, location):
    conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized, location)"
        " VALUES (?, 'ats', ?, 'Acme', 'acme', 'AI Engineer',"
        " 'aiengineer', ?)", (fp, fp, location))


def _prepare(tmp_path, monkeypatch):
    """Env + a stubbed discovery, so run_once exercises only the scoring loop."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "dummy")
    monkeypatch.setattr("career_agent.discovery.run_discovery",
                        lambda *a, **k: [])
    brief_path = tmp_path / "brief.toml"
    brief_path.write_text(TEST_BRIEF, encoding="utf-8")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    return db_path, str(brief_path), conn


async def test_max_score_caps_model_calls_not_rows_examined(
        tmp_path, monkeypatch):
    """The bug: --max-score was a LIMIT on rows pulled, so jobs the hard
    filter rejects consumed the budget and a run scored ~nothing."""
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(20):
        _seed_job(conn, f"far{i}", "San Francisco, CA")   # fail the filter
    for i in range(3):
        _seed_job(conn, f"near{i}", "Chennai")            # pass it
    conn.commit()

    calls = []

    async def fake_score(job, brief, facts, ask):
        calls.append(job.external_id)
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=2, brief=brief_path))

    assert len(calls) == 2, "the cap must bound MODEL CALLS"
    conn = db.connect(db_path)
    hard = conn.execute("SELECT COUNT(*) n FROM assessment"
                        " WHERE stage = 'hard'").fetchone()["n"]
    assert hard == 20, "the whole pool must still be hard-filtered in one sweep"


async def test_hard_skips_persist_so_the_next_run_finds_real_candidates(
        tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(5):
        _seed_job(conn, f"far{i}", "San Francisco, CA")
    _seed_job(conn, "near0", "Chennai")
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=0, brief=brief_path))

    conn = db.connect(db_path)
    from career_agent import gate, store
    # scoring was disabled, but the sweep still retired the 5 rejects, so the
    # remaining pool is exactly the one real candidate
    remaining = store.unscored_jobs(conn, gate.PROMPT_VERSION)
    assert [r["external_id"] for r in remaining] == ["near0"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_run.py -k "max_score_caps or hard_skips_persist" -v`
Expected: FAIL — the first asserts `len(calls) == 2` but gets 0 (the
`LIMIT 2` pulls two San Francisco jobs); the second gets 6 remaining
because `LIMIT 0` returns no rows, so nothing is swept.

- [ ] **Step 3: Implement**

In `src/career_agent/store.py`, replace `unscored_jobs`:

```python
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
```

In `src/career_agent/run.py`, replace the scoring loop in `run_once`:

```python
    # 4-5. hard filter the whole pool, then score as many survivors as the
    # budget allows. The sweep is deterministic, local, free, and PERSISTED,
    # and unscored_jobs already excludes stage='hard' -- so one pass retires
    # every job that can never pass and the next run finds real candidates
    # immediately. Breaking early instead would leave the pool dirty and
    # reproduce the bug on the following run.
    scored = skipped = 0
    for row in store.unscored_jobs(conn, gate.PROMPT_VERSION):
        job = _row_to_job(row)

        reason = hardfilter.check(job, brief)
        if reason:
            store.save_hard_skip(conn, row["id"], reason)
            skipped += 1
            continue

        if scored >= args.max_score:
            continue  # budget spent; this survivor carries to the next run

        verdict = await gate.score(job, brief, store.facts(conn), _ask)
        store.save_assessment(conn, row["id"], verdict, MODEL_ID,
                              gate.PROMPT_VERSION)
        scored += 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_run.py tests/test_store.py -v`
Expected: PASS, including the pre-existing `test_run_once_derives_no_response_after_scoring`.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/store.py src/career_agent/run.py tests/test_run.py
git commit -m "fix: cap scored jobs by model calls, not rows examined"
```

---

## Task 4: model selection and settings precedence in `run_once`

**Files:**
- Modify: `src/career_agent/run.py`
- Modify: `src/career_agent/web/pipeline.py`
- Test: `tests/test_run.py`

**Interfaces:**
- Consumes: `store.get_settings` (Task 1), `config.SCORING_MODELS`.
- Produces: `run._ask(prompt, model=None)`; `run_once` resolving both the model and the scoring cap from the `setting` row, with an explicitly-passed `--max-score` overriding for that run only.
- Removes: the `MODEL_ID = "claude-agent-sdk"` placeholder constant.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_run.py` (reusing `_prepare`, `_seed_job`, `VERDICT`,
`_Args` from Task 3):

```python
async def test_scoring_model_comes_from_settings(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")
    conn.commit()
    from career_agent import store
    store.save_settings(conn, "claude-haiku-4-5-20251001", 25)

    seen = {}

    async def fake_score(job, brief, facts, ask):
        seen["model"] = ask.keywords["model"]
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=None, brief=brief_path))

    assert seen["model"] == "claude-haiku-4-5-20251001"
    conn = db.connect(db_path)
    row = conn.execute("SELECT model FROM assessment"
                       " WHERE stage = 'scored'").fetchone()
    assert row["model"] == "claude-haiku-4-5-20251001", \
        "the stored verdict must be attributable to the model that produced it"


async def test_omitted_max_score_uses_the_stored_setting(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(4):
        _seed_job(conn, f"near{i}", "Chennai")
    conn.commit()
    from career_agent import store
    store.save_settings(conn, "claude-sonnet-5", 2)

    calls = []

    async def fake_score(job, brief, facts, ask):
        calls.append(job.external_id)
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=None, brief=brief_path))
    assert len(calls) == 2


async def test_explicit_max_score_overrides_the_setting(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(4):
        _seed_job(conn, f"near{i}", "Chennai")
    conn.commit()
    from career_agent import store
    store.save_settings(conn, "claude-sonnet-5", 2)

    calls = []

    async def fake_score(job, brief, facts, ask):
        calls.append(job.external_id)
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=3, brief=brief_path))
    assert len(calls) == 3
    # the override is for this run only and must not be persisted
    conn = db.connect(db_path)
    assert store.get_settings(conn)["max_score_per_run"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_run.py -k "scoring_model or max_score_uses or overrides" -v`
Expected: FAIL — `ask` is the bare `_ask` function with no `.keywords`, the
stored model is `"claude-agent-sdk"`, and `args.max_score=None` raises a
`TypeError` comparing `int >= None`.

- [ ] **Step 3: Implement**

In `src/career_agent/run.py`:

1. Add `import functools` to the imports, and change the config import to
   `from career_agent.config import load_boards, load_brief`.
2. Delete the `MODEL_ID = "claude-agent-sdk"` line.
3. Give `_ask` a model parameter:

```python
async def _ask(prompt: str, model: str | None = None) -> str:
    """One tool-less call for gate scoring."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  TextBlock, query)

    chunks = []
    async for message in query(prompt=prompt,
                               options=ClaudeAgentOptions(tools=None,
                                                          model=model)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks)
```

4. In `run_once`, resolve settings after opening the connection and move
   the zero-budget warning below it, so the warning reflects the *effective*
   value rather than just the flag:

```python
async def run_once(args) -> None:
    verify_auth()

    conn = db.connect(Path(args.db))
    db.init_schema(conn)

    settings = store.get_settings(conn)
    scoring_model = settings["scoring_model"]
    # An explicit --max-score overrides the stored setting for THIS RUN only
    # and is not persisted; omitting it uses the Settings value.
    max_score = (args.max_score if getattr(args, "max_score", None) is not None
                 else settings["max_score_per_run"])
    ask = functools.partial(_ask, model=scoring_model)

    if max_score == 0:
        log.warning("scoring cap is 0: scoring is disabled this run; only "
                    "discovery and the hard filter will run")

    brief = load_brief(Path(args.brief))
    boards = load_boards(Path(args.boards))
```

5. In the scoring loop, use the resolved values:

```python
        if scored >= max_score:
            continue  # budget spent; this survivor carries to the next run

        verdict = await gate.score(job, brief, store.facts(conn), ask)
        store.save_assessment(conn, row["id"], verdict, scoring_model,
                              gate.PROMPT_VERSION)
```

6. In `main()`, make the flag's absence meaningful:

```python
    parser.add_argument("--max-score", type=int, default=None, dest="max_score",
                        help="cap model calls this run, overriding the stored "
                             "Settings value; omit to use that value")
```

In `src/career_agent/web/pipeline.py`, change the signature default so the
dashboard uses the stored setting instead of forcing 25:

```python
async def run_background(conn_factory, db_path, brief_path,
                         boards_path: str = "ats_boards.toml",
                         max_score: int | None = None) -> None:
```

The `SimpleNamespace(...)` line is unchanged — it already forwards
`max_score`, which is now `None` by default and resolved from the setting
inside `run_once`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS, no regressions. `tests/test_pipeline.py`'s existing
`test_run_background_passes_paths_and_defaults_through` asserts
`args.max_score == 25`; update that assertion to `is None` in the same
commit, since the default deliberately changed.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/run.py src/career_agent/web/pipeline.py tests/test_run.py tests/test_pipeline.py
git commit -m "feat: select the scoring model from settings and record it"
```

---

## Task 5: the Settings screen

**Files:**
- Modify: `src/career_agent/web/app.py`
- Create: `src/career_agent/web/templates/settings.html`
- Modify: `src/career_agent/web/templates/base.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `config.save_brief`, `config.SCORING_MODELS`, `config.MODEL_LABELS`, `config.CareerBrief`, `store.get_settings`, `store.save_settings`.
- Produces: `GET /settings`, `POST /settings`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`. **The `brief_path` fixture below is
mandatory** — without it these tests overwrite the repository's real
`career_brief.toml`:

```python
BRIEF_TOML = """\
# Keep this comment.
target_titles = ["AI Engineer"]
search_locations = ["Chennai"]
locations = ["Chennai", "Remote"]
remote_ok = true
salary_floor_inr = 1200000
daily_cap = 5
gate_threshold = 72
staleness_days = 30
"""


@pytest.fixture
def brief_path(tmp_path, monkeypatch):
    p = tmp_path / "career_brief.toml"
    p.write_text(BRIEF_TOML, encoding="utf-8")
    monkeypatch.setattr(web, "BRIEF_PATH", p)
    return p


def _form(**overrides):
    base = {"target_titles": "AI Engineer", "title_families": "",
            "search_locations": "Chennai", "locations": "Chennai, Remote",
            "work_authorization": "", "excluded_companies": "",
            "non_negotiables": "", "remote_ok": "on",
            "salary_floor_inr": "1200000", "daily_cap": "5",
            "gate_threshold": "72", "staleness_days": "30",
            "scoring_model": "claude-sonnet-5", "max_score_per_run": "25"}
    return {**base, **overrides}


def test_settings_page_renders_current_values(client, brief_path):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Career Brief" in r.text
    assert "Agent Settings" in r.text
    assert "claude-sonnet-5" in r.text
    assert "AI Engineer" in r.text


def test_settings_nav_link_is_wired_up(client, brief_path):
    assert 'href="/settings"' in client.get("/settings").text


def test_saving_persists_the_agent_settings(client, brief_path):
    r = client.post("/settings", data=_form(
        scoring_model="claude-haiku-4-5-20251001", max_score_per_run="40"))
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    s = store.get_settings(conn)
    assert s["scoring_model"] == "claude-haiku-4-5-20251001"
    assert s["max_score_per_run"] == 40


def test_saving_writes_the_brief_and_keeps_its_comments(client, brief_path):
    client.post("/settings", data=_form(daily_cap="9",
                                        target_titles="AI Engineer, ML Engineer"))
    text = brief_path.read_text(encoding="utf-8")
    assert "# Keep this comment." in text
    brief = load_brief(brief_path)
    assert brief.daily_cap == 9
    assert brief.target_titles == ["AI Engineer", "ML Engineer"]


def test_an_invalid_brief_leaves_both_stores_untouched(client, brief_path):
    before = brief_path.read_text(encoding="utf-8")
    r = client.post("/settings", data=_form(
        target_titles="",                      # violates min_length=1
        scoring_model="claude-haiku-4-5-20251001"))
    assert r.status_code == 200
    assert "target_titles" in r.text
    assert brief_path.read_text(encoding="utf-8") == before
    conn = db.connect(web.DB_PATH)
    assert store.get_settings(conn)["scoring_model"] == "claude-sonnet-5"


def test_an_unknown_model_is_rejected(client, brief_path):
    before = brief_path.read_text(encoding="utf-8")
    r = client.post("/settings", data=_form(scoring_model="gpt-4"))
    assert "scoring_model" in r.text
    assert brief_path.read_text(encoding="utf-8") == before


def test_a_missing_brief_file_does_not_crash_the_page(client, tmp_path,
                                                      monkeypatch):
    """Rendering brief defaults would be a trap: saving them would then
    write a brief the user never chose over the file they lost."""
    monkeypatch.setattr(web, "BRIEF_PATH", tmp_path / "gone.toml")
    r = client.get("/settings")
    assert r.status_code == 200
    assert "could not be read" in r.text
    # the Agent Settings half still works, since it does not need the file
    assert "Scoring model" in r.text
```

Add `from career_agent import store` and `from career_agent.config import
load_brief` to `tests/test_web.py`'s imports if not already present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -k settings -v`
Expected: FAIL — 404 on `/settings`.

- [ ] **Step 3: Implement**

In `src/career_agent/web/app.py`, extend the config import to
`from career_agent.config import (MODEL_LABELS, SCORING_MODELS, CareerBrief,
load_brief, save_brief)`, add `from pydantic import ValidationError`, and
append:

```python
def _split_list(raw: str) -> list[str]:
    """List fields are comma-separated text inputs, which keeps the form
    static HTML with no JS array widgets."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _validate_brief(target_titles, title_families, search_locations, locations,
                    work_authorization, excluded_companies, non_negotiables,
                    remote_ok, salary_floor_inr, daily_cap, gate_threshold,
                    staleness_days, errors) -> CareerBrief | None:
    """Build a CareerBrief from the form, recording any problems in `errors`.
    Validation is the pydantic model's job -- min_length on target_titles and
    search_locations, daily_cap >= 1, 0 <= gate_threshold <= 100,
    staleness_days >= 1 -- so no second rule set is written here."""
    try:
        return CareerBrief(
            target_titles=_split_list(target_titles),
            title_families=_split_list(title_families),
            search_locations=_split_list(search_locations),
            locations=_split_list(locations),
            work_authorization=_split_list(work_authorization),
            excluded_companies=_split_list(excluded_companies),
            non_negotiables=_split_list(non_negotiables),
            remote_ok=remote_ok is not None,
            salary_floor_inr=(int(salary_floor_inr)
                              if salary_floor_inr.strip() else None),
            daily_cap=daily_cap, gate_threshold=gate_threshold,
            staleness_days=staleness_days)
    except ValidationError as exc:
        for err in exc.errors():
            field = err["loc"][0] if err["loc"] else "form"
            errors[str(field)] = err["msg"]
    except ValueError:
        errors["salary_floor_inr"] = "must be a whole number, or blank"
    return None


def _settings_context(conn, *, brief=None, settings=None, form=None,
                      errors=None, saved=False) -> dict:
    """Values shown come from the stores unless a failed submission is being
    re-rendered, in which case the user's own input is preserved."""
    brief_error = None
    if brief is None:
        try:
            brief = load_brief(BRIEF_PATH)
        except Exception as exc:
            # Falling back to CareerBrief() defaults would be a trap: saving
            # them would overwrite the file the user lost with a brief they
            # never chose. Disable that half of the form instead.
            brief_error = f"{BRIEF_PATH} could not be read: {exc}"
    settings = settings or store.get_settings(conn)
    today_submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    return {"active_nav": "settings", "brief": brief,
            "brief_error": brief_error,
            "daily_cap": brief.daily_cap if brief else "-",
            "today_submitted": today_submitted,
            "settings": settings, "scoring_models": SCORING_MODELS,
            "model_labels": MODEL_LABELS, "form": form or {},
            "errors": errors or {}, "saved": saved}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    conn = _conn()
    return templates.TemplateResponse(request=request, name="settings.html",
                                      context=_settings_context(conn))


@app.post("/settings", response_class=HTMLResponse)
def settings_save(request: Request,
                  target_titles: str = Form(""),
                  title_families: str = Form(""),
                  search_locations: str = Form(""),
                  locations: str = Form(""),
                  work_authorization: str = Form(""),
                  excluded_companies: str = Form(""),
                  non_negotiables: str = Form(""),
                  remote_ok: str | None = Form(None),
                  salary_floor_inr: str = Form(""),
                  daily_cap: int = Form(5),
                  gate_threshold: int = Form(72),
                  staleness_days: int = Form(30),
                  scoring_model: str = Form(...),
                  max_score_per_run: int = Form(25),
                  brief_present: str | None = Form(None)):
    conn = _conn()
    form = {"target_titles": target_titles, "title_families": title_families,
            "search_locations": search_locations, "locations": locations,
            "work_authorization": work_authorization,
            "excluded_companies": excluded_companies,
            "non_negotiables": non_negotiables, "remote_ok": remote_ok,
            "salary_floor_inr": salary_floor_inr, "daily_cap": daily_cap,
            "gate_threshold": gate_threshold, "staleness_days": staleness_days,
            "scoring_model": scoring_model,
            "max_score_per_run": max_score_per_run}
    errors: dict[str, str] = {}

    # Validate EVERYTHING before writing ANYTHING: a partial save would leave
    # the DB describing a state the TOML does not.
    #
    # brief_present is the hidden marker the brief section renders. When the
    # TOML could not be read that section is hidden, and this POST carries
    # only Agent Settings -- which must still save, exactly as the banner on
    # that page promises.
    brief = _validate_brief(
        target_titles, title_families, search_locations, locations,
        work_authorization, excluded_companies, non_negotiables,
        remote_ok, salary_floor_inr, daily_cap, gate_threshold,
        staleness_days, errors) if brief_present else None

    if scoring_model not in SCORING_MODELS:
        errors["scoring_model"] = f"unknown scoring model: {scoring_model}"
    if max_score_per_run < 0:
        errors["max_score_per_run"] = "cannot be negative"

    if errors:
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context=_settings_context(conn, form=form, errors=errors))

    if brief is not None:
        save_brief(BRIEF_PATH, brief)
    store.save_settings(conn, scoring_model, max_score_per_run)
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context=_settings_context(conn, saved=True))
```

In `src/career_agent/web/templates/base.html`, wire the nav stub:

```html
      <a href="/settings" {% if active_nav == "settings" %}class="active"{% endif %}>Settings</a>
```

Guard the sidebar's Career Brief card, which currently dereferences
`brief` unconditionally and would raise `UndefinedError` on the Settings
page when the TOML could not be read. Wrap the existing
`<div class="card">` holding the `Career Brief` heading and its `.kv`
block in:

```html
    {% if brief %}
    ... the existing Career Brief card, unchanged ...
    {% endif %}
```

Then add to the `<style>` block, after the overview-page rules:

```css
  /* settings page */
  .field{display:grid;gap:4px;margin-bottom:12px;max-width:640px}
  .field label{font-size:11px;font-weight:700;color:var(--muted)}
  .field input[type=text],.field input[type=number],.field select{
    border:1px solid var(--line);border-radius:8px;padding:8px 10px;
    font-size:13px;font-family:inherit;background:#fff}
  .field .hint{font-size:10px;color:var(--muted)}
  .field .err{font-size:11px;color:var(--red);font-weight:600}
  .field.checkbox{display:flex;align-items:center;gap:8px}
```

Create `src/career_agent/web/templates/settings.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Settings</h1>

{% if saved %}
<div class="card" style="margin-bottom:12px"><span class="done">Settings saved.</span>
  <span class="rationale">They take effect on the next run — no restart needed.</span></div>
{% endif %}
{% if errors %}
<div class="card" style="margin-bottom:12px">
  <span class="denied"><b>Nothing was saved.</b></span>
  <ul class="rationale">
    {% for field, message in errors.items() %}<li>{{ field }}: {{ message }}</li>{% endfor %}
  </ul>
</div>
{% endif %}

{% if brief_error %}
<div class="card" style="margin-bottom:12px">
  <span class="denied"><b>Career brief unavailable.</b> {{ brief_error }}</span>
  <div class="rationale">Agent Settings below still work. The brief section is
    hidden rather than pre-filled with defaults, because saving those would
    overwrite the file with a brief you never chose.</div>
</div>
{% endif %}

<form method="post" action="/settings">
  {% if brief %}
  <div class="card panel">
    <div class="panel-head"><h2>Career Brief</h2>
      <span class="run-meta">saves to career_brief.toml</span></div>
    <!-- Marks the brief half as submitted. Without it, a POST from a page
         whose brief section was hidden looks identical to one where the user
         cleared every field, and the save would fail on an empty brief. -->
    <input type="hidden" name="brief_present" value="1">

    {% macro textfield(name, label, value, hint="") %}
    <div class="field">
      <label for="{{ name }}">{{ label }}</label>
      <input type="text" id="{{ name }}" name="{{ name }}" value="{{ value }}">
      {% if hint %}<span class="hint">{{ hint }}</span>{% endif %}
      {% if errors.get(name) %}<span class="err">{{ errors[name] }}</span>{% endif %}
    </div>
    {% endmacro %}

    {{ textfield("target_titles", "Target titles",
                 form.target_titles if form else brief.target_titles | join(", "),
                 "Comma-separated. At least one required.") }}
    {{ textfield("title_families", "Title families",
                 form.title_families if form else brief.title_families | join(", "),
                 "Comma-separated. The hard filter matches these against job titles.") }}
    {{ textfield("search_locations", "Search locations",
                 form.search_locations if form else brief.search_locations | join(", "),
                 "What we ASK sources for. Each city multiplies daily Actor runs.") }}
    {{ textfield("locations", "Accepted locations",
                 form.locations if form else brief.locations | join(", "),
                 "What the hard filter ACCEPTS on the way back. Usually a superset.") }}
    {{ textfield("work_authorization", "Work authorization",
                 form.work_authorization if form else brief.work_authorization | join(", ")) }}
    {{ textfield("excluded_companies", "Excluded companies",
                 form.excluded_companies if form else brief.excluded_companies | join(", ")) }}
    {{ textfield("non_negotiables", "Non-negotiables",
                 form.non_negotiables if form else brief.non_negotiables | join(", ")) }}

    <div class="field checkbox">
      <input type="checkbox" id="remote_ok" name="remote_ok"
             {% if (form.remote_ok if form else brief.remote_ok) %}checked{% endif %}>
      <label for="remote_ok">Remote acceptable</label>
    </div>

    <div class="field">
      <label for="salary_floor_inr">Salary floor (INR/year)</label>
      <input type="number" id="salary_floor_inr" name="salary_floor_inr" min="0"
             value="{{ form.salary_floor_inr if form else (brief.salary_floor_inr or '') }}">
      <span class="hint">Leave blank for no floor. 0 would mean something else.</span>
      {% if errors.get("salary_floor_inr") %}<span class="err">{{ errors["salary_floor_inr"] }}</span>{% endif %}
    </div>

    <div class="field">
      <label for="daily_cap">Daily application cap</label>
      <input type="number" id="daily_cap" name="daily_cap" min="1"
             value="{{ form.daily_cap if form else brief.daily_cap }}">
      {% if errors.get("daily_cap") %}<span class="err">{{ errors["daily_cap"] }}</span>{% endif %}
    </div>

    <div class="field">
      <label for="gate_threshold">Gate threshold</label>
      <input type="number" id="gate_threshold" name="gate_threshold" min="0" max="100"
             value="{{ form.gate_threshold if form else brief.gate_threshold }}">
      <span class="hint">Weighted score at or above this scores "submit".</span>
      {% if errors.get("gate_threshold") %}<span class="err">{{ errors["gate_threshold"] }}</span>{% endif %}
    </div>

    <div class="field">
      <label for="staleness_days">Staleness (days)</label>
      <input type="number" id="staleness_days" name="staleness_days" min="1"
             value="{{ form.staleness_days if form else brief.staleness_days }}">
      {% if errors.get("staleness_days") %}<span class="err">{{ errors["staleness_days"] }}</span>{% endif %}
    </div>
  </div>
  {% endif %}

  <div class="card panel" style="margin-top:11px">
    <div class="panel-head"><h2>Agent Settings</h2>
      <span class="run-meta">saves to the database</span></div>

    <div class="field">
      <label for="scoring_model">Scoring model</label>
      <select id="scoring_model" name="scoring_model">
        {% set current = form.scoring_model if form else settings["scoring_model"] %}
        {% for m in scoring_models %}
        <option value="{{ m }}" {% if m == current %}selected{% endif %}>{{ model_labels[m] }}</option>
        {% endfor %}
      </select>
      <span class="hint">Recorded on every verdict, so past scores stay attributable.</span>
      {% if errors.get("scoring_model") %}<span class="err">{{ errors["scoring_model"] }}</span>{% endif %}
    </div>

    <div class="field">
      <label for="max_score_per_run">Jobs scored per run</label>
      <input type="number" id="max_score_per_run" name="max_score_per_run" min="0"
             value="{{ form.max_score_per_run if form else settings['max_score_per_run'] }}">
      <span class="hint">Caps model calls, not jobs examined. 0 runs discovery
        and the hard filter only.</span>
      {% if errors.get("max_score_per_run") %}<span class="err">{{ errors["max_score_per_run"] }}</span>{% endif %}
    </div>
  </div>

  <button class="btn primary" style="margin-top:11px" type="submit">Save settings</button>
</form>
{% endblock %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/settings.html src/career_agent/web/templates/base.html tests/test_web.py
git commit -m "feat: add the Settings screen"
```

---

## Task 6: Manual verification

- [ ] **Step 1: Start the dashboard**

`.venv/Scripts/career-agent.exe serve`, then open
`http://localhost:8000/settings`.

- [ ] **Step 2: Confirm the page reflects the real config**

The Career Brief section should show the actual values from
`career_brief.toml` (target titles including "AI Engineer", accepted
locations "Chennai, Remote", daily cap 5, gate threshold 72). Agent
Settings should show Sonnet and 25. The sidebar "Settings" nav entry
should be highlighted.

- [ ] **Step 3: Save a change and verify both stores**

Change the model to Haiku and jobs-per-run to 30, and change the daily cap
to 6. Save. Then check, from the repository root:

```bash
git diff career_brief.toml
```

The diff must show **only** `daily_cap` changing, with every comment
intact. Then confirm the DB side:

```bash
.venv/Scripts/python.exe -c "from pathlib import Path; from career_agent import db, store; print(dict(store.get_settings(db.connect(Path('data/career.db')))))"
```

- [ ] **Step 4: Confirm an invalid save changes nothing**

Clear the Target titles field and save. The page must re-render with a
`target_titles` error, `git diff career_brief.toml` must be unchanged from
step 3, and the stored settings must be unchanged.

- [ ] **Step 5: Confirm scoring uses the configuration**

Restore the daily cap, set jobs-per-run to 3, and trigger **Run Now** from
the Dashboard. When it completes, confirm exactly three new scored
assessments exist and that they record the configured model:

```bash
.venv/Scripts/python.exe -c "
from pathlib import Path
from career_agent import db
conn = db.connect(Path('data/career.db'))
for r in conn.execute(\"SELECT model, COUNT(*) n FROM assessment WHERE stage='scored' GROUP BY model\"):
    print(dict(r))
"
```

This is the end-to-end proof: the budget capped model calls (not rows), the
configured model ran, and the verdict is attributable to it.

- [ ] **Step 6: Report results**

Note any visual issues or numbers that look wrong — fix inline if small,
or flag as a follow-up if they need decisions beyond this plan.
