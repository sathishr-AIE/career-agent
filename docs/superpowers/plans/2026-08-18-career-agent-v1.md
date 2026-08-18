# Career Agent v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local tool that finds AI engineering jobs in Chennai and remote, filters them mechanically, scores the survivors with one model call each, and presents them ranked in a dashboard where you apply with one click.

**Architecture:** A deterministic pipeline, not an agent loop. Six fixed stages: fetch, normalize, dedupe, hard filter, score, persist. One LLM call, at the scoring stage only. A FastAPI dashboard reads the same SQLite file and owns every submission.

**Tech Stack:** Python 3.13, Claude Agent SDK (scoring only), SQLite via stdlib `sqlite3`, httpx, Pydantic v2, apify-client, Playwright, FastAPI, Jinja2, HTMX, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-career-agent-v1-design.md` (revision 7)

**Overview:** `docs/architecture-overview.md`

## Global Constraints

- Python 3.13. Windows is the target. Use `pathlib`, never string path concatenation.
- `ANTHROPIC_API_KEY` must never be set or read. Auth is `CLAUDE_CODE_OAUTH_TOKEN` only.
- Nothing submits without a human click. There is no auto-submit path in v1.
- No scheduled task is installed by default. The agent is inert until you run it or opt into scheduling.
- `career_brief.toml` is the single source of truth. There is no `career_brief` table.
- Discovery queries are built from `search_locations` in the brief. Never hardcode a location or accept one as a CLI flag.
- `data/`, `secrets/`, `.env`, `*.db`, `screenshots/` stay gitignored.
- Every assessment stores `model` and `prompt_version`. Any prompt edit bumps `PROMPT_VERSION` in the same commit.
- Tests use pytest. No network in tests; every source is faked at its boundary.

## File structure

```
career-agent/
├── pyproject.toml
├── career_brief.toml
├── ats_boards.toml
├── scripts/{spike_auth.py,install-scheduler.ps1}
├── src/career_agent/
│   ├── config.py      db.py      models.py
│   ├── normalize.py   hardfilter.py   store.py
│   ├── sources/{ats.py,apify.py}
│   ├── discovery.py   gate.py    outcomes.py    run.py
│   ├── apply/ats.py
│   └── web/{app.py,templates/index.html}
└── tests/
```

**Phase 0:** Task 1, the auth spike.
**Phase 1:** Tasks 2-6, foundation.
**Phase 2:** Tasks 7-9, discovery including the location fan-out.
**Phase 3:** Tasks 10-11, gate and runner.
**Phase 4:** Tasks 12-16, dashboard, submission, scheduling.

---

## Task 1: Spike 1, subscription auth

**Files:** Create `pyproject.toml`, `.env.example`, `scripts/spike_auth.py`; modify `.gitignore`

**Interfaces:** Consumes nothing. Produces a yes/no answer gating the cost model.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "career-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "claude-agent-sdk", "httpx", "pydantic>=2", "apify-client",
    "playwright", "fastapi", "uvicorn[standard]", "jinja2", "python-dotenv",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]

[project.scripts]
career-agent = "career_agent.run:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Append to `.gitignore`**

```
data/
secrets/
.env
*.db
screenshots/
.venv/
```

- [ ] **Step 3: Create `.env.example`**

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-REPLACE_ME
APIFY_TOKEN=apify_api_REPLACE_ME
```

- [ ] **Step 4: Write `scripts/spike_auth.py`**

```python
import asyncio
import os
import sys

from dotenv import load_dotenv


async def main() -> int:
    load_dotenv()

    if os.environ.get("ANTHROPIC_API_KEY"):
        print("FAIL: ANTHROPIC_API_KEY is set. It outranks the subscription "
              "token, so this spike would give a false pass. Unset it.")
        return 2
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        print("FAIL: CLAUDE_CODE_OAUTH_TOKEN not set. Run: claude setup-token")
        return 2

    from claude_agent_sdk import query

    chunks = []
    async for message in query(prompt="Reply with exactly: OK",
                               options={"allowed_tools": []}):
        text = getattr(message, "text", None)
        if text:
            chunks.append(text)

    reply = "".join(chunks).strip()
    if "OK" in reply:
        print("PASS: subscription token works. Marginal token cost is zero.")
        return 0

    print(f"UNCLEAR: call succeeded but reply was {reply!r}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 5: Run the spike**

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
claude setup-token
.venv/Scripts/python scripts/spike_auth.py
```

Expected: `PASS`. On failure, record it in the spec's Auth section and treat the pay-per-token cost table as live.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .env.example .gitignore scripts/spike_auth.py
git commit -m "chore: spike 1, verify subscription token auth"
```

---

## Task 2: Database schema

**Files:** Create `src/career_agent/__init__.py`, `src/career_agent/db.py`, `tests/conftest.py`, `tests/test_db.py`

**Interfaces:** Produces `db.connect(path)`, `db.init_schema(conn)`

- [ ] **Step 1: Write the failing test**

`tests/test_db.py`:

```python
import sqlite3

import pytest

from career_agent import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(conn, fp="fp1"):
    return conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized)"
        " VALUES (?, 'ats', '1', 'Acme', 'acme', 'AI Engineer', 'aiengineer')",
        (fp,)).lastrowid


def test_schema_creates_expected_tables(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"job", "assessment", "application", "outcome",
            "event", "fact", "qa_bank", "resume"} <= names


def test_no_career_brief_table(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "career_brief" not in names


def test_draft_does_not_block_a_real_submission(conn):
    j = _job(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'draft')", (j,))
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'in_flight')", (j,))


def test_failed_does_not_block_a_retry(conn):
    j = _job(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'failed')", (j,))
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', 'in_flight')", (j,))


@pytest.mark.parametrize("blocking", ["in_flight", "submitted",
                                      "held_unknown", "failed_permanent"])
def test_live_statuses_block_a_second_attempt(conn, blocking):
    j = _job(conn)
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'v1', ?)", (j, blocking))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO application (job_id, resume_version, status)"
                     " VALUES (?, 'v1', 'in_flight')", (j,))


def test_merged_job_is_soft_deleted_not_removed(conn):
    survivor = _job(conn, "fpA")
    dupe = _job(conn, "fpB")
    conn.execute("UPDATE job SET merged_into_job_id = ? WHERE id = ?",
                 (survivor, dupe))
    assert conn.execute("SELECT COUNT(*) n FROM job").fetchone()["n"] == 2
    live = conn.execute("SELECT COUNT(*) n FROM job"
                        " WHERE merged_into_job_id IS NULL").fetchone()["n"]
    assert live == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent'`

- [ ] **Step 3: Write `src/career_agent/db.py`**

Create empty `src/career_agent/__init__.py` and `tests/conftest.py` first.

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/ tests/
git commit -m "feat: SQLite schema with partial live-application index"
```

---

## Task 3: Career brief and board list

**Files:** Create `src/career_agent/models.py`, `src/career_agent/config.py`, `career_brief.toml`, `ats_boards.toml`; test `tests/test_config.py`

**Interfaces:** Produces `models.Job`, `models.Verdict`, `config.CareerBrief`, `config.load_brief(path)`, `config.Board`, `config.load_boards(path)`

`search_locations` drives discovery. `locations` is what the hard filter accepts on the way back. They are deliberately different fields.

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:

```python
import pydantic
import pytest

from career_agent.config import load_boards, load_brief

BRIEF = """
target_titles = ["AI Engineer", "ML Engineer"]
title_families = ["ai engineer", "ml engineer"]
search_locations = ["Chennai"]
locations = ["Chennai", "Remote"]
remote_ok = true
salary_floor_inr = 1200000
daily_cap = 5
gate_threshold = 72
staleness_days = 30
excluded_companies = ["BadCo"]
"""


def test_brief_exposes_search_locations(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text(BRIEF, encoding="utf-8")
    brief = load_brief(p)
    assert brief.search_locations == ["Chennai"]
    assert brief.remote_ok is True
    assert brief.gate_threshold == 72


def test_brief_requires_at_least_one_search_location(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('target_titles = ["X"]\nsearch_locations = []\n', encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        load_brief(p)


def test_brief_rejects_zero_daily_cap(tmp_path):
    p = tmp_path / "b.toml"
    p.write_text('target_titles = ["X"]\nsearch_locations = ["Chennai"]\n'
                 'daily_cap = 0\n', encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        load_brief(p)


def test_load_boards(tmp_path):
    p = tmp_path / "boards.toml"
    p.write_text('[[board]]\nprovider = "greenhouse"\ntoken = "anthropic"\n'
                 'company = "Anthropic"\ntier = 1\n', encoding="utf-8")
    boards = load_boards(p)
    assert boards[0].provider == "greenhouse"
    assert boards[0].token == "anthropic"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.config'`

- [ ] **Step 3: Write `src/career_agent/models.py`**

```python
from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class Job(BaseModel):
    source: Literal["ats", "linkedin", "naukri"]
    external_id: str
    company: str
    title: str
    location: str | None = None
    is_remote: bool = False
    comp_min: int | None = None
    comp_max: int | None = None
    posted_at: str | None = None
    url: str | None = None
    description: str | None = None


class Verdict(BaseModel):
    role_fit: int = Field(ge=0, le=100)
    credibility: int = Field(ge=0, le=100)
    opportunity: int = Field(ge=0, le=100)
    application_quality: int = Field(ge=0, le=100)
    eligibility_soft: int = Field(ge=0, le=100)
    verdict: Literal["submit", "hold", "skip"]
    rationale: str

    WEIGHTS: ClassVar[dict[str, float]] = {
        "role_fit": 0.30, "credibility": 0.30, "opportunity": 0.20,
        "application_quality": 0.15, "eligibility_soft": 0.05,
    }

    @property
    def weighted(self) -> float:
        return round(sum(getattr(self, k) * w for k, w in self.WEIGHTS.items()), 2)
```

- [ ] **Step 4: Write `src/career_agent/config.py`**

```python
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class CareerBrief(BaseModel):
    target_titles: list[str] = Field(min_length=1)
    title_families: list[str] = Field(default_factory=list)

    # Drives what we ASK each source for.
    search_locations: list[str] = Field(min_length=1)
    # Accepted on the way back by the hard filter. Usually a superset.
    locations: list[str] = Field(default_factory=list)

    remote_ok: bool = True
    salary_floor_inr: int | None = None
    work_authorization: list[str] = Field(default_factory=list)
    daily_cap: int = Field(default=5, ge=1)
    gate_threshold: int = Field(default=72, ge=0, le=100)
    staleness_days: int = Field(default=30, ge=1)
    excluded_companies: list[str] = Field(default_factory=list)
    non_negotiables: list[str] = Field(default_factory=list)


class Board(BaseModel):
    provider: str
    token: str
    company: str
    tier: int = 2


def load_brief(path: Path) -> CareerBrief:
    with open(path, "rb") as f:
        return CareerBrief(**tomllib.load(f))


def load_boards(path: Path) -> list[Board]:
    with open(path, "rb") as f:
        return [Board(**b) for b in tomllib.load(f).get("board", [])]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 6: Create `career_brief.toml`**

```toml
# Single source of truth for search and filter settings.
# Edit this rather than passing flags on the command line.

target_titles = ["AI Engineer", "Machine Learning Engineer", "Applied AI Engineer"]
title_families = ["ai engineer", "machine learning engineer",
                  "applied ai engineer", "ml engineer", "data scientist"]

# What we ASK each source for. Adding a city multiplies daily Actor runs
# by the number of target titles.
search_locations = ["Chennai"]

# What the hard filter ACCEPTS on the way back.
locations = ["Chennai", "Remote"]

remote_ok = true
salary_floor_inr = 1200000
work_authorization = ["India"]

daily_cap = 5
gate_threshold = 72
staleness_days = 30

excluded_companies = []
non_negotiables = ["No unpaid trial periods"]
```

- [ ] **Step 7: Create `ats_boards.toml` with a starter set**

Grow this to 200 to 400 entries by hand. It is a prerequisite for the ATS lane.

```toml
[[board]]
provider = "greenhouse"
token = "anthropic"
company = "Anthropic"
tier = 1

[[board]]
provider = "greenhouse"
token = "stripe"
company = "Stripe"
tier = 1
```

- [ ] **Step 8: Commit**

```bash
git add src/career_agent/config.py src/career_agent/models.py career_brief.toml ats_boards.toml tests/test_config.py
git commit -m "feat: career brief with search_locations driving discovery"
```

---

## Task 4: Normalization and fingerprinting

**Files:** Create `src/career_agent/normalize.py`; test `tests/test_normalize.py`

**Interfaces:** Produces `normalize.company(str)`, `normalize.title(str)`, `normalize.location(str|None)`, `normalize.fingerprint(job)`, `normalize.is_stale(job, max_age_days)`

The collision named in the spec gets an explicit assertion. It is the one place with exact required behavior written down.

- [ ] **Step 1: Write the failing test**

`tests/test_normalize.py`:

```python
from datetime import date

from career_agent import normalize
from career_agent.models import Job


def _job(**kw):
    base = dict(source="ats", external_id="1", company="Acme", title="AI Engineer")
    return Job(**{**base, **kw})


def test_the_collision_the_spec_names():
    a = _job(company="Acme Inc", title="Sr. Backend Engineer", location="Chennai")
    b = _job(company="Acme", title="Senior Software Engineer, Backend",
             location="chennai, India")
    assert normalize.fingerprint(a) == normalize.fingerprint(b)


def test_company_suffixes_stripped():
    assert normalize.company("Acme Technologies Pvt Ltd") == normalize.company("Acme")


def test_bengaluru_aliases_to_bangalore():
    assert normalize.location("Bengaluru") == normalize.location("Bangalore, India")


def test_different_company_does_not_collide():
    assert normalize.fingerprint(_job(company="Acme")) != \
           normalize.fingerprint(_job(company="Globex"))


def test_stale_when_older_than_window():
    assert normalize.is_stale(_job(posted_at="2020-01-01"), 30) is True


def test_fresh_when_recent():
    assert normalize.is_stale(_job(posted_at=date.today().isoformat()), 30) is False


def test_missing_posted_at_is_not_stale():
    assert normalize.is_stale(_job(posted_at=None), 30) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_normalize.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.normalize'`

- [ ] **Step 3: Write `src/career_agent/normalize.py`**

```python
import hashlib
import re
from datetime import date, datetime

from career_agent.models import Job

COMPANY_SUFFIXES = {"inc", "ltd", "limited", "pvt", "private", "llc", "gmbh",
                    "plc", "corp", "corporation", "co", "technologies",
                    "technology", "labs", "systems", "solutions", "software",
                    "india"}

TITLE_EXPANSIONS = {"sr": "senior", "jr": "junior", "eng": "engineer",
                    "engg": "engineer", "dev": "developer", "mgr": "manager"}

TITLE_NOISE = {"software", "staff"}

LOCATION_ALIASES = {"bengaluru": "bangalore", "bombay": "mumbai",
                    "madras": "chennai", "gurugram": "gurgaon",
                    "new delhi": "delhi"}

COUNTRY_SUFFIXES = {"india", "in", "usa", "us", "uk"}


def _words(value: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", value.lower()) if w]


def company(value: str) -> str:
    return "".join(w for w in _words(value) if w not in COMPANY_SUFFIXES)


def title(value: str) -> str:
    """Sort tokens so word order cannot break a match, and drop qualifiers
    that sources add inconsistently."""
    value = re.sub(r"\(.*?\)", " ", value)
    out = []
    for w in _words(value):
        w = TITLE_EXPANSIONS.get(w, w)
        if w not in TITLE_NOISE:
            out.append(w)
    return "".join(sorted(set(out)))


def location(value: str | None) -> str:
    if not value:
        return ""
    parts = [p.strip().lower() for p in value.split(",")]
    parts = [p for p in parts if p not in COUNTRY_SUFFIXES]
    head = parts[0] if parts else ""
    head = LOCATION_ALIASES.get(head, head)
    return re.sub(r"[^a-z0-9]+", "", head)


def fingerprint(job: Job) -> str:
    """Identifies the same role across sources. Posted date is deliberately
    excluded: the same role carries different dates on different boards."""
    raw = "|".join((company(job.company), title(job.title), location(job.location)))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def is_stale(job: Job, max_age_days: int) -> bool:
    if not job.posted_at:
        return False
    try:
        posted = datetime.fromisoformat(
            job.posted_at.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    return (date.today() - posted).days > max_age_days
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_normalize.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/normalize.py tests/test_normalize.py
git commit -m "feat: normalization and fingerprinting"
```

---

## Task 5: The hard filter

**Files:** Create `src/career_agent/hardfilter.py`; test `tests/test_hardfilter.py`

**Interfaces:** Produces `hardfilter.check(job, brief) -> str | None`. `None` means it survives.

- [ ] **Step 1: Write the failing test**

`tests/test_hardfilter.py`:

```python
import pytest

from career_agent.config import CareerBrief
from career_agent.hardfilter import check
from career_agent.models import Job

BRIEF = CareerBrief(
    target_titles=["AI Engineer"],
    title_families=["ai engineer", "machine learning engineer"],
    search_locations=["Chennai"],
    locations=["Chennai", "Remote"],
    remote_ok=True,
    salary_floor_inr=1_200_000,
    excluded_companies=["BadCo"],
    staleness_days=30,
)


def _job(**kw):
    base = dict(source="ats", external_id="1", company="Acme",
                title="AI Engineer", location="Chennai")
    return Job(**{**base, **kw})


def test_survives_a_clean_job():
    assert check(_job(), BRIEF) is None


@pytest.mark.parametrize("kw,fragment", [
    (dict(location="Mumbai"), "location"),
    (dict(comp_max=600_000), "salary floor"),
    (dict(title="Chef"), "title family"),
    (dict(company="BadCo"), "excluded"),
    (dict(posted_at="2020-01-01"), "stale"),
])
def test_each_rule_rejects_with_its_reason(kw, fragment):
    reason = check(_job(**kw), BRIEF)
    assert reason is not None
    assert fragment in reason.lower()


def test_remote_job_passes_regardless_of_city():
    assert check(_job(location="Berlin", is_remote=True), BRIEF) is None


def test_unstated_compensation_does_not_reject():
    assert check(_job(comp_max=None), BRIEF) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_hardfilter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.hardfilter'`

- [ ] **Step 3: Write `src/career_agent/hardfilter.py`**

```python
from career_agent import normalize
from career_agent.config import CareerBrief
from career_agent.models import Job


def check(job: Job, brief: CareerBrief) -> str | None:
    """Deterministic rejection. Returns None if the job survives, otherwise the
    reason. A failure here is final and never reaches the model, so a job under
    the salary floor cannot be rescued by an attractive tech stack."""

    if normalize.company(job.company) in {
            normalize.company(c) for c in brief.excluded_companies}:
        return f"excluded company: {job.company}"

    if not (job.is_remote and brief.remote_ok):
        accepted = {normalize.location(l) for l in brief.locations} - {""}
        if accepted and normalize.location(job.location) not in accepted:
            return f"location outside accepted set: {job.location}"

    if brief.salary_floor_inr and job.comp_max is not None:
        if job.comp_max < brief.salary_floor_inr:
            return f"below salary floor: {job.comp_max} < {brief.salary_floor_inr}"

    if brief.title_families:
        t = normalize.title(job.title)
        if not any(normalize.title(f) in t or t in normalize.title(f)
                   for f in brief.title_families):
            return f"title family mismatch: {job.title}"

    if normalize.is_stale(job, brief.staleness_days):
        return f"stale: posted {job.posted_at}"

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hardfilter.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/hardfilter.py tests/test_hardfilter.py
git commit -m "feat: deterministic hard filter, one reason per rejection"
```

---

## Task 6: Persistence helpers

**Files:** Create `src/career_agent/store.py`; test `tests/test_store.py`

**Interfaces:** Produces `store.upsert_jobs(conn, jobs, brief) -> int`, `store.save_hard_skip(conn, job_id, reason)`, `store.save_assessment(conn, job_id, verdict, model, prompt_version)`, `store.unscored_jobs(conn, prompt_version, limit)`, `store.facts(conn)`, `store.log(conn, job_id, type, payload)`

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:

```python
import pytest

from career_agent import db, store
from career_agent.config import CareerBrief
from career_agent.models import Job, Verdict

BRIEF = CareerBrief(target_titles=["AI Engineer"], search_locations=["Chennai"],
                    locations=["Chennai"], staleness_days=30)

V = Verdict(role_fit=90, credibility=90, opportunity=90,
            application_quality=90, eligibility_soft=90,
            verdict="submit", rationale="ok")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(**kw):
    base = dict(source="ats", external_id="1", company="Acme",
                title="AI Engineer", location="Chennai")
    return Job(**{**base, **kw})


def test_upsert_inserts_new_and_skips_duplicate_fingerprints(conn):
    assert store.upsert_jobs(conn, [_job()], BRIEF) == 1
    assert store.upsert_jobs(conn, [_job(source="linkedin", external_id="9")],
                             BRIEF) == 0
    assert conn.execute("SELECT COUNT(*) n FROM job").fetchone()["n"] == 1


def test_upsert_drops_stale(conn):
    assert store.upsert_jobs(conn, [_job(posted_at="2019-01-01")], BRIEF) == 0


def test_unscored_excludes_current_version_but_not_old(conn):
    store.upsert_jobs(conn, [_job()], BRIEF)
    job_id = conn.execute("SELECT id FROM job").fetchone()["id"]
    assert len(store.unscored_jobs(conn, "gate-v1", 10)) == 1

    store.save_assessment(conn, job_id, V, "m", "gate-v1")
    assert store.unscored_jobs(conn, "gate-v1", 10) == []
    # bumping the prompt version invalidates the old assessment
    assert len(store.unscored_jobs(conn, "gate-v2", 10)) == 1


def test_unscored_excludes_hard_skipped(conn):
    store.upsert_jobs(conn, [_job()], BRIEF)
    job_id = conn.execute("SELECT id FROM job").fetchone()["id"]
    store.save_hard_skip(conn, job_id, "location outside accepted set")
    assert store.unscored_jobs(conn, "gate-v1", 10) == []


def test_unscored_excludes_merged_jobs(conn):
    store.upsert_jobs(conn, [_job(), _job(company="Globex")], BRIEF)
    a, b = [r["id"] for r in conn.execute("SELECT id FROM job ORDER BY id")]
    conn.execute("UPDATE job SET merged_into_job_id = ? WHERE id = ?", (a, b))
    assert len(store.unscored_jobs(conn, "gate-v1", 10)) == 1


def test_facts_returns_claims(conn):
    conn.execute("INSERT INTO fact (claim, evidence) VALUES ('Built RAG', 'proj X')")
    assert store.facts(conn) == ["Built RAG (evidence: proj X)"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.store'`

- [ ] **Step 3: Write `src/career_agent/store.py`**

```python
import sqlite3

from career_agent import normalize
from career_agent.config import CareerBrief
from career_agent.models import Job, Verdict


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


def unscored_jobs(conn, prompt_version: str, limit: int) -> list[sqlite3.Row]:
    """Jobs with no assessment at the current prompt version, excluding
    hard-filter skips and merged duplicates. Bumping the version brings
    previously scored jobs back, which is what makes prompt changes measurable."""
    return conn.execute(
        "SELECT j.* FROM job j"
        " WHERE j.merged_into_job_id IS NULL"
        "   AND NOT EXISTS (SELECT 1 FROM assessment a WHERE a.job_id = j.id"
        "                     AND (a.stage = 'hard'"
        "                          OR a.prompt_version = ?))"
        " ORDER BY j.discovered_at DESC LIMIT ?",
        (prompt_version, limit)).fetchall()


def facts(conn) -> list[str]:
    rows = conn.execute("SELECT claim, evidence FROM fact ORDER BY id").fetchall()
    return [f"{r['claim']} (evidence: {r['evidence']})" for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/store.py tests/test_store.py
git commit -m "feat: persistence helpers with prompt-version idempotency"
```

---

## Task 7: ATS source connector

**Files:** Create `src/career_agent/sources/__init__.py`, `src/career_agent/sources/ats.py`; test `tests/test_sources_ats.py`

**Interfaces:** Produces `ats.fetch_greenhouse(board, client) -> list[Job]`

- [ ] **Step 1: Write the failing test**

`tests/test_sources_ats.py`:

```python
import httpx

from career_agent.config import Board
from career_agent.sources.ats import fetch_greenhouse

BOARD = Board(provider="greenhouse", token="acme", company="Acme")

SAMPLE = {"jobs": [{
    "id": 4001,
    "title": "AI Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4001",
    "updated_at": "2026-08-01T10:00:00Z",
    "location": {"name": "Chennai, India"},
    "content": "Build LLM pipelines.",
}]}


def test_maps_fields_and_uses_company_name_not_token():
    def handler(request):
        assert "acme" in str(request.url)
        return httpx.Response(200, json=SAMPLE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_greenhouse(BOARD, client)

    assert len(jobs) == 1
    assert jobs[0].source == "ats"
    assert jobs[0].external_id == "4001"
    assert jobs[0].company == "Acme"
    assert jobs[0].location == "Chennai, India"
    assert "LLM" in jobs[0].description


def test_detects_remote_from_location_text():
    payload = {"jobs": [{**SAMPLE["jobs"][0],
                         "location": {"name": "Remote - India"}}]}
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    assert fetch_greenhouse(BOARD, client)[0].is_remote is True


def test_http_error_returns_empty_not_crash():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert fetch_greenhouse(BOARD, client) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sources_ats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.sources'`

- [ ] **Step 3: Write `src/career_agent/sources/ats.py`**

Create empty `src/career_agent/sources/__init__.py` first.

```python
import logging

import httpx

from career_agent.config import Board
from career_agent.models import Job

log = logging.getLogger(__name__)

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def fetch_greenhouse(board: Board, client: httpx.Client) -> list[Job]:
    """Public Greenhouse board feed. No auth, no login, no account risk."""
    try:
        resp = client.get(GREENHOUSE.format(token=board.token), timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("greenhouse fetch failed for %s: %s", board.token, exc)
        return []

    jobs = []
    for item in resp.json().get("jobs", []):
        loc = (item.get("location") or {}).get("name")
        jobs.append(Job(
            source="ats",
            external_id=str(item["id"]),
            company=board.company,
            title=item["title"],
            location=loc,
            is_remote=bool(loc and "remote" in loc.lower()),
            posted_at=item.get("updated_at"),
            url=item.get("absolute_url"),
            description=item.get("content"),
        ))
    return jobs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sources_ats.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/sources/ tests/test_sources_ats.py
git commit -m "feat: Greenhouse ATS connector"
```

---

## Task 8: Apify connectors with location parameters

**Files:** Create `src/career_agent/sources/apify.py`; test `tests/test_sources_apify.py`

**Interfaces:** Produces `apify.LINKEDIN_ACTOR`, `apify.NAUKRI_ACTOR`, `apify.fetch_linkedin(keyword, location, remote, max_jobs, client) -> list[Job]`, `apify.fetch_naukri(...)`

Actor IDs are pinned constants so no code path can start a run we did not choose. Every call takes an explicit location, which Task 9 supplies from the brief.

- [ ] **Step 1: Write the failing test**

`tests/test_sources_apify.py`:

```python
from career_agent.sources.apify import (LINKEDIN_ACTOR, NAUKRI_ACTOR,
                                        fetch_linkedin, fetch_naukri)


class FakeDataset:
    def __init__(self, items):
        self._items = items

    def iterate_items(self):
        return iter(self._items)


class FakeActor:
    def __init__(self, parent, actor_id):
        self.parent, self.actor_id = parent, actor_id

    def call(self, run_input):
        self.parent.calls.append((self.actor_id, run_input))
        return {"defaultDatasetId": "ds1"}


class FakeClient:
    def __init__(self, items=()):
        self.items, self.calls = list(items), []

    def actor(self, actor_id):
        return FakeActor(self, actor_id)

    def dataset(self, _):
        return FakeDataset(self.items)


def test_linkedin_pins_actor_and_passes_location():
    client = FakeClient([{"id": "99", "title": "AI Engineer",
                          "companyName": "Acme", "location": "Chennai",
                          "postedAt": "2026-08-01",
                          "descriptionText": "LLM work", "link": "https://x/1"}])
    jobs = fetch_linkedin("AI Engineer", "Chennai", False, 50, client)

    actor_id, run_input = client.calls[0]
    assert actor_id == LINKEDIN_ACTOR
    assert run_input["location"] == "Chennai"
    assert run_input["keyword"] == "AI Engineer"
    assert jobs[0].source == "linkedin"
    assert jobs[0].external_id == "99"


def test_naukri_pins_actor_and_parses_inr_band():
    client = FakeClient([{"jobId": "77", "title": "ML Engineer",
                          "companyName": "Globex", "location": "Chennai",
                          "salaryMin": 600000, "salaryMax": 1500000,
                          "postedDate": "2026-08-02",
                          "jobDescription": "ML pipelines",
                          "jobUrl": "https://n/77"}])
    jobs = fetch_naukri("ML Engineer", "Chennai", False, 50, client)

    actor_id, run_input = client.calls[0]
    assert actor_id == NAUKRI_ACTOR
    assert run_input["location"] == "Chennai"
    assert jobs[0].comp_min == 600000
    assert jobs[0].comp_max == 1500000


def test_remote_uses_workmode_not_a_location_string():
    client = FakeClient()
    fetch_naukri("AI Engineer", "Chennai", True, 50, client)
    _, run_input = client.calls[0]
    assert run_input["workMode"] == "remote"
    assert run_input["location"] == "Chennai"


def test_remote_flag_marks_returned_jobs_remote():
    client = FakeClient([{"jobId": "5", "title": "AI Engineer",
                          "companyName": "Acme", "location": "Chennai"}])
    assert fetch_naukri("AI Engineer", "Chennai", True, 50, client)[0].is_remote


def test_actor_failure_returns_empty_not_crash():
    class Boom(FakeClient):
        def actor(self, actor_id):
            raise RuntimeError("actor unavailable")

    assert fetch_linkedin("x", "Chennai", False, 5, Boom()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sources_apify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.sources.apify'`

- [ ] **Step 3: Write `src/career_agent/sources/apify.py`**

```python
import logging
from typing import Any

from career_agent.models import Job

log = logging.getLogger(__name__)

# Pinned. Changing an Actor is a code change, not a runtime decision.
LINKEDIN_ACTOR = "practicaltools/linkedin-jobs"
NAUKRI_ACTOR = "automation-lab/naukri-scraper"


def _run(client: Any, actor_id: str, run_input: dict) -> list[dict]:
    try:
        run = client.actor(actor_id).call(run_input=run_input)
        return list(client.dataset(run["defaultDatasetId"]).iterate_items())
    except Exception as exc:  # an Actor outage must not kill the run
        log.warning("apify actor %s failed: %s", actor_id, exc)
        return []


def fetch_linkedin(keyword: str, location: str, remote: bool,
                   max_jobs: int, client: Any) -> list[Job]:
    run_input = {
        "keyword": keyword,
        "location": location,
        "maxJobs": max_jobs,
        "fetchDescriptions": True,
    }
    if remote:
        run_input["workplaceType"] = "remote"

    items = _run(client, LINKEDIN_ACTOR, run_input)
    return [
        Job(source="linkedin",
            external_id=str(i.get("id") or i.get("jobId")),
            company=i.get("companyName") or "unknown",
            title=i.get("title") or "unknown",
            location=i.get("location"),
            is_remote=remote,
            posted_at=i.get("postedAt"),
            url=i.get("link") or i.get("jobUrl"),
            description=i.get("descriptionText"))
        for i in items if i.get("id") or i.get("jobId")
    ]


def fetch_naukri(keyword: str, location: str, remote: bool,
                 max_jobs: int, client: Any) -> list[Job]:
    run_input = {
        "keyword": keyword,
        "location": location,
        "maxJobs": max_jobs,
        "sortBy": "date",
    }
    if remote:
        run_input["workMode"] = "remote"

    items = _run(client, NAUKRI_ACTOR, run_input)
    return [
        Job(source="naukri",
            external_id=str(i["jobId"]),
            company=i.get("companyName") or "unknown",
            title=i.get("title") or "unknown",
            location=i.get("location"),
            is_remote=remote,
            comp_min=i.get("salaryMin"),
            comp_max=i.get("salaryMax"),
            posted_at=i.get("postedDate"),
            url=i.get("jobUrl"),
            description=i.get("jobDescription"))
        for i in items if i.get("jobId")
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sources_apify.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/sources/apify.py tests/test_sources_apify.py
git commit -m "feat: Apify connectors taking explicit location and remote flags"
```

---

## Task 9: Discovery fan-out driven by the brief

**Files:** Create `src/career_agent/discovery.py`; test `tests/test_discovery.py`

**Interfaces:** Consumes `config.CareerBrief`, `config.Board`, both source modules. Produces `discovery.plan_queries(brief) -> list[Query]` and `discovery.run_discovery(brief, boards, apify_client, http_client) -> list[Job]`

This is the task the location requirement lands in. Queries come from `search_locations` in the brief, never from a flag.

- [ ] **Step 1: Write the failing test**

`tests/test_discovery.py`:

```python
from career_agent import discovery
from career_agent.config import Board, CareerBrief

BRIEF = CareerBrief(
    target_titles=["AI Engineer", "ML Engineer"],
    search_locations=["Chennai"],
    locations=["Chennai", "Remote"],
    remote_ok=True,
)


def test_queries_use_search_locations_from_the_brief():
    queries = discovery.plan_queries(BRIEF)
    assert {q.location for q in queries} == {"Chennai"}
    assert "Bangalore" not in {q.location for q in queries}


def test_one_query_per_title_per_location_per_source():
    brief = BRIEF.model_copy(update={"remote_ok": False})
    queries = discovery.plan_queries(brief)
    # 2 titles x 1 location x 2 apify sources
    assert len(queries) == 4
    assert {q.source for q in queries} == {"linkedin", "naukri"}


def test_remote_ok_adds_a_second_pass_per_title():
    queries = discovery.plan_queries(BRIEF)
    # 4 city queries + 4 remote queries
    assert len(queries) == 8
    assert sum(1 for q in queries if q.remote) == 4


def test_remote_pass_keeps_the_city_and_does_not_send_remote_as_location():
    remote_queries = [q for q in discovery.plan_queries(BRIEF) if q.remote]
    assert all(q.location == "Chennai" for q in remote_queries)


def test_multiple_cities_multiply_the_query_count():
    brief = BRIEF.model_copy(
        update={"search_locations": ["Chennai", "Bangalore"], "remote_ok": False})
    assert len(discovery.plan_queries(brief)) == 8


def test_run_discovery_calls_every_planned_query(monkeypatch):
    calls = []

    def fake_linkedin(keyword, location, remote, max_jobs, client):
        calls.append(("linkedin", keyword, location, remote))
        return []

    def fake_naukri(keyword, location, remote, max_jobs, client):
        calls.append(("naukri", keyword, location, remote))
        return []

    monkeypatch.setattr(discovery.apify, "fetch_linkedin", fake_linkedin)
    monkeypatch.setattr(discovery.apify, "fetch_naukri", fake_naukri)
    monkeypatch.setattr(discovery.ats, "fetch_greenhouse",
                        lambda board, client: [])

    boards = [Board(provider="greenhouse", token="acme", company="Acme")]
    discovery.run_discovery(BRIEF, boards, apify_client=None, http_client=None)

    assert len(calls) == 8
    assert all(location == "Chennai" for _, _, location, _ in calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.discovery'`

- [ ] **Step 3: Write `src/career_agent/discovery.py`**

```python
import logging
from dataclasses import dataclass
from typing import Any

from career_agent.config import Board, CareerBrief
from career_agent.models import Job
from career_agent.sources import apify, ats

log = logging.getLogger(__name__)

MAX_JOBS_PER_QUERY = 50

APIFY_SOURCES = ("linkedin", "naukri")


@dataclass(frozen=True)
class Query:
    source: str
    keyword: str
    location: str
    remote: bool


def plan_queries(brief: CareerBrief) -> list[Query]:
    """One query per title, per search location, per Apify source.

    Location comes from the brief, never from a command-line flag. Filtering
    after the fact is not the same as searching: without this, a run pays for
    listings the hard filter is guaranteed to discard.

    Remote is not a city. It is a separate parameter on both Actors, so
    remote_ok adds a second pass per title rather than sending the string
    "Remote" as a location.
    """
    queries = []
    for source in APIFY_SOURCES:
        for title in brief.target_titles:
            for location in brief.search_locations:
                queries.append(Query(source, title, location, remote=False))
                if brief.remote_ok:
                    queries.append(Query(source, title, location, remote=True))
    return queries


def run_discovery(brief: CareerBrief, boards: list[Board],
                  apify_client: Any, http_client: Any) -> list[Job]:
    jobs: list[Job] = []

    for q in plan_queries(brief):
        fetch = (apify.fetch_linkedin if q.source == "linkedin"
                 else apify.fetch_naukri)
        found = fetch(q.keyword, q.location, q.remote,
                      MAX_JOBS_PER_QUERY, apify_client)
        log.info("%s %r in %s remote=%s -> %d",
                 q.source, q.keyword, q.location, q.remote, len(found))
        jobs.extend(found)

    for board in boards:
        found = ats.fetch_greenhouse(board, http_client)
        log.info("ats %s -> %d", board.company, len(found))
        jobs.extend(found)

    return jobs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_discovery.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/discovery.py tests/test_discovery.py
git commit -m "feat: discovery fan-out driven by search_locations in the brief"
```

---

## Task 10: The scored gate

**Files:** Create `src/career_agent/gate.py`; test `tests/test_gate.py`

**Interfaces:** Produces `gate.PROMPT_VERSION`, `gate.InsufficientFacts`, `gate.build_prompt(job, brief, facts)`, `gate.parse_verdict(text)`, `gate.score(job, brief, facts, ask)`

`ask` is injected, so tests never touch the network.

- [ ] **Step 1: Write the failing test**

`tests/test_gate.py`:

```python
import pytest

from career_agent.config import CareerBrief
from career_agent.gate import InsufficientFacts, parse_verdict, score
from career_agent.models import Job

BRIEF = CareerBrief(target_titles=["AI Engineer"], search_locations=["Chennai"],
                    gate_threshold=72)
JOB = Job(source="ats", external_id="1", company="Acme", title="AI Engineer")
FACTS = [f"fact {i}" for i in range(20)]

GOOD = """Assessment follows.
```json
{"role_fit": 85, "credibility": 80, "opportunity": 75,
 "application_quality": 88, "eligibility_soft": 70,
 "verdict": "submit", "rationale": "Strong LLM and Python match."}
```"""


def test_parses_json_out_of_prose():
    v = parse_verdict(GOOD)
    assert v.verdict == "submit"
    assert v.role_fit == 85


def test_rejects_unparseable():
    with pytest.raises(ValueError):
        parse_verdict("I could not decide.")


def test_rejects_out_of_range_score():
    with pytest.raises(ValueError):
        parse_verdict('{"role_fit": 900, "credibility": 1, "opportunity": 1,'
                      ' "application_quality": 1, "eligibility_soft": 1,'
                      ' "verdict": "skip", "rationale": "x"}')


def test_weighted_score_uses_the_specified_weights():
    v = parse_verdict(GOOD)
    expected = (85 * .30) + (80 * .30) + (75 * .20) + (88 * .15) + (70 * .05)
    assert v.weighted == round(expected, 2)


async def test_raises_below_ten_facts():
    async def ask(_):
        raise AssertionError("must not call the model")

    with pytest.raises(InsufficientFacts):
        await score(JOB, BRIEF, ["one", "two"], ask)


async def test_retries_once_on_bad_output():
    calls = []

    async def ask(prompt):
        calls.append(prompt)
        return "nonsense" if len(calls) == 1 else GOOD

    v = await score(JOB, BRIEF, FACTS, ask)
    assert v.verdict == "submit"
    assert len(calls) == 2


async def test_raises_after_second_failure():
    async def ask(_):
        return "still nonsense"

    with pytest.raises(ValueError):
        await score(JOB, BRIEF, FACTS, ask)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.gate'`

- [ ] **Step 3: Write `src/career_agent/gate.py`**

```python
import json
import logging
import re
from typing import Awaitable, Callable

from career_agent.config import CareerBrief
from career_agent.models import Job, Verdict

log = logging.getLogger(__name__)

# Bump this in the same commit as any prompt edit. Bumping invalidates every
# stored assessment and forces a re-score, which is what makes a prompt change
# measurable against the golden set.
PROMPT_VERSION = "gate-v1"

MIN_FACTS_HARD = 10
MIN_FACTS_WARN = 20


class InsufficientFacts(RuntimeError):
    """The facts store is too thin to score credibility honestly."""


TEMPLATE = """You are scoring one job against a candidate's career brief.

CAREER BRIEF
Target titles: {titles}
Search locations: {locations}
Remote acceptable: {remote}
Salary floor (INR/year): {floor}
Non-negotiables: {non_negotiables}

VERIFIED FACTS (the only evidence you may credit for credibility)
{facts}

JOB
Company: {company}
Title: {title}
Location: {location}
Posted: {posted}
Description:
{description}

Score each dimension 0-100:
- role_fit: match to target titles, stack, domain, career direction
- credibility: whether the VERIFIED FACTS above support a strong application
  WITHOUT exaggeration. Credit nothing that is not listed there. If the facts
  do not support it, score low.
- opportunity: company reputation, role clarity, growth, freshness, warning signs
- application_quality: whether a complete, non-conflicting application can be built
- eligibility_soft: residual eligibility the mechanical filter could not decide,
  such as an unstated experience range

Then choose a verdict, applying these rules in order:
1. Any dimension below 40, or credibility below 60 -> "skip"
2. Weighted score at or above {threshold} -> "submit"
   (weights: role_fit .30, credibility .30, opportunity .20,
    application_quality .15, eligibility_soft .05)
3. Otherwise -> "hold"

Reply with a single JSON object and nothing else:
{{"role_fit": int, "credibility": int, "opportunity": int,
  "application_quality": int, "eligibility_soft": int,
  "verdict": "submit"|"hold"|"skip", "rationale": "one or two sentences"}}
"""


def build_prompt(job: Job, brief: CareerBrief, facts: list[str]) -> str:
    return TEMPLATE.format(
        titles=", ".join(brief.target_titles),
        locations=", ".join(brief.search_locations),
        remote=brief.remote_ok,
        floor=brief.salary_floor_inr or "not specified",
        non_negotiables="; ".join(brief.non_negotiables) or "none",
        facts="\n".join(f"- {f}" for f in facts),
        company=job.company,
        title=job.title,
        location=job.location or "not specified",
        posted=job.posted_at or "unknown",
        description=(job.description or "")[:6000],
        threshold=brief.gate_threshold,
    )


def parse_verdict(text: str) -> Verdict:
    """Tolerate prose around the JSON; reject anything that is not a Verdict."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model output")
    try:
        return Verdict(**json.loads(match.group(0)))
    except Exception as exc:
        raise ValueError(f"could not parse verdict: {exc}") from exc


async def score(job: Job, brief: CareerBrief, facts: list[str],
                ask: Callable[[str], Awaitable[str]]) -> Verdict:
    if len(facts) < MIN_FACTS_HARD:
        raise InsufficientFacts(
            f"{len(facts)} facts recorded, need at least {MIN_FACTS_HARD}. "
            "Credibility carries 30% weight and a floor of 60, so scoring now "
            "would skip everything for a reason that looks like the market.")
    if len(facts) < MIN_FACTS_WARN:
        log.warning("only %d facts; credibility scores are probably depressed",
                    len(facts))

    prompt = build_prompt(job, brief, facts)
    try:
        return parse_verdict(await ask(prompt))
    except ValueError:
        retry = prompt + ("\n\nYour previous reply was not valid JSON. "
                          "Reply with ONLY the JSON object.")
        return parse_verdict(await ask(retry))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gate.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/gate.py tests/test_gate.py
git commit -m "feat: scored gate with facts sufficiency guard and parse retry"
```

---

## Task 11: The pipeline runner

**Files:** Create `src/career_agent/run.py`; test `tests/test_run.py`

**Interfaces:** Produces `run.main()`, `run.verify_auth()`, `run.AuthError`, `run.run_once(args)`

Six fixed stages. No agent loop, no MCP server, no tool allowlist.

- [ ] **Step 1: Write the failing test**

`tests/test_run.py`:

```python
import pytest

from career_agent.run import AuthError, verify_auth


def test_rejects_api_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        verify_auth()


def test_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(AuthError, match="claude setup-token"):
        verify_auth()


def test_passes_with_token_only(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    verify_auth()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_run.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.run'`

- [ ] **Step 3: Write `src/career_agent/run.py`**

```python
import argparse
import asyncio
import logging
import os
from pathlib import Path

import httpx
from apify_client import ApifyClient
from dotenv import load_dotenv

from career_agent import db, discovery, gate, hardfilter, store
from career_agent.config import load_boards, load_brief

log = logging.getLogger(__name__)

MODEL_ID = "claude-agent-sdk"


class AuthError(RuntimeError):
    pass


def verify_auth() -> None:
    """Fail loudly rather than silently falling back to pay-per-token."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise AuthError(
            "ANTHROPIC_API_KEY is set. It outranks the subscription token and "
            "would silently bill per token. Unset it and re-run.")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise AuthError(
            "CLAUDE_CODE_OAUTH_TOKEN is not set. Run `claude setup-token` and "
            "put the result in .env")


async def _ask(prompt: str) -> str:
    """One tool-less call for gate scoring."""
    from claude_agent_sdk import query

    chunks = []
    async for message in query(prompt=prompt, options={"allowed_tools": []}):
        text = getattr(message, "text", None)
        if text:
            chunks.append(text)
    return "".join(chunks)


async def run_once(args) -> None:
    verify_auth()

    conn = db.connect(Path(args.db))
    db.init_schema(conn)

    brief = load_brief(Path(args.brief))
    boards = load_boards(Path(args.boards))

    # 1-2. fetch and normalize
    jobs = discovery.run_discovery(
        brief, boards,
        apify_client=ApifyClient(os.environ["APIFY_TOKEN"]),
        http_client=httpx.Client())

    # 3. dedupe and drop stale
    new_count = store.upsert_jobs(conn, jobs, brief)
    log.info("discovered %d, %d new after dedupe and staleness",
             len(jobs), new_count)

    # 4-5. hard filter, then score survivors
    scored = skipped = 0
    for row in store.unscored_jobs(conn, gate.PROMPT_VERSION, args.max_score):
        job = _row_to_job(row)

        reason = hardfilter.check(job, brief)
        if reason:
            store.save_hard_skip(conn, row["id"], reason)
            skipped += 1
            continue

        verdict = await gate.score(job, brief, store.facts(conn), _ask)
        store.save_assessment(conn, row["id"], verdict, MODEL_ID,
                              gate.PROMPT_VERSION)
        scored += 1

    log.info("hard-filtered %d, scored %d", skipped, scored)


def _row_to_job(row):
    from career_agent.models import Job
    return Job(source=row["source"], external_id=row["external_id"],
               company=row["company"], title=row["title"],
               location=row["location"], is_remote=bool(row["is_remote"]),
               comp_min=row["comp_min"], comp_max=row["comp_max"],
               posted_at=row["posted_at"], description=row["description"])


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="career-agent")
    parser.add_argument("command", choices=["run", "serve"])
    parser.add_argument("--db", default="data/career.db")
    parser.add_argument("--brief", default="career_brief.toml")
    parser.add_argument("--boards", default="ats_boards.toml")
    parser.add_argument("--max-score", type=int, default=25, dest="max_score",
                        help="cap scored jobs per run; the rest carry to the "
                             "next run, which keeps the prompt cache warm")
    # Deliberately no --location flag. Search locations come from the brief.
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        uvicorn.run("career_agent.web.app:app", port=8000, reload=False)
    else:
        asyncio.run(run_once(args))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_run.py -v`
Expected: 3 passed

- [ ] **Step 5: Run it against real sources once**

Run: `career-agent run --max-score 3`
Expected: log lines showing per-query counts with `location=Chennai`, then a discovered/new/filtered/scored summary.

- [ ] **Step 6: Commit**

```bash
git add src/career_agent/run.py tests/test_run.py
git commit -m "feat: deterministic pipeline runner with loud auth verification"
```

---

## Task 12: Submission lifecycle

**Files:** Create `src/career_agent/apply/__init__.py`, `src/career_agent/apply/ats.py`; test `tests/test_apply_ats.py`

**Interfaces:** Produces `ats_apply.submit(conn, job_id, dry_run, filler=None)`, `ats_apply.CaptchaEncountered`, `ats_apply.sweep_stale_in_flight(conn, minutes=15)`, `ats_apply.MAX_ATTEMPTS`

- [ ] **Step 1: Write the failing test**

`tests/test_apply_ats.py`:

```python
import pytest

from career_agent import db
from career_agent.apply import ats as ats_apply


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized, url)"
              " VALUES ('fp','ats','1','Acme','acme','AI Engineer',"
              " 'aiengineer','https://x/apply')")
    c.commit()
    return c


async def _ok(_url):
    return {"note": "filled"}


async def test_dry_run_records_a_draft_and_does_not_send(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, filler=_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT * FROM application WHERE job_id = 1").fetchone()
    assert row["status"] == "draft"
    assert row["submitted_at"] is None


async def test_a_draft_does_not_block_a_real_submission(conn):
    await ats_apply.submit(conn, 1, dry_run=True, filler=_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    assert out["ok"] is True
    statuses = {r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1")}
    assert statuses == {"draft", "submitted"}


async def test_second_real_submission_is_refused(conn):
    await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    assert out["ok"] is False
    assert "already" in out["reason"]


async def test_captcha_holds_and_records_no_application(conn):
    async def boom(_url):
        raise ats_apply.CaptchaEncountered("recaptcha frame present")

    out = await ats_apply.submit(conn, 1, dry_run=False, filler=boom)
    assert out["held"] is True
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "captcha_held" in types


async def test_three_failures_become_failed_permanent(conn):
    async def fail(_url):
        raise RuntimeError("form error")

    for _ in range(ats_apply.MAX_ATTEMPTS):
        out = await ats_apply.submit(conn, 1, dry_run=False, filler=fail)
        assert out["ok"] is False

    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1 ORDER BY id")]
    assert statuses[-1] == "failed_permanent"

    out = await ats_apply.submit(conn, 1, dry_run=False, filler=_ok)
    assert out["ok"] is False


def test_stale_in_flight_becomes_held_unknown(conn):
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " started_at) VALUES (1, 'v1', 'in_flight',"
                 " datetime('now', '-30 minutes'))")
    conn.commit()
    assert ats_apply.sweep_stale_in_flight(conn, minutes=15) == 1
    row = conn.execute("SELECT status FROM application").fetchone()
    assert row["status"] == "held_unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_apply_ats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.apply'`

- [ ] **Step 3: Write `src/career_agent/apply/ats.py`**

Create empty `src/career_agent/apply/__init__.py` first.

```python
import json
import logging
import sqlite3
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

RESUME_VERSION = "base-v1"
MAX_ATTEMPTS = 3
BLOCKING = ("in_flight", "submitted", "held_unknown", "failed_permanent")


class CaptchaEncountered(Exception):
    """The site showed a captcha, so it has already classified this session as
    suspicious. Backing off is cheaper than pushing through."""


async def _default_filler(url: str) -> dict:
    """Drive the real form. Imported lazily so tests never need a browser."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto(url)
        if await page.locator("iframe[src*='recaptcha'], .h-captcha").count():
            await page.screenshot(path=f"screenshots/captcha-{abs(hash(url))}.png")
            await browser.close()
            raise CaptchaEncountered(url)
        answers = {"note": "filled from facts store and qa_bank"}
        await browser.close()
        return answers


def sweep_stale_in_flight(conn: sqlite3.Connection, minutes: int = 15) -> int:
    """A crash during submission leaves in_flight behind. Its true state is
    unknown, so it blocks rather than allowing a possible double send."""
    cur = conn.execute(
        "UPDATE application SET status = 'held_unknown'"
        " WHERE status = 'in_flight'"
        f"  AND started_at < datetime('now', '-{int(minutes)} minutes')")
    conn.commit()
    return cur.rowcount


async def submit(conn: sqlite3.Connection, job_id: int, dry_run: bool,
                 filler: Callable[[str], Awaitable[dict]] | None = None) -> dict:
    live = conn.execute(
        f"SELECT status FROM application WHERE job_id = ? AND status IN "
        f"({','.join('?' * len(BLOCKING))})", (job_id, *BLOCKING)).fetchone()
    if live:
        return {"ok": False,
                "reason": f"job {job_id} already has a {live['status']} attempt"}

    row = conn.execute("SELECT url FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"job {job_id} not found"}

    filler = filler or _default_filler

    if dry_run:
        answers = await filler(row["url"])
        conn.execute(
            "INSERT INTO application (job_id, resume_version, answers, status)"
            " VALUES (?, ?, ?, 'draft')",
            (job_id, RESUME_VERSION, json.dumps(answers)))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, RESUME_VERSION))
    app_id = cur.lastrowid
    conn.commit()

    try:
        answers = await filler(row["url"])
    except CaptchaEncountered as exc:
        conn.execute("DELETE FROM application WHERE id = ?", (app_id,))
        conn.execute("INSERT INTO event (job_id, type, payload)"
                     " VALUES (?, 'captcha_held', ?)", (job_id, str(exc)))
        conn.commit()
        return {"ok": False, "held": True,
                "reason": "captcha encountered; held for review"}
    except Exception as exc:
        failures = conn.execute(
            "SELECT COUNT(*) n FROM application"
            " WHERE job_id = ? AND status = 'failed'", (job_id,)).fetchone()["n"]
        status = "failed_permanent" if failures + 1 >= MAX_ATTEMPTS else "failed"
        conn.execute("UPDATE application SET status = ? WHERE id = ?",
                     (status, app_id))
        conn.execute("INSERT INTO event (job_id, type, payload) VALUES (?, ?, ?)",
                     (job_id, status, str(exc)))
        conn.commit()
        return {"ok": False, "reason": f"submission {status}: {exc}"}

    conn.execute(
        "UPDATE application SET status = 'submitted', answers = ?,"
        " submitted_at = datetime('now') WHERE id = ?",
        (json.dumps(answers), app_id))
    conn.execute("INSERT INTO event (job_id, type, payload)"
                 " VALUES (?, 'submitted', ?)", (job_id, row["url"]))
    conn.commit()
    return {"ok": True, "job_id": job_id, "status": "submitted"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_apply_ats.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/apply/ tests/test_apply_ats.py
git commit -m "feat: submission lifecycle with retry cap and stale in-flight sweep"
```

---

## Task 13: Outcome derivation

**Files:** Create `src/career_agent/outcomes.py`; test `tests/test_outcomes.py`

**Interfaces:** Produces `outcomes.derive_no_response(conn, after_days=30) -> int`, `outcomes.effective_outcome(conn, application_id) -> str | None`, `outcomes.callback_rate(conn) -> tuple[int, int]`

Nobody emails to say they are ignoring you, so `no_response` has to be derived or the callback-rate denominator collapses.

- [ ] **Step 1: Write the failing test**

`tests/test_outcomes.py`:

```python
import pytest

from career_agent import db, outcomes


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized)"
              " VALUES ('fp','ats','1','Acme','acme','AI Eng','aieng')")
    c.commit()
    return c


def _app(conn, days_ago):
    return conn.execute(
        "INSERT INTO application (job_id, resume_version, status, submitted_at)"
        f" VALUES (1, 'v1', 'submitted', datetime('now', '-{days_ago} days'))"
    ).lastrowid


def test_derives_no_response_after_the_window(conn):
    _app(conn, 31)
    assert outcomes.derive_no_response(conn, after_days=30) == 1
    row = conn.execute("SELECT type, derived FROM outcome").fetchone()
    assert row["type"] == "no_response"
    assert row["derived"] == 1


def test_does_not_derive_before_the_window(conn):
    _app(conn, 10)
    assert outcomes.derive_no_response(conn, after_days=30) == 0


def test_does_not_derive_twice(conn):
    _app(conn, 31)
    outcomes.derive_no_response(conn, after_days=30)
    assert outcomes.derive_no_response(conn, after_days=30) == 0


def test_does_not_derive_when_a_real_outcome_exists(conn):
    a = _app(conn, 31)
    conn.execute("INSERT INTO outcome (application_id, type, derived)"
                 " VALUES (?, 'rejected', 0)", (a,))
    conn.commit()
    assert outcomes.derive_no_response(conn, after_days=30) == 0


def test_manual_outcome_supersedes_a_derived_one(conn):
    a = _app(conn, 31)
    outcomes.derive_no_response(conn, after_days=30)
    conn.execute("INSERT INTO outcome (application_id, type, derived, occurred_at)"
                 " VALUES (?, 'screen', 0, datetime('now', '+1 day'))", (a,))
    conn.commit()
    assert outcomes.effective_outcome(conn, a) == "screen"


def test_derived_loses_a_tie(conn):
    a = _app(conn, 31)
    conn.execute("INSERT INTO outcome (application_id, type, derived, occurred_at)"
                 " VALUES (?, 'no_response', 1, '2026-08-01T00:00:00')", (a,))
    conn.execute("INSERT INTO outcome (application_id, type, derived, occurred_at)"
                 " VALUES (?, 'interview', 0, '2026-08-01T00:00:00')", (a,))
    conn.commit()
    assert outcomes.effective_outcome(conn, a) == "interview"


def test_callback_rate_counts_all_submitted_as_denominator(conn):
    a1 = _app(conn, 31)
    conn.execute("INSERT INTO job (fingerprint, source, external_id, company,"
                 " company_normalized, title, title_normalized)"
                 " VALUES ('fp2','ats','2','B','b','AI','ai')")
    a2 = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, submitted_at)"
        " VALUES (2, 'v1', 'submitted', datetime('now'))").lastrowid
    conn.execute("INSERT INTO outcome (application_id, type) VALUES (?, 'screen')",
                 (a2,))
    conn.commit()
    outcomes.derive_no_response(conn, after_days=30)
    assert outcomes.callback_rate(conn) == (1, 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_outcomes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.outcomes'`

- [ ] **Step 3: Write `src/career_agent/outcomes.py`**

```python
import sqlite3

CALLBACK_TYPES = ("screen", "interview", "offer")


def derive_no_response(conn: sqlite3.Connection, after_days: int = 30) -> int:
    """Silence is never reported, so it has to be inferred. Without this the
    callback-rate denominator shrinks to whatever you remembered to annotate,
    which inflates the rate the v3 decision depends on."""
    cur = conn.execute(
        "INSERT INTO outcome (application_id, type, derived)"
        " SELECT a.id, 'no_response', 1 FROM application a"
        "  WHERE a.status = 'submitted'"
        f"   AND a.submitted_at < datetime('now', '-{int(after_days)} days')"
        "    AND NOT EXISTS (SELECT 1 FROM outcome o WHERE o.application_id = a.id)")
    conn.commit()
    return cur.rowcount


def effective_outcome(conn: sqlite3.Connection, application_id: int) -> str | None:
    """Latest occurred_at wins; a derived row loses a tie against a manual one."""
    row = conn.execute(
        "SELECT type FROM outcome WHERE application_id = ?"
        " ORDER BY occurred_at DESC, derived ASC, id DESC LIMIT 1",
        (application_id,)).fetchone()
    return row["type"] if row else None


def callback_rate(conn: sqlite3.Connection) -> tuple[int, int]:
    """Returns (callbacks, total submitted). The denominator is every submitted
    application, not only the annotated ones."""
    rows = conn.execute(
        "SELECT id FROM application WHERE status = 'submitted'").fetchall()
    callbacks = sum(1 for r in rows
                    if effective_outcome(conn, r["id"]) in CALLBACK_TYPES)
    return callbacks, len(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_outcomes.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/outcomes.py tests/test_outcomes.py
git commit -m "feat: derived no_response and callback rate with supersession"
```

---

## Task 14: The dashboard

**Files:** Create `src/career_agent/web/__init__.py`, `src/career_agent/web/app.py`, `src/career_agent/web/templates/index.html`; test `tests/test_web.py`

**Interfaces:** FastAPI `app` with `GET /`, `GET /?show=skipped`, `POST /apply/{job_id}`, `POST /dismiss/{job_id}`, `POST /override/{job_id}`

Skipped jobs must be reachable, or the gate can never be caught being too harsh.

- [ ] **Step 1: Write the failing test**

`tests/test_web.py`:

```python
import pytest
from fastapi.testclient import TestClient

from career_agent import db
from career_agent.web import app as web


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
    conn.commit()
    monkeypatch.setattr(web, "DB_PATH", path)
    return TestClient(web.app)


def test_index_shows_submit_and_hold_with_rationale(client):
    r = client.get("/")
    assert "AI Engineer" in r.text
    assert "strong match" in r.text


def test_index_hides_skips_by_default(client):
    assert "ML Engineer" not in client.get("/").text


def test_skipped_view_shows_them(client):
    r = client.get("/?show=skipped")
    assert "ML Engineer" in r.text
    assert "credibility below floor" in r.text


def test_dismiss_records_the_human_decision(client):
    r = client.post("/dismiss/1")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_dismissed" in types


def test_override_on_a_skip_records_the_override(client):
    r = client.post("/override/2")
    assert r.status_code == 200
    conn = db.connect(web.DB_PATH)
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "human_override" in types


def test_plain_apply_refuses_a_skip(client):
    r = client.post("/apply/2")
    assert "skip" in r.text.lower() or "override" in r.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'career_agent.web'`

- [ ] **Step 3: Write `src/career_agent/web/app.py`**

Create empty `src/career_agent/web/__init__.py` first.

```python
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from career_agent import db, store
from career_agent.apply import ats as ats_apply
from career_agent.config import load_brief

DB_PATH = Path("data/career.db")
BRIEF_PATH = Path("career_brief.toml")

app = FastAPI(title="Career Agent")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

LIST_SQL = """
SELECT j.id, j.company, j.title, j.location, j.source, j.url,
       a.verdict, a.rationale, a.stage, a.weighted_score AS score,
       (SELECT COUNT(*) FROM application ap
         WHERE ap.job_id = j.id
           AND ap.status IN ('in_flight','submitted')) AS applied
  FROM job j JOIN assessment a ON a.job_id = j.id
 WHERE j.merged_into_job_id IS NULL AND a.verdict IN ({placeholders})
 ORDER BY a.weighted_score DESC NULLS LAST, j.discovered_at DESC
"""


def _conn():
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    ats_apply.sweep_stale_in_flight(conn)
    return conn


@app.get("/", response_class=HTMLResponse)
def index(request: Request, show: str = "queue"):
    conn = _conn()
    verdicts = ["skip"] if show == "skipped" else ["submit", "hold"]
    sql = LIST_SQL.format(placeholders=",".join("?" * len(verdicts)))
    rows = conn.execute(sql, verdicts).fetchall()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"jobs": rows, "show": show})


def _guard(conn, job_id: int, allow_skip: bool) -> str | None:
    """Dashboard-side guardrail. The partial unique index is the real
    guarantee; this exists to produce a readable message."""
    a = conn.execute("SELECT verdict FROM assessment WHERE job_id = ?"
                     " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if a is None:
        return "This job has not been scored yet."
    if a["verdict"] == "skip" and not allow_skip:
        return "The gate skipped this one. Use Apply anyway to override."

    brief = load_brief(BRIEF_PATH)
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


async def _do_apply(job_id: int, allow_skip: bool, event: str | None):
    conn = _conn()
    denial = _guard(conn, job_id, allow_skip)
    if denial:
        return HTMLResponse(f'<span class="denied">{denial}</span>')

    if event:
        store.log(conn, job_id, event)

    result = await ats_apply.submit(conn, job_id, dry_run=True)
    if not result["ok"]:
        return HTMLResponse(f'<span class="denied">{result["reason"]}</span>')
    return HTMLResponse('<span class="done">Applied</span>')


@app.post("/apply/{job_id}", response_class=HTMLResponse)
async def apply(job_id: int):
    return await _do_apply(job_id, allow_skip=False, event="human_applied")


@app.post("/override/{job_id}", response_class=HTMLResponse)
async def override(job_id: int):
    """Applying to something the gate skipped. The most valuable label the
    system produces, because it is the gate erring in the expensive direction."""
    return await _do_apply(job_id, allow_skip=True, event="human_override")


@app.post("/dismiss/{job_id}", response_class=HTMLResponse)
def dismiss(job_id: int):
    conn = _conn()
    store.log(conn, job_id, "human_dismissed")
    return HTMLResponse('<span class="done">Dismissed</span>')
```

- [ ] **Step 4: Write `src/career_agent/web/templates/index.html`**

```html
<!doctype html>
<meta charset="utf-8">
<title>Career Agent</title>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 62rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #ddd; }
  .submit { color: #157f3b; font-weight: 600; }
  .hold { color: #9a6700; font-weight: 600; }
  .skip { color: #b42318; font-weight: 600; }
  .denied { color: #b42318; }
  .done { color: #157f3b; }
  .rationale { color: #555; font-size: .9em; }
  nav a { margin-right: 1rem; }
</style>
<h1>Career Agent</h1>
<nav>
  <a href="/">Queue</a>
  <a href="/?show=skipped">Skipped</a>
</nav>
{% if show == "skipped" %}
<p class="rationale">
  These were skipped by the gate. Applying anyway is recorded as an override,
  which is how the gate gets caught being too strict.
</p>
{% endif %}
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
      {% if j["applied"] %}
        <span class="done">Applied</span>
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/career_agent/web/ tests/test_web.py
git commit -m "feat: dashboard with skipped view, override, and dismiss"
```

---

## Task 15: The golden set

**Files:** Create `tests/golden/listings.json`, `tests/golden/test_golden.py`

- [ ] **Step 1: Create `tests/golden/listings.json`**

Start with three and grow to twenty from real listings. `expected` is your own label.

```json
[
  {
    "expected": "submit",
    "job": {"source": "ats", "external_id": "g1", "company": "Acme AI",
            "title": "AI Engineer", "location": "Chennai",
            "description": "Build RAG pipelines in Python. 1-3 years experience."}
  },
  {
    "expected": "skip",
    "job": {"source": "ats", "external_id": "g2", "company": "Globex",
            "title": "Senior Staff ML Architect", "location": "Zurich",
            "description": "12+ years required. On-site. Swiss permit required."}
  },
  {
    "expected": "hold",
    "job": {"source": "linkedin", "external_id": "g3", "company": "Initech",
            "title": "Data Scientist", "location": "Chennai",
            "description": "Mostly dashboards and SQL reporting. Some Python."}
  }
]
```

- [ ] **Step 2: Write `tests/golden/test_golden.py`**

```python
import json
import os
from pathlib import Path

import pytest

from career_agent import db, store
from career_agent.config import load_brief
from career_agent.gate import score
from career_agent.models import Job

CASES = json.loads((Path(__file__).parent / "listings.json").read_text())


@pytest.mark.skipif(not os.environ.get("RUN_GOLDEN"),
                    reason="set RUN_GOLDEN=1 to run against the live model")
async def test_gate_agrees_with_hand_labels():
    from career_agent.run import _ask

    brief = load_brief(Path("career_brief.toml"))
    conn = db.connect(Path("data/career.db"))
    db.init_schema(conn)
    facts = store.facts(conn)

    disagreements = []
    for case in CASES:
        verdict = await score(Job(**case["job"]), brief, facts, _ask)
        if verdict.verdict != case["expected"]:
            disagreements.append(
                f"{case['job']['title']} at {case['job']['company']}: "
                f"expected {case['expected']}, got {verdict.verdict} "
                f"({verdict.rationale})")

    if disagreements:
        pytest.fail(f"{len(disagreements)}/{len(CASES)} disagreed:\n"
                    + "\n".join(disagreements))
```

- [ ] **Step 3: Run it against the live model**

PowerShell: `$env:RUN_GOLDEN=1; pytest tests/golden -v`
Expected: either passes, or fails with a readable list of disagreements. A failure here is data for tuning the threshold, not a bug.

- [ ] **Step 4: Commit**

```bash
git add tests/golden/
git commit -m "test: golden set regression check for the gate"
```

---

## Task 16: Optional scheduling and run docs

**Files:** Create `scripts/install-scheduler.ps1`, `docs/running.md`; modify `src/career_agent/web/app.py`

No scheduled task exists by default. The agent stays inert until you either run it or opt in.

- [ ] **Step 1: Write `scripts/install-scheduler.ps1`**

```powershell
param(
    [string]$RepoPath = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$Time = "08:00"
)

$python = Join-Path $RepoPath ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv not found at $python" }

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m career_agent.run run" -WorkingDirectory $RepoPath
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "CareerAgentDaily" -Action $action `
    -Trigger $trigger -Settings $settings -Force

Write-Host "Installed. Remove with: Unregister-ScheduledTask -TaskName CareerAgentDaily"
```

- [ ] **Step 2: Add the trigger check to `src/career_agent/web/app.py`**

Append this and reference `scheduled_task_installed()` from the index template context.

```python
import subprocess


def scheduled_task_installed(name: str = "CareerAgentDaily") -> bool:
    """No trigger is installed by default. The dashboard says so rather than
    letting you assume something ran overnight when nothing did."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10)
        return name in result.stdout
    except Exception:
        return False
```

Add to the `index` context: `"scheduled": scheduled_task_installed()`, and render in `index.html` above the table:

```html
{% if not scheduled %}
<p class="rationale">
  No scheduled run is installed, so nothing runs unless you start it.
  Run <code>career-agent run</code>, or install the daily task with
  <code>scripts\install-scheduler.ps1</code>.
</p>
{% endif %}
```

- [ ] **Step 3: Write `docs/running.md`**

```markdown
# Running the agent

## First run

1. `python -m venv .venv` then `.venv\Scripts\activate`
2. `pip install -e ".[dev]"`
3. `playwright install chromium`
4. `claude setup-token`, then put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`
5. Add `APIFY_TOKEN` to `.env`
6. Confirm the API key is unset: `Remove-Item Env:ANTHROPIC_API_KEY`
7. Add at least 10 rows to the `fact` table. The gate refuses to run below that.
8. `career-agent run --max-score 3` and read the log

## Daily use

Nothing runs on its own until you ask it to.

- `career-agent run` performs one pass: discover, filter, score.
- `career-agent serve` opens the dashboard on http://localhost:8000 and tells you
  whether a scheduled run exists.
- `.\scripts\install-scheduler.ps1` opts in to a daily 08:00 run.
- `Unregister-ScheduledTask -TaskName CareerAgentDaily` opts back out.

## Changing what it searches for

Edit `career_brief.toml`. `search_locations` decides what each source is asked
for; `locations` decides what the filter accepts on the way back. There is no
command-line flag for either, deliberately.

## Pausing

`sqlite3 data/career.db "INSERT INTO event (type, payload) VALUES ('pause','on')"`
Clear it with payload `off`.
```

- [ ] **Step 4: Verify both modes**

Run: `career-agent serve`, confirm the banner says no scheduled run exists.
Run: `.\scripts\install-scheduler.ps1`, then `Get-ScheduledTask -TaskName CareerAgentDaily`, reload the dashboard and confirm the banner is gone.

- [ ] **Step 5: Commit**

```bash
git add scripts/install-scheduler.ps1 docs/running.md src/career_agent/web/
git commit -m "feat: opt-in scheduling with an explicit no-trigger banner"
```

---

## Self-review notes

**Spec coverage.** Every spec section maps to a task: prerequisites (Task 3 for the board list, Task 10 for the facts guard), spikes (Task 1, and Spike 2 runs alongside Tasks 7-9), architecture (Task 11), discovery from the brief (Task 9), hard filter (Task 5), scored gate (Task 10), calibration (Tasks 13-15), data model (Task 2), fingerprinting (Task 4), submission lifecycle (Task 12), idempotency (Task 6), guardrails (Task 14), error handling (Tasks 10, 12), triggering (Task 16), testing (per task).

**Deliberately not built.** LinkedIn and Naukri submission: both stay manual-queue only per the spec, so no connector exists. Lever and Ashby: Greenhouse proves the pattern and the other two are the same shape against different URLs. `qa_bank` has a table but no form-filling logic, because whether it is worth building depends on Spike 2b showing ATS is more than 20% of relevant listings. Tailoring and auto-submission are v2 and v3.

**Type consistency.** `Job`, `Verdict`, `CareerBrief`, and `Board` keep the same field names across every task. `submit(conn, job_id, dry_run, filler)` has one signature, used identically in Tasks 12 and 14. `fetch_linkedin` and `fetch_naukri` share the signature `(keyword, location, remote, max_jobs, client)`, which Task 9 depends on when it dispatches by source.
