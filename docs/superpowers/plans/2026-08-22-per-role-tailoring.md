# Per-Role Tailoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For any job the gate marked `submit` or `hold`, produce a real, job-specific DOCX resume the first time you Apply to it — built by selecting and rephrasing entries from the verified `fact` table into your own resume's layout — and make every version generated browsable and downloadable later.

**Architecture:** A new `career_agent/tailor.py` module mirrors `gate.py`'s shape: one LLM call per job, trusted to select-and-rephrase facts but never invent them, output validated against the current `fact` table before anything is rendered. Rendering is deterministic `python-docx` paragraph-marker replacement against a user-supplied master template, never the LLM. A new shared `worker.tailor_for_apply()` helper triggers this the first time a job is drafted — whether that draft comes from the dashboard's Apply button or the auto-mode worker loop — and both existing draft-creation call sites are changed to use it. New `resume.job_id` / `resume.content` columns and four small `store.py` helpers make each generation an additive, auditable row; two new routes (a download endpoint and a Resumes page) make the history visible.

**Tech Stack:** Python 3.13, FastAPI, Jinja2/htmx, SQLite, Pydantic v2, `python-docx` (new dependency), the existing Agent SDK subscription call path (`run._ask`).

**Spec:** `docs/superpowers/specs/2026-08-22-per-role-tailoring-design.md`

## Global Constraints

- DOCX is the only rendered format. No PDF export in this slice.
- The master template is a file at the conventional path `resume/master.docx`, gitignored, provided by the user directly — no in-app upload/edit UI.
- A job is tailored once. There is no "Retailor" action; regenerating on demand is out of scope.
- Tailoring trusts the model to **select and rephrase**, never invent — the same trust model `gate.py` already uses for its `credibility` dimension. Every bullet must cite the `fact` row id(s) it draws from, and every cited id is validated against the current `fact` table before a result is accepted.
- At least one bullet is required; a zero-bullet result is refused and retried, never rendered.
- `resume` table rows are additive — one row per generation, never an overwrite — per the v1 spec's note that this is what makes "what exactly did I send them" answerable later.
- Reuse existing patterns rather than inventing new ones: `gate.py`'s prompt/parse/retry-once shape for `tailor.py`; `store.upsert_jobs`'s insert-then-catch-`IntegrityError`-then-reread shape for the `resume.version` uniqueness race; `test_gate.py`'s table-driven style for `test_tailor.py`.

---

## Task 1: Data model — `resume.job_id`/`resume.content` columns, and store.py lookup helpers

**Files:**
- Modify: `src/career_agent/db.py:140-154` (`init_schema`)
- Modify: `src/career_agent/store.py` (add functions after `facts()`, currently `src/career_agent/store.py:82-84`)
- Test: `tests/test_db.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `store.fact_rows(conn) -> list[tuple[int, str]]` (id, formatted text — same text format as the existing `store.facts()`); `store.latest_resume_version(conn, job_id: int) -> str | None`; `store.resume_version_for(conn, job_id: int) -> str` (never `None`, falls back to `ats.RESUME_VERSION`); `store.next_resume_version(conn, job_id: int) -> str`; `store.insert_resume(conn, job_id: int, version: str, path: str, content: str) -> str` (returns the version actually stored — the winner's, if a concurrent insert raced).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`, after `test_job_priority_defaults_to_null`:

```python
def test_resume_has_job_id_and_content_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(resume)")}
    assert {"job_id", "content"} <= cols


def test_resume_columns_are_added_idempotently(conn):
    db.init_schema(conn)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(resume)")]
    assert cols.count("job_id") == 1
    assert cols.count("content") == 1
```

Add to `tests/test_store.py`, after `_seed_one`:

```python
def _seed_facts(conn, n=10):
    for i in range(n):
        conn.execute("INSERT INTO fact (claim, evidence) VALUES (?, ?)",
                     (f"claim {i}", f"evidence {i}"))
    conn.commit()


def test_fact_rows_returns_ids_paired_with_formatted_text(conn):
    _seed_facts(conn, n=2)
    rows = store.fact_rows(conn)
    assert rows == [(1, "claim 0 (evidence: evidence 0)"),
                    (2, "claim 1 (evidence: evidence 1)")]


def test_latest_resume_version_is_none_with_no_rows(conn):
    job_id = _seed_one(conn)
    assert store.latest_resume_version(conn, job_id) is None


def test_latest_resume_version_returns_the_newest_row(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'a.docx', ?)", (job_id,))
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r2', 'b.docx', ?)", (job_id,))
    conn.commit()
    assert store.latest_resume_version(conn, job_id) == "tailored-1-r2"


def test_resume_version_for_falls_back_to_the_constant(conn):
    job_id = _seed_one(conn)
    assert store.resume_version_for(conn, job_id) == ats_apply.RESUME_VERSION


def test_resume_version_for_prefers_a_tailored_row(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'a.docx', ?)", (job_id,))
    conn.commit()
    assert store.resume_version_for(conn, job_id) == "tailored-1-r1"


def test_next_resume_version_counts_existing_rows_for_that_job(conn):
    job_id = _seed_one(conn)
    assert store.next_resume_version(conn, job_id) == f"tailored-{job_id}-r1"
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES (?, 'a.docx', ?)",
                 (f"tailored-{job_id}-r1", job_id))
    conn.commit()
    assert store.next_resume_version(conn, job_id) == f"tailored-{job_id}-r2"


def test_insert_resume_stores_a_new_row(conn):
    job_id = _seed_one(conn)
    version = store.insert_resume(conn, job_id, "tailored-1-r1", "a.docx", "{}")
    assert version == "tailored-1-r1"
    row = conn.execute("SELECT * FROM resume WHERE version = ?",
                       (version,)).fetchone()
    assert row["job_id"] == job_id
    assert row["path"] == "a.docx"


def test_insert_resume_on_a_version_collision_returns_the_winner(conn):
    job_id = _seed_one(conn)
    store.insert_resume(conn, job_id, "tailored-1-r1", "a.docx", "{}")
    # A second insert under the same version string (the concurrent-click
    # race) must not raise -- it reports back the row that actually won.
    version = store.insert_resume(conn, job_id, "tailored-1-r1", "b.docx", "{}")
    assert version == "tailored-1-r1"
    assert conn.execute(
        "SELECT COUNT(*) n FROM resume WHERE version = ?",
        ("tailored-1-r1",)).fetchone()["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -k resume_has_job_id -v tests/test_store.py -k "fact_rows or resume_version or insert_resume" -v`
Expected: FAIL — `resume` has no `job_id`/`content` columns, and `store` has none of the five new functions.

- [ ] **Step 3: Add the migration to `db.py`**

In `src/career_agent/db.py`, inside `init_schema` (after the existing `_add_column_if_missing(conn, "job", "priority", "INTEGER")` line):

```python
    _add_column_if_missing(conn, "resume", "job_id", "INTEGER REFERENCES job(id)")
    _add_column_if_missing(conn, "resume", "content", "TEXT")
```

- [ ] **Step 4: Add the helpers to `store.py`**

In `src/career_agent/store.py`, immediately after the existing `facts()` function:

```python
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
                  content: str) -> str:
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
```

`sqlite3` is already imported at the top of `store.py`; no new import needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_db.py -k resume -v tests/test_store.py -k "fact_rows or resume_version or insert_resume" -v`
Expected: PASS

- [ ] **Step 6: Run the full suite to check nothing else broke**

Run: `pytest -q`
Expected: PASS (these are purely additive changes)

- [ ] **Step 7: Commit**

```bash
git add src/career_agent/db.py src/career_agent/store.py tests/test_db.py tests/test_store.py
git commit -m "feat: add resume.job_id/content columns and version lookup helpers"
```

---

## Task 2: `tailor.py` core — models, prompt, parse, validate, `tailor()`

**Files:**
- Modify: `src/career_agent/models.py` (add after `Verdict`, currently ending `src/career_agent/models.py:36`)
- Create: `src/career_agent/tailor.py`
- Test: `tests/test_tailor.py`

**Interfaces:**
- Consumes: `career_agent.gate.MIN_FACTS_HARD` (int, 10), `career_agent.gate.InsufficientFacts` (exception class) — both reused directly, not redefined.
- Produces: `models.Bullet` (fields: `text: str`, `fact_ids: list[int]`), `models.TailorResult` (fields: `summary: str`, `bullets: list[Bullet]`); `tailor.TAILOR_PROMPT_VERSION` (str constant); `tailor.build_prompt(job: Job, brief: CareerBrief, facts: list[tuple[int, str]]) -> str`; `tailor.parse_tailor_result(text: str) -> TailorResult`; `async tailor.tailor(job: Job, brief: CareerBrief, facts: list[tuple[int, str]], ask: Callable[[str], Awaitable[str]]) -> TailorResult`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tailor.py`:

```python
import json

import pytest

from career_agent.config import CareerBrief
from career_agent.gate import InsufficientFacts
from career_agent.models import Job
from career_agent.tailor import build_prompt, parse_tailor_result, tailor

BRIEF = CareerBrief(target_titles=["AI Engineer"], search_locations=["Chennai"])
JOB = Job(source="ats", external_id="1", company="Acme", title="AI Engineer",
          description="Build LLM-backed features in Python.")
FACTS = [(i, f"claim {i} (evidence: evidence {i})") for i in range(1, 11)]

GOOD = json.dumps({"summary": "Tailored summary.", "bullets": [
    {"text": "Built an LLM feature", "fact_ids": [1, 2]}]})


def test_build_prompt_includes_fact_ids_and_job_description():
    prompt = build_prompt(JOB, BRIEF, FACTS)
    assert "[1] claim 1" in prompt
    assert "Build LLM-backed features" in prompt


def test_parses_json_out_of_prose():
    r = parse_tailor_result(f"Here it is.\n```json\n{GOOD}\n```")
    assert r.summary == "Tailored summary."
    assert r.bullets[0].fact_ids == [1, 2]


def test_rejects_unparseable():
    with pytest.raises(ValueError):
        parse_tailor_result("I could not decide.")


def test_rejects_zero_bullets():
    with pytest.raises(ValueError):
        parse_tailor_result(json.dumps({"summary": "S", "bullets": []}))


def test_rejects_a_bullet_with_no_fact_ids():
    with pytest.raises(ValueError):
        parse_tailor_result(json.dumps(
            {"summary": "S", "bullets": [{"text": "x", "fact_ids": []}]}))


async def test_raises_below_ten_facts():
    async def ask(_):
        raise AssertionError("must not call the model")

    with pytest.raises(InsufficientFacts):
        await tailor(JOB, BRIEF, FACTS[:2], ask)


async def test_retries_once_on_bad_output():
    calls = []

    async def ask(prompt):
        calls.append(prompt)
        return "nonsense" if len(calls) == 1 else GOOD

    r = await tailor(JOB, BRIEF, FACTS, ask)
    assert r.summary == "Tailored summary."
    assert len(calls) == 2


async def test_retries_once_on_an_unknown_fact_id():
    calls = []
    bad = json.dumps({"summary": "S", "bullets": [
        {"text": "x", "fact_ids": [999]}]})

    async def ask(prompt):
        calls.append(prompt)
        return bad if len(calls) == 1 else GOOD

    r = await tailor(JOB, BRIEF, FACTS, ask)
    assert r.summary == "Tailored summary."
    assert len(calls) == 2


async def test_raises_after_second_failure():
    async def ask(_):
        return "still nonsense"

    with pytest.raises(ValueError):
        await tailor(JOB, BRIEF, FACTS, ask)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tailor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.tailor'`

- [ ] **Step 3: Add `Bullet`/`TailorResult` to `models.py`**

In `src/career_agent/models.py`, after the `Verdict` class:

```python
class Bullet(BaseModel):
    text: str
    fact_ids: list[int] = Field(min_length=1)


class TailorResult(BaseModel):
    summary: str
    bullets: list[Bullet] = Field(min_length=1)
```

- [ ] **Step 4: Write `tailor.py`**

Create `src/career_agent/tailor.py`:

```python
import json
import re
from typing import Awaitable, Callable

from career_agent.config import CareerBrief
from career_agent.gate import MIN_FACTS_HARD, InsufficientFacts
from career_agent.models import Job, TailorResult

TAILOR_PROMPT_VERSION = "tailor-v1"

TEMPLATE = """You are tailoring resume content for one job, from a candidate's
verified facts only.

CAREER BRIEF
Target titles: {titles}

VERIFIED FACTS (numbered by id; cite only these ids, never state anything
not listed here)
{facts}

JOB
Company: {company}
Title: {title}
Description:
{description}

Select the facts most relevant to this job, then write:
- a 2-3 sentence professional summary tailored to this job, built only from
  the facts above
- a list of resume bullets, each rephrased from one or more of the facts
  above for clarity and relevance to this job -- never inventing a claim,
  metric, or skill that is not listed above

Every bullet must cite the id of every fact it draws from.

Reply with a single JSON object and nothing else:
{{"summary": "...", "bullets": [{{"text": "...", "fact_ids": [int, ...]}}]}}
"""


def build_prompt(job: Job, brief: CareerBrief, facts: list[tuple[int, str]]) -> str:
    facts_block = "\n".join(f"[{fid}] {text}" for fid, text in facts)
    return TEMPLATE.format(
        titles=", ".join(brief.target_titles),
        facts=facts_block,
        company=job.company,
        title=job.title,
        description=(job.description or "")[:6000],
    )


def parse_tailor_result(text: str) -> TailorResult:
    """Tolerate prose around the JSON; reject anything that is not a
    TailorResult (including zero bullets or a bullet with no fact_ids,
    both enforced by the model's own min_length constraints)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model output")
    try:
        return TailorResult(**json.loads(match.group(0)))
    except Exception as exc:
        raise ValueError(f"could not parse tailoring result: {exc}") from exc


def _validate_fact_ids(result: TailorResult, valid_ids: set[int]) -> None:
    for bullet in result.bullets:
        unknown = [fid for fid in bullet.fact_ids if fid not in valid_ids]
        if unknown:
            raise ValueError(f"bullet cites unknown fact id(s): {unknown}")


async def tailor(job: Job, brief: CareerBrief, facts: list[tuple[int, str]],
                 ask: Callable[[str], Awaitable[str]]) -> TailorResult:
    if len(facts) < MIN_FACTS_HARD:
        raise InsufficientFacts(
            f"{len(facts)} facts recorded, need at least {MIN_FACTS_HARD} to "
            "tailor a resume honestly.")

    valid_ids = {fid for fid, _ in facts}
    prompt = build_prompt(job, brief, facts)
    try:
        result = parse_tailor_result(await ask(prompt))
        _validate_fact_ids(result, valid_ids)
        return result
    except ValueError:
        retry = prompt + ("\n\nYour previous reply was invalid: either not "
                          "valid JSON, missing a required field, or citing a "
                          "fact id not listed above. Reply with ONLY a valid "
                          "JSON object, citing only the fact ids listed.")
        result = parse_tailor_result(await ask(retry))
        _validate_fact_ids(result, valid_ids)
        return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_tailor.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/career_agent/models.py src/career_agent/tailor.py tests/test_tailor.py
git commit -m "feat: add tailor.py's prompt/parse/validate core, mirroring gate.py"
```

---

## Task 3: `render_docx` — deterministic DOCX rendering, `python-docx` dependency

**Files:**
- Modify: `pyproject.toml:5-9` (dependencies list)
- Modify: `.gitignore:42-44` (project-specific section)
- Modify: `tests/conftest.py` (currently empty)
- Modify: `src/career_agent/tailor.py` (append)
- Test: `tests/test_tailor.py` (append)

**Interfaces:**
- Produces: `tailor.render_docx(template_path: Path, result: TailorResult, out_path: Path) -> None`; `conftest.build_tailor_template(path: Path) -> None` (test helper, shared with Tasks 6 and 7).

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, change the `dependencies` list to include `python-docx`:

```toml
dependencies = [
    "claude-agent-sdk", "httpx", "pydantic>=2", "apify-client",
    "playwright", "fastapi", "uvicorn[standard]", "jinja2", "python-dotenv",
    "python-multipart", "tomlkit", "python-docx",
]
```

Run: `pip install -e .`
Expected: `python-docx` installs cleanly.

- [ ] **Step 2: Gitignore the resume directory**

In `.gitignore`, under `# --- Project-specific ---`:

```
# --- Project-specific ---
*.db
screenshots/
resume/
```

`resume/` holds your personal master template and every generated tailored resume — identifying content, same treatment as `data/`.

- [ ] **Step 3: Add the shared test template builder**

Write `tests/conftest.py` (currently empty):

```python
import docx


def build_tailor_template(path):
    """A minimal DOCX carrying tailor.py's two marker paragraphs, built with
    python-docx rather than committed as a binary fixture -- keeps the
    fixture readable and diffable in the test files that use it."""
    doc = docx.Document()
    doc.add_paragraph("Sathish R -- AI Engineer")
    doc.add_paragraph("<<SUMMARY>>")
    doc.add_paragraph("<<PROJECT_BULLET>>", style="List Bullet")
    doc.add_paragraph("Education")
    doc.save(str(path))
```

- [ ] **Step 4: Write the failing tests**

Append to `tests/test_tailor.py`:

```python
from pathlib import Path

import docx

from conftest import build_tailor_template
from career_agent.models import Bullet, TailorResult
from career_agent.tailor import render_docx


def test_render_docx_replaces_markers_and_clones_bullets(tmp_path):
    template = tmp_path / "template.docx"
    build_tailor_template(template)

    result = TailorResult(summary="Tailored summary.", bullets=[
        Bullet(text="Bullet one", fact_ids=[1]),
        Bullet(text="Bullet two", fact_ids=[2]),
    ])
    out = tmp_path / "out.docx"
    render_docx(template, result, out)

    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    assert "<<SUMMARY>>" not in texts
    assert "<<PROJECT_BULLET>>" not in texts
    assert "Tailored summary." in texts
    assert "Bullet one" in texts
    assert "Bullet two" in texts
    # header/footer paragraphs outside the markers are untouched
    assert "Sathish R -- AI Engineer" in texts
    assert "Education" in texts


def test_render_docx_raises_on_a_template_missing_a_marker(tmp_path):
    template = tmp_path / "bad.docx"
    doc = docx.Document()
    doc.add_paragraph("<<SUMMARY>>")  # no <<PROJECT_BULLET>>
    doc.save(str(template))

    result = TailorResult(summary="S", bullets=[Bullet(text="B", fact_ids=[1])])
    with pytest.raises(ValueError):
        render_docx(template, result, tmp_path / "out.docx")


def test_render_docx_creates_missing_output_directories(tmp_path):
    template = tmp_path / "template.docx"
    build_tailor_template(template)
    result = TailorResult(summary="S", bullets=[Bullet(text="B", fact_ids=[1])])
    out = tmp_path / "nested" / "dir" / "out.docx"
    render_docx(template, result, out)
    assert out.exists()
```

Add `import pytest` to the top of `tests/test_tailor.py` if not already present from Task 2 (it is).

- [ ] **Step 5: Run tests to verify they fail**

Run: `pytest tests/test_tailor.py -k render_docx -v`
Expected: FAIL with `ImportError: cannot import name 'render_docx'`

- [ ] **Step 6: Append `render_docx` to `tailor.py`**

Append to `src/career_agent/tailor.py`:

```python
import copy
from pathlib import Path

import docx
from docx.text.paragraph import Paragraph


def _find_marker(doc, marker: str) -> Paragraph:
    for p in doc.paragraphs:
        if p.text.strip() == marker:
            return p
    raise ValueError(f"template is missing the {marker!r} marker paragraph")


def _set_text(paragraph: Paragraph, text: str) -> None:
    """Preserve the paragraph's first run's formatting; drop any others."""
    for run in paragraph.runs[1:]:
        run.text = ""
    if paragraph.runs:
        paragraph.runs[0].text = text
    else:
        paragraph.add_run(text)


def render_docx(template_path: Path, result: TailorResult, out_path: Path) -> None:
    """Deterministic, not the LLM: find the two marker paragraphs by exact
    text, replace <<SUMMARY>>'s text in place, and clone <<PROJECT_BULLET>>
    once per bullet before removing the marker itself. Everything else in
    the template -- header, contact info, education, layout -- is untouched."""
    doc = docx.Document(str(template_path))

    summary_para = _find_marker(doc, "<<SUMMARY>>")
    _set_text(summary_para, result.summary)

    bullet_marker = _find_marker(doc, "<<PROJECT_BULLET>>")
    for bullet in result.bullets:
        clone_el = copy.deepcopy(bullet_marker._p)
        bullet_marker._p.addprevious(clone_el)
        _set_text(Paragraph(clone_el, bullet_marker._parent), bullet.text)
    bullet_marker._p.getparent().remove(bullet_marker._p)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_tailor.py -v`
Expected: PASS. If paragraph cloning doesn't preserve formatting the way `_set_text` assumes, this is the implementation risk the design doc named explicitly — iterate on `_find_marker`/`_set_text`/the clone-and-insert sequence until the assertions above pass for real; don't weaken the test.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore tests/conftest.py src/career_agent/tailor.py tests/test_tailor.py
git commit -m "feat: render tailored content into a DOCX via marker paragraphs"
```

---

## Task 4: `ensure_tailored()` — tailor-once, render, and store a job's resume

**Files:**
- Modify: `src/career_agent/tailor.py` (append)
- Test: `tests/test_tailor.py` (append)

**Interfaces:**
- Consumes: `store.latest_resume_version`, `store.fact_rows`, `store.next_resume_version`, `store.insert_resume` (Task 1); `tailor.tailor`, `tailor.render_docx` (Tasks 2-3).
- Produces: `tailor.TEMPLATE_PATH: Path` and `tailor.OUTPUT_DIR: Path` (module-level, read at call time so tests can `monkeypatch.setattr(tailor, "TEMPLATE_PATH", ...)`); `async tailor.ensure_tailored(conn, job_id: int, job: Job, brief: CareerBrief, ask) -> str` — the one function both the dashboard's Apply button and the auto-mode worker loop call (wired in Tasks 6-7).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tailor.py`:

```python
import sqlite3

from career_agent import db, store, tailor


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized)"
              " VALUES ('fp1', 'ats', '1', 'Acme', 'acme', 'AI Engineer',"
              " 'aiengineer')")
    for i in range(10):
        c.execute("INSERT INTO fact (claim, evidence) VALUES (?, ?)",
                  (f"claim {i}", f"evidence {i}"))
    c.commit()
    return c


async def _ask(_prompt):
    return json.dumps({"summary": "S", "bullets": [
        {"text": "B1", "fact_ids": [1]}]})


def test_ensure_tailored_creates_a_resume_row_and_file(tmp_path, conn, monkeypatch):
    template = tmp_path / "master.docx"
    build_tailor_template(template)
    monkeypatch.setattr(tailor, "TEMPLATE_PATH", template)
    monkeypatch.setattr(tailor, "OUTPUT_DIR", tmp_path / "generated")

    version = await tailor.ensure_tailored(conn, 1, JOB, BRIEF, _ask)

    assert version == "tailored-1-r1"
    row = conn.execute("SELECT * FROM resume WHERE version = ?",
                       (version,)).fetchone()
    assert row["job_id"] == 1
    assert Path(row["path"]).exists()
    content = json.loads(row["content"])
    assert content["bullets"][0]["fact_ids"] == [1]


async def test_ensure_tailored_reuses_an_existing_version(conn):
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'x.docx', 1)")
    conn.commit()

    async def must_not_call(_prompt):
        raise AssertionError("must not tailor again")

    version = await tailor.ensure_tailored(conn, 1, JOB, BRIEF, must_not_call)
    assert version == "tailored-1-r1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tailor.py -k ensure_tailored -v`
Expected: FAIL with `AttributeError: module 'career_agent.tailor' has no attribute 'ensure_tailored'`

- [ ] **Step 3: Append `ensure_tailored` to `tailor.py`**

Add `from career_agent import store` to the imports at the top of
`src/career_agent/tailor.py` (`store.py` does not import `tailor.py`, so
this does not cycle — `tailor` only needs `store` for this one
persistence-facing function; `tailor()`/`render_docx` above it stay
decoupled from persistence, the same way `gate.score` stays decoupled by
taking `ask` as a parameter instead of reaching for it itself).

Append to `src/career_agent/tailor.py`:

```python
TEMPLATE_PATH = Path("resume/master.docx")
OUTPUT_DIR = Path("resume/generated")


async def ensure_tailored(conn, job_id: int, job: Job, brief: CareerBrief,
                          ask: Callable[[str], Awaitable[str]]) -> str:
    """Tailor and render this job's resume if one doesn't exist yet, else
    reuse the most recent version -- there is no "Retailor" action. Reads
    TEMPLATE_PATH/OUTPUT_DIR as module attributes (not function defaults)
    so tests can monkeypatch them without needing to reload this module."""
    existing = store.latest_resume_version(conn, job_id)
    if existing:
        return existing

    result = await tailor(job, brief, store.fact_rows(conn), ask)

    version = store.next_resume_version(conn, job_id)
    out_path = OUTPUT_DIR / f"{version}.docx"
    render_docx(TEMPLATE_PATH, result, out_path)

    content = json.dumps({"summary": result.summary,
                          "bullets": [b.model_dump() for b in result.bullets]})
    return store.insert_resume(conn, job_id, version, str(out_path), content)
```

Note the local `import` inside the function: `store.py` imports `career_agent.apply.ats`, and if `tailor.py` imported `store` at module level while something in `store`'s import chain ever imported `tailor`, that would cycle. Nothing does today, but keeping the `store` dependency local to this one orchestration function (rather than importing it at module level like `gate.py`'s `ask`-only design) keeps `tailor()` itself — the trust-sensitive core — fully decoupled from persistence, exactly like `gate.score`'s.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tailor.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/career_agent/tailor.py tests/test_tailor.py
git commit -m "feat: add ensure_tailored, the tailor-once-then-reuse orchestration"
```

---

## Task 5: Thread `resume_version` through `ats.submit()` and `store.mark_applied()`

**Files:**
- Modify: `src/career_agent/apply/ats.py:57-93` (`submit`)
- Modify: `src/career_agent/store.py:103-139` (`mark_applied`)
- Test: `tests/test_apply_ats.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `ats.submit(conn, job_id, dry_run, filler=None, resume_version: str | None = None) -> dict` — an application row is now stored with the given `resume_version`, defaulting to the existing `RESUME_VERSION` constant when the caller doesn't pass one (keeps every current caller's behavior unchanged until Tasks 6-7 start passing a real value).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_apply_ats.py`:

```python
async def test_dry_run_stores_the_given_resume_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, filler=_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == "tailored-1-r1"


async def test_dry_run_falls_back_to_the_default_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, filler=_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == ats_apply.RESUME_VERSION


async def test_real_submission_stores_the_given_resume_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == "tailored-1-r1"
```

Append to `tests/test_store.py`, after `test_mark_applied_refuses_a_job_that_already_has_one`:

```python
def test_mark_applied_uses_the_tailored_resume_when_one_exists(conn):
    job_id = _seed_one(conn)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'x.docx', ?)", (job_id,))
    conn.commit()

    store.mark_applied(conn, job_id, "2026-08-22")

    row = conn.execute("SELECT resume_version FROM application"
                       " WHERE job_id = ?", (job_id,)).fetchone()
    assert row["resume_version"] == "tailored-1-r1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_apply_ats.py -k resume_version -v`
Expected: FAIL — `submit()` raises `TypeError: submit() got an unexpected keyword argument 'resume_version'`.

Run: `pytest tests/test_store.py -k mark_applied_uses_the_tailored -v`
Expected: FAIL — `mark_applied` currently always stores `ats.RESUME_VERSION`, so the assertion fails.

- [ ] **Step 3: Update `ats.submit()`**

In `src/career_agent/apply/ats.py`, change the `submit` signature and both `INSERT INTO application` calls:

```python
async def submit(conn: sqlite3.Connection, job_id: int, dry_run: bool,
                 filler: Callable[[str], Awaitable[dict]] | None = None,
                 resume_version: str | None = None) -> dict:
```

Right after the existing `filler = filler or _default_filler` line, add:

```python
    resume_version = resume_version or RESUME_VERSION
```

Then change the `dry_run` branch's insert:

```python
        conn.execute(
            "INSERT INTO application (job_id, resume_version, answers, status)"
            " VALUES (?, ?, ?, 'draft')",
            (job_id, resume_version, json.dumps(answers)))
```

And the real-submission insert:

```python
    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, resume_version))
```

(Both previously used the module constant `RESUME_VERSION` directly; both now use the local `resume_version` variable.)

- [ ] **Step 4: Update `store.mark_applied()`**

In `src/career_agent/store.py`, in the `draft is None` branch of `mark_applied`, change:

```python
        cur = conn.execute(
            "INSERT INTO application (job_id, resume_version, status,"
            " submitted_at) VALUES (?, ?, 'submitted', ?)",
            (job_id, ats.RESUME_VERSION, when))
```

to:

```python
        cur = conn.execute(
            "INSERT INTO application (job_id, resume_version, status,"
            " submitted_at) VALUES (?, ?, 'submitted', ?)",
            (job_id, resume_version_for(conn, job_id), when))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_apply_ats.py tests/test_store.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: PASS — `test_mark_applied_inserts_when_there_is_no_draft` still asserts `row["resume_version"] == ats_apply.RESUME_VERSION`, and still passes because that test's `conn` has no `resume` row for the job, so `resume_version_for` falls back exactly as before.

- [ ] **Step 7: Commit**

```bash
git add src/career_agent/apply/ats.py src/career_agent/store.py tests/test_apply_ats.py tests/test_store.py
git commit -m "feat: thread resume_version through ats.submit and mark_applied"
```

---

## Task 6: `worker.tailor_for_apply()` — shared trigger, wired into auto-mode's `apply_tick`

**Files:**
- Modify: `src/career_agent/web/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `tailor.ensure_tailored` (Task 4), `run._ask`/`run.verify_auth`/`run._row_to_job` (existing, in `career_agent/run.py`), `store.get_settings`/`store.resume_version_for` (existing/Task 1).
- Produces: `async worker.tailor_for_apply(conn, job_id: int, brief_path: Path) -> str` — the one function that turns "a job is about to be drafted" into "this job has a tailored resume, here's its version." Called from `apply_tick` here, and from `app.py`'s `_do_apply` in Task 7 — one implementation, two callers, so a future fix only has one place to land.

Why this lives in `worker.py` and not duplicated in `app.py`: `app.py` already imports `worker` for `guard`/`get_run_state`/etc., so adding one more shared function there costs nothing new, and both draft-creation sites (the dashboard's Apply button and the unattended auto-mode loop) end up calling the exact same tailoring path instead of two independently-maintained copies.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_worker.py`, near the top (after the existing fixtures, before the first test):

```python
import json

from career_agent import tailor
from conftest import build_tailor_template


@pytest.fixture(autouse=True)
def stub_tailoring(monkeypatch, tmp_path):
    """apply_tick now tailors before drafting. Default every test to a safe,
    deterministic tailoring path -- no real LLM call, no
    CLAUDE_CODE_OAUTH_TOKEN dependency -- so tests that only care about the
    apply-queue state machine keep working unchanged."""
    template = tmp_path / "master.docx"
    build_tailor_template(template)
    monkeypatch.setattr(tailor, "TEMPLATE_PATH", template)
    monkeypatch.setattr(tailor, "OUTPUT_DIR", tmp_path / "generated")
    monkeypatch.setattr(worker.run_module, "verify_auth", lambda: None)

    async def _default_ask(prompt, model=None):
        return json.dumps({"summary": "Tailored summary.",
                           "bullets": [{"text": "Relevant bullet",
                                       "fact_ids": [1]}]})
    monkeypatch.setattr(worker.run_module, "_ask", _default_ask)
```

Change the existing `conn` fixture to seed facts (needed once `apply_tick` tailors for real by default):

```python
@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    for i in range(10):
        c.execute("INSERT INTO fact (claim, evidence) VALUES (?, ?)",
                  (f"claim {i}", f"evidence {i}"))
    c.commit()
    return c
```

Add a new test, anywhere after the fixtures:

```python
async def test_apply_tick_tailors_before_drafting_and_threads_the_version(
        conn, brief_path, monkeypatch):
    _job(conn, "fp1")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    captured = {}

    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):
        captured["resume_version"] = resume_version
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path)

    assert captured["resume_version"] == "tailored-1-r1"
    row = conn.execute("SELECT * FROM resume WHERE version = ?",
                       (captured["resume_version"],)).fetchone()
    assert row is not None


async def test_apply_tick_reuses_an_already_tailored_resume(conn, brief_path,
                                                             monkeypatch):
    job_id = _job(conn, "fp1")
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', 'x.docx', ?)", (job_id,))
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    async def must_not_tailor(prompt, model=None):
        raise AssertionError("must not tailor again")

    monkeypatch.setattr(worker.run_module, "_ask", must_not_tailor)

    captured = {}

    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):
        captured["resume_version"] = resume_version
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path)

    assert captured["resume_version"] == "tailored-1-r1"
```

- [ ] **Step 2: Fix the existing `fake_submit` fakes' signatures**

Use Edit with `replace_all: true` on `tests/test_worker.py`:

old_string: `    async def fake_submit(conn, job_id, dry_run, filler=None):`
new_string: `    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):`

This matches all 3 existing occurrences in the file.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -v`
Expected: The two new tests FAIL with `AttributeError: module 'career_agent.web.worker' has no attribute 'tailor_for_apply'` (or `run_module` not found yet). Other existing tests should still pass at this point — `stub_tailoring` and the seeded facts don't change behavior until `apply_tick` actually calls tailoring.

- [ ] **Step 4: Add `tailor_for_apply` and wire it into `apply_tick`**

In `src/career_agent/web/worker.py`, update the imports at the top:

```python
import asyncio
import functools
import sqlite3
from pathlib import Path

from career_agent import run as run_module
from career_agent import store, tailor
from career_agent.apply import ats as ats_apply
from career_agent.config import load_brief
```

Add, after `queue_count` is fine, or anywhere before `apply_tick` — place it right before `apply_tick`:

```python
async def tailor_for_apply(conn: sqlite3.Connection, job_id: int,
                           brief_path: Path) -> str:
    """Tailor and render this job's resume if one doesn't exist yet, else
    reuse the most recent version. Shared by the dashboard's Apply button
    (app.py's _do_apply) and this module's apply_tick -- both create a
    draft the same way, so both need the same resume behind it."""
    run_module.verify_auth()
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    job = run_module._row_to_job(row)
    brief = load_brief(brief_path)
    settings = store.get_settings(conn)
    ask = functools.partial(run_module._ask, model=settings["scoring_model"])
    return await tailor.ensure_tailored(conn, job_id, job, brief, ask)
```

In `apply_tick`, replace the first `submit` call:

```python
    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return
```

with:

```python
    try:
        resume_version = await tailor_for_apply(conn, job_id, brief_path)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True,
                                        resume_version=resume_version)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return
```

And the second `submit` call (real send, `dry_run=False`), a few lines later:

```python
    try:
        result = await ats_apply.submit(conn, job_id, dry_run=False,
                                        resume_version=store.resume_version_for(
                                            conn, job_id))
    except Exception as exc:
```

(previously `await ats_apply.submit(conn, job_id, dry_run=False)`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/career_agent/web/worker.py tests/test_worker.py
git commit -m "feat: tailor a job's resume before drafting it, in both apply paths"
```

---

## Task 7: Wire `worker.tailor_for_apply` into the dashboard's Apply button

**Files:**
- Modify: `src/career_agent/web/app.py:159-174` (`_do_apply`), `src/career_agent/web/app.py:198-229` (`send`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `worker.tailor_for_apply` (Task 6), `store.resume_version_for` (Task 1).

- [ ] **Step 1: Update the `client` fixture and fix existing `fake_submit`/etc. signatures**

In `tests/test_web.py`, add to the imports at the top:

```python
import json
```

(`datetime as dt`, `threading`, `time`, `Path`, `pytest`, `TestClient`, and the `career_agent` imports are already there.)

Add, right after the existing imports:

```python
from conftest import build_tailor_template
```

Update the `client` fixture — after the existing `conn.commit()` / before `monkeypatch.setattr(web, "DB_PATH", path)`, seed facts, and after that line, stub tailoring:

```python
@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    db.init_schema(conn)
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp1','ats','1','Acme','acme','AI Engineer',"
                 " 'aiengineer','https://x/1')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (1,'scored',90,90,90,90,90,90,'submit',"
                 " 'strong match','m','gate-v1')")
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized, url)"
                 " VALUES ('fp2','ats','2','Globex','globex','ML Engineer',"
                 " 'engineerml','https://x/2')")
    conn.execute("INSERT INTO assessment (job_id, stage, role_fit, credibility,"
                 " opportunity, application_quality, eligibility_soft,"
                 " weighted_score, verdict, rationale, model, prompt_version)"
                 " VALUES (2,'scored',80,50,80,80,80,72,'skip',"
                 " 'credibility below floor','m','gate-v1')")
    for i in range(10):
        conn.execute("INSERT INTO fact (claim, evidence) VALUES (?, ?)",
                     (f"claim {i}", f"evidence {i}"))
    conn.commit()
    monkeypatch.setattr(web, "DB_PATH", path)

    # Every /apply and /override route now tailors before drafting. Default
    # every test to a safe, deterministic tailoring path -- no real LLM
    # call, no CLAUDE_CODE_OAUTH_TOKEN dependency -- so tests that only care
    # about the apply/send state machine get real tailoring for free.
    template = tmp_path / "master.docx"
    build_tailor_template(template)
    monkeypatch.setattr(web.tailor, "TEMPLATE_PATH", template)
    monkeypatch.setattr(web.tailor, "OUTPUT_DIR", tmp_path / "generated")
    monkeypatch.setattr(web.worker.run_module, "verify_auth", lambda: None)

    async def _default_ask(prompt, model=None):
        return json.dumps({"summary": "Tailored summary.",
                           "bullets": [{"text": "Relevant bullet",
                                       "fact_ids": [1]}]})
    monkeypatch.setattr(web.worker.run_module, "_ask", _default_ask)

    return TestClient(web.app)
```

Use Edit with `replace_all: true` on `tests/test_web.py`:

old_string: `    async def fake_submit(conn, job_id, dry_run, filler=None):`
new_string: `    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):`

This matches all 14 existing occurrences in the file. (`boom`, `denied`, and `spy` already use `*args, **kwargs` and need no change.)

- [ ] **Step 2: Write the new route-level test**

Add anywhere after the `client` fixture:

```python
def test_apply_tailors_before_drafting_and_threads_the_version(client, monkeypatch):
    captured = {}

    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):
        captured["resume_version"] = resume_version
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)

    r = client.post("/apply/1")
    assert r.status_code == 200
    assert captured["resume_version"] == "tailored-1-r1"

    conn = db.connect(web.DB_PATH)
    row = conn.execute("SELECT * FROM resume WHERE version = ?",
                       (captured["resume_version"],)).fetchone()
    assert row is not None
    assert Path(row["path"]).exists()


def test_a_captcha_hold_does_not_lose_the_already_tailored_resume(client, monkeypatch):
    """Ordering matters: tailoring is committed before ats_apply.submit is
    even called, so a captcha (or any submission failure) afterward must not
    make the resume row or file disappear."""
    async def held(*args, **kwargs):
        return {"ok": False, "held": True, "reason": "captcha encountered"}

    monkeypatch.setattr(web.ats_apply, "submit", held)

    client.post("/apply/1")

    conn = db.connect(web.DB_PATH)
    row = conn.execute("SELECT * FROM resume WHERE job_id = 1").fetchone()
    assert row is not None
    assert Path(row["path"]).exists()


def test_two_near_simultaneous_apply_clicks_converge_on_one_resume(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)

    r1 = client.post("/apply/1")
    r2 = client.post("/apply/1")
    assert r1.status_code == 200 and r2.status_code == 200

    conn = db.connect(web.DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) n FROM resume WHERE job_id = 1").fetchone()["n"]
    assert count == 1, "the second click must reuse the first click's resume"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_web.py -v`
Expected: The three new tests FAIL — `_do_apply` doesn't call any tailoring yet, so `captured["resume_version"]` is never set / `resume` stays empty.

- [ ] **Step 4: Wire tailoring into `_do_apply` and `send`**

In `src/career_agent/web/app.py`, replace `_do_apply`:

```python
async def _do_apply(job_id: int, allow_skip: bool, event: str | None):
    conn = _conn()
    denial = worker.guard(conn, job_id, allow_skip=allow_skip, brief_path=BRIEF_PATH)
    if denial:
        return HTMLResponse(f'<span class="denied">{denial}</span>')

    if event:
        store.log(conn, job_id, event)

    try:
        resume_version = await worker.tailor_for_apply(conn, job_id, BRIEF_PATH)
    except Exception as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True,
                                        resume_version=resume_version)
    except Exception as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')
    if not result["ok"]:
        return HTMLResponse(f'<span class="denied">{escape(result["reason"])}</span>')
    return HTMLResponse('<span class="done">Applied</span>')
```

And in `send`, replace:

```python
    try:
        result = await ats_apply.submit(conn, job_id, dry_run=False)
    except Exception as exc:
```

with:

```python
    try:
        result = await ats_apply.submit(
            conn, job_id, dry_run=False,
            resume_version=store.resume_version_for(conn, job_id))
    except Exception as exc:
```

`app.py` already imports `store` and `worker` at the top (`from career_agent import db, outcomes, store` and `from career_agent.web import overview, pipeline, worker`) — no new imports needed for this task. (`web.tailor` used by the test fixture is imported in Task 8, needed there for the download route; add `from career_agent import tailor` to `app.py`'s imports now so `web.tailor` resolves in the fixture — see Step 5.)

- [ ] **Step 5: Add the `tailor` import to `app.py`**

Add to the top of `src/career_agent/web/app.py`, alongside the existing `from career_agent import db, outcomes, store` line:

```python
from career_agent import db, outcomes, store, tailor
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/career_agent/web/app.py tests/test_web.py
git commit -m "feat: tailor before drafting on the dashboard's Apply/Override buttons"
```

---

## Task 8: `/resume/{version}` download route, and a link on the Applications page

**Files:**
- Modify: `src/career_agent/web/app.py` (`LIST_SQL`, new route)
- Modify: `src/career_agent/web/templates/applications.html` (`action_cell` macro)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /resume/{version}` — serves the file at `resume.path` for that version, or a 404 if the version doesn't exist. `LIST_SQL` gains a `resume_version` column on every job row.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_resume_download_serves_the_file(client, tmp_path):
    resume_file = tmp_path / "r.docx"
    resume_file.write_bytes(b"fake docx bytes")
    conn = db.connect(web.DB_PATH)
    conn.execute("INSERT INTO resume (version, path, job_id)"
                 " VALUES ('tailored-1-r1', ?, 1)", (str(resume_file),))
    conn.commit()

    r = client.get("/resume/tailored-1-r1")
    assert r.status_code == 200
    assert r.content == b"fake docx bytes"


def test_resume_download_404s_on_an_unknown_version(client):
    r = client.get("/resume/does-not-exist")
    assert r.status_code == 404


def test_applications_page_links_to_a_tailored_resume(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/apply/1")

    r = client.get("/applications")
    assert 'href="/resume/tailored-1-r1"' in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k resume_download -v tests/test_web.py -k links_to_a_tailored -v`
Expected: FAIL — `/resume/{version}` doesn't exist (404 for the wrong reason / `RuntimeError` from FastAPI routing), and the Applications page has no such link yet.

- [ ] **Step 3: Add the `resume_version` column to `LIST_SQL`**

In `src/career_agent/web/app.py`, update `LIST_SQL`:

```python
LIST_SQL = """
SELECT j.id, j.company, j.title, j.location, j.source, j.url,
       a.verdict, a.rationale, a.stage, a.weighted_score AS score,
       (SELECT ap.status FROM application ap
         WHERE ap.job_id = j.id
           AND ap.status IN ('in_flight','submitted','held_unknown','failed_permanent')
         ORDER BY ap.id DESC LIMIT 1) AS terminal_status,
       (SELECT ap.status FROM application ap WHERE ap.job_id = j.id
         ORDER BY ap.id DESC LIMIT 1) IS 'draft' AS has_draft,
       (SELECT ap.resume_version FROM application ap WHERE ap.job_id = j.id
         ORDER BY ap.id DESC LIMIT 1) AS resume_version
  FROM job j JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL AND a.verdict IN ({placeholders})
 ORDER BY a.weighted_score DESC NULLS LAST, j.discovered_at DESC
"""
```

- [ ] **Step 4: Add the download route**

In `src/career_agent/web/app.py`, add `FileResponse` to the FastAPI import:

```python
from fastapi.responses import FileResponse, HTMLResponse
```

Add the route (anywhere after `_conn`, e.g. right before `@app.get("/applications", ...)`):

```python
@app.get("/resume/{version}")
def download_resume(version: str):
    conn = _conn()
    row = conn.execute("SELECT path FROM resume WHERE version = ?",
                       (version,)).fetchone()
    if row is None:
        return HTMLResponse("Resume not found", status_code=404)
    return FileResponse(row["path"], filename=Path(row["path"]).name)
```

- [ ] **Step 5: Add the link to `applications.html`**

In `src/career_agent/web/templates/applications.html`, at the top of the `action_cell` macro:

```html
{% macro action_cell(j) %}
  {% if j["resume_version"] and j["resume_version"].startswith("tailored-") %}
  <div><a href="/resume/{{ j['resume_version'] }}" target="_blank">Download resume</a></div>
  {% endif %}
  {% set tracked = applied.get(j["id"]) %}
```

(everything else in the macro is unchanged).

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/applications.html tests/test_web.py
git commit -m "feat: add a resume download route and a link on the Applications page"
```

---

## Task 9: `/resumes` page — master template status and full version history

**Files:**
- Modify: `src/career_agent/web/app.py` (new route)
- Create: `src/career_agent/web/templates/resumes.html`
- Modify: `src/career_agent/web/templates/base.html:198-205` (nav)
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /resumes` — renders `resumes.html` with the master template's status and every generated resume, grouped by job, each bullet's fact provenance included.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_resumes_page_shows_no_master_when_none_exists(client, monkeypatch):
    monkeypatch.setattr(web.tailor, "TEMPLATE_PATH", Path("/no/such/file.docx"))
    r = client.get("/resumes")
    assert r.status_code == 200
    assert "No master template found" in r.text


def test_resumes_page_shows_the_master_when_present(client, monkeypatch, tmp_path):
    template = tmp_path / "master.docx"
    build_tailor_template(template)
    monkeypatch.setattr(web.tailor, "TEMPLATE_PATH", template)
    r = client.get("/resumes")
    assert "master.docx" in r.text


def test_resumes_page_lists_generated_versions_with_provenance(client, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, filler=None, resume_version=None):
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(web.ats_apply, "submit", fake_submit)
    client.post("/apply/1")

    r = client.get("/resumes")
    assert "AI Engineer" in r.text  # job title
    assert "Acme" in r.text          # job company
    assert "tailored-1-r1" in r.text
    assert "Relevant bullet" in r.text  # the fixture's default tailored bullet
    assert "fact 1" in r.text.lower()   # provenance: which fact backs it


def test_resumes_nav_link_is_present(client):
    r = client.get("/applications")
    assert 'href="/resumes"' in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k resumes_page -v tests/test_web.py -k resumes_nav -v`
Expected: FAIL — `/resumes` doesn't exist, and `base.html` has no such link.

- [ ] **Step 3: Add the `/resumes` route**

In `src/career_agent/web/app.py`, add `import json` and `import datetime as dt` is already present. Add the route (anywhere after the download route from Task 8):

```python
@app.get("/resumes", response_class=HTMLResponse)
async def resumes_page(request: Request):
    conn = _conn()
    brief = load_brief(BRIEF_PATH)

    master_path = tailor.TEMPLATE_PATH
    master = {"exists": master_path.exists(), "name": master_path.name}
    if master["exists"]:
        master["modified"] = dt.datetime.fromtimestamp(
            master_path.stat().st_mtime).isoformat(timespec="seconds")

    versions = []
    for r in conn.execute(
            "SELECT r.version, r.created_at, r.content, j.company, j.title"
            "  FROM resume r JOIN job j ON j.id = r.job_id"
            " ORDER BY r.created_at DESC"):
        content = json.loads(r["content"]) if r["content"] else {}
        versions.append({**dict(r), "summary": content.get("summary", ""),
                         "bullets": content.get("bullets", [])})

    return templates.TemplateResponse(
        request=request, name="resumes.html",
        context={"active_nav": "resumes", "brief": brief,
                 "daily_cap": brief.daily_cap,
                 "today_submitted": conn.execute(
                     "SELECT COUNT(*) n FROM application WHERE status ="
                     " 'submitted' AND date(submitted_at) = date('now')"
                 ).fetchone()["n"],
                 "master": master, "versions": versions})
```

(`today_submitted` and `brief`/`daily_cap` are needed because `base.html`'s sidebar always renders the "Today's Progress" and "Career Brief" cards — every other page route provides them, so this one must too.)

- [ ] **Step 4: Create `resumes.html`**

Create `src/career_agent/web/templates/resumes.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Resumes</h1>

<div class="card">
  <div class="brief-head"><span>Primary (master) resume</span></div>
  {% if master.exists %}
  <div class="kv">
    <div><span>File</span><b>{{ master.name }}</b></div>
    <div><span>Last modified</span><b>{{ master.modified }}</b></div>
  </div>
  {% else %}
  <p class="rationale">
    No master template found at {{ master.name }}. Add your resume there
    (see the design doc for the &lt;&lt;SUMMARY&gt;&gt; /
    &lt;&lt;PROJECT_BULLET&gt;&gt; marker convention) before tailoring can run.
  </p>
  {% endif %}
</div>

<h2>Tailored versions</h2>
{% if not versions %}
<p class="rationale">
  No tailored resumes generated yet — the first one is created the first
  time you click Apply on a job.
</p>
{% else %}
<table>
  <tr><th>Job</th><th>Version</th><th>Generated</th><th>Bullets</th><th></th></tr>
  {% for v in versions %}
  <tr>
    <td>{{ v["title"] }} at {{ v["company"] }}</td>
    <td>{{ v["version"] }}</td>
    <td>{{ v["created_at"] }}</td>
    <td>
      {% for b in v["bullets"] %}
      <div class="rationale">
        {{ b["text"] }} <i>(fact {{ b["fact_ids"] | join(", ") }})</i>
      </div>
      {% endfor %}
    </td>
    <td><a href="/resume/{{ v['version'] }}" target="_blank">Download</a></td>
  </tr>
  {% endfor %}
</table>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Add the nav entry**

In `src/career_agent/web/templates/base.html`, change:

```html
      <a href="/applications" {% if active_nav == "applications" %}class="active"{% endif %}>Applications</a>
      <a href="#">Pipeline</a>
```

to:

```html
      <a href="/applications" {% if active_nav == "applications" %}class="active"{% endif %}>Applications</a>
      <a href="/resumes" {% if active_nav == "resumes" %}class="active"{% endif %}>Resumes</a>
      <a href="#">Pipeline</a>
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/resumes.html src/career_agent/web/templates/base.html tests/test_web.py
git commit -m "feat: add the Resumes page (master status + full tailored-version history)"
```

---

## Final check: set up your real master template

The plan above is fully covered by automated tests using a fixture template built with `python-docx`. Before using the feature for real:

1. Copy your resume to `resume/master.docx` (create the `resume/` directory first — it's gitignored, so this step is not a commit).
2. Open it and replace the line(s) you want tailored with the literal marker text `<<SUMMARY>>` (one paragraph) and `<<PROJECT_BULLET>>` (one bullet-styled paragraph — its formatting is what every generated bullet clones).
3. Run `career-agent serve`, open a `submit`/`hold` job on the Applications page, and click Apply. Check `resume/generated/` for the rendered file and the new `/resumes` page for its listing.

## Self-review

**Spec coverage**: every section of `docs/superpowers/specs/2026-08-22-per-role-tailoring-design.md` maps to a task — data model (Task 1), tailoring core + retry/validation (Task 2), rendering (Task 3), orchestration/reuse (Task 4), `ats.submit`/`mark_applied` plumbing (Task 5), the trigger point in both the manual and auto-mode apply paths (Tasks 6-7), the download route + Applications link (Task 8), and the Resumes page (Task 9). The spec's browser-launch-ordering fix, the concurrent-click race, render-failure isolation, and the fact-id/zero-bullet checks are each covered by a named test in Tasks 2-3 and 7.

**Beyond the literal spec, flagged explicitly**: the spec only names `_do_apply` as the trigger point. Tasks 6-7 also wire the identical tailoring step into `worker.apply_tick` (the auto-mode loop), because it creates drafts through the exact same `ats_apply.submit(dry_run=True)` call `_do_apply` does — leaving it out would mean auto-mode silently never tailors anything. This is called out here rather than silently expanded.

**Type/interface consistency**: `tailor()`'s signature (`job, brief, facts, ask`) is identical across Task 2's definition, Task 2/4's tests, and Task 4's `ensure_tailored`. `ensure_tailored(conn, job_id, job, brief, ask)` is identical across Task 4's definition and both Task 6/7 callers. `resume_version` flows as an optional keyword with the same default-fallback behavior through `ats.submit` (Task 5), `store.mark_applied`/`resume_version_for` (Tasks 1 and 5), and both wiring tasks (6-7). The `fake_submit(conn, job_id, dry_run, filler=None, resume_version=None)` signature is applied identically (via `replace_all`) everywhere it's needed.
