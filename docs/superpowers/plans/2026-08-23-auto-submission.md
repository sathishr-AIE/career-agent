# v3 Real Greenhouse Auto-Submission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `ats.py`'s stubbed `_default_filler` with a real, tested Greenhouse form-filler, give `qa_bank` its first real behavior, and add the candidate identity data Greenhouse's standard fields need — while keeping `SUBMISSION_IMPLEMENTED = False` so nothing sends for real until that's flipped by hand later.

**Architecture:** A new gitignored `candidate_profile.toml` holds PII the tracked `career_brief.toml` shouldn't. `store.py` gains `qa_bank` reads/writes. `ats.py` splits its filler into a pure, fully-unit-tested decision function (`resolve_answers`, given a form's questions, decides each answer or raises `NeedsAnswer`) and a thin Playwright-driven shell around it (reads the live DOM, later types the decided answers back in). `submit()` routes by `job.source`: Greenhouse (`'ats'`) gets the real filler gated by `SUBMISSION_IMPLEMENTED`; everything else keeps today's unconditional refusal. `worker.apply_tick` and the dashboard both learn to park a job and prompt for an answer instead of looping when a question has none.

**Tech Stack:** Python, FastAPI, SQLite, Playwright (already a dependency), Jinja2/htmx (existing dashboard), pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-23-auto-submission-design.md` — read it first; this plan argues from it and flags every place it deviates.

## Deviations from the spec, and why

The spec was written before this level of implementation detail was worked out. Two places below do something slightly different from the spec's literal text, in both cases because it's a better fit with how this codebase already works:

1. **No LLM-driven "answer from facts" path.** The spec says a question like "years of ML experience" is "resolved from `fact` rows the same way `tailor()` already does." Building that would mean a second full prompt/parse/retry pipeline (like `tailor.py`'s), which the spec never actually specified (no prompt, no schema). This plan drops it: **every** custom question goes through `qa_bank` only. The first time a fact-answerable question shows up, it holds and you type the answer once — functionally the same outcome as an automatic lookup, at the cost of one manual answer instead of zero, and it never needs a second LLM pipeline. Strictly more conservative than the spec, not less.
2. **`compute_answers`/`fill_and_submit` own their own browser**, matching `_default_filler`'s existing convention, rather than taking a `page` argument from a caller that manages the browser lifecycle. No behavior change — same two functions, same responsibilities, just self-contained like the code they replace.
3. **The Greenhouse field-matching code (Task 4) has no automated test.** Every existing Playwright-touching function in this codebase (`_default_filler` today) is exercised only through injected test doubles — never against a real or synthetic browser page. This plan keeps that convention rather than introducing the project's first real-browser test (which would need `playwright install chromium` in CI and is exactly the kind of flakiness this codebase has avoided so far). The decision logic that actually matters (`resolve_answers` — Task 3) is pulled out into a pure function and fully unit tested instead; only the DOM-reading/typing shell around it goes untested by pytest, verified manually against a real posting instead (see Task 4 and the README note in Task 9).

## Global Constraints

- `SUBMISSION_IMPLEMENTED` in `ats.py` stays `False` after this plan ships. Flipping it is a manual, later decision — no task in this plan flips it.
- Real automation targets Greenhouse only, detected as `job.source == "ats"` (the only ATS-lane fetcher that exists — see the spec's "assumption to revisit later"). Every other source keeps today's unconditional refusal on a real send, regardless of `SUBMISSION_IMPLEMENTED`.
- `qa_bank`'s volatile-answer reconfirmation window is 30 days, compared using naive UTC (`datetime.utcnow()` against SQLite's naive-UTC `datetime('now')` strings) — never `dt.datetime.now()` or a timezone-aware UTC now, which this project has hit as a recurring bug class before.
- `candidate_profile.toml` is gitignored; nothing in this plan commits real PII.
- No task in this plan touches Lever, Ashby, or PDF rendering — all explicitly deferred in the spec.
- Every existing test in `tests/test_apply_ats.py` uses the OLD single-argument `filler(url) -> dict` contract. Task 5 rewrites that whole file to match the new `compute_answers`/`fill_and_submit` contract — this is expected, not a regression to chase down task by task.

---

### Task 1: Candidate profile config

**Files:**
- Modify: `src/career_agent/config.py`
- Modify: `.gitignore`
- Create: `candidate_profile.toml.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `CandidateProfile` (pydantic model), `load_candidate_profile(path: Path) -> CandidateProfile`, `save_candidate_profile(path: Path, profile: CandidateProfile) -> None`. Tasks 4, 6, 7, and 8 import all three from `career_agent.config`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py` (new imports at the top: `from career_agent.config import (CandidateProfile, CareerBrief, load_boards, load_brief, load_candidate_profile, save_brief, save_candidate_profile)`):

```python
CANDIDATE = """
candidate_name = "Jane Doe"
candidate_email = "jane@example.com"
candidate_phone = "+91-90000-00000"
"""


def test_load_candidate_profile(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    p.write_text(CANDIDATE, encoding="utf-8")
    profile = load_candidate_profile(p)
    assert profile.candidate_name == "Jane Doe"
    assert profile.candidate_email == "jane@example.com"
    assert profile.linkedin_url is None


def test_load_candidate_profile_missing_file_raises_clearly(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    with pytest.raises(FileNotFoundError, match="candidate_profile.toml.example"):
        load_candidate_profile(p)


def test_candidate_profile_rejects_blank_name(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    p.write_text('candidate_name = ""\ncandidate_email = "a@b.com"\n'
                 'candidate_phone = "123"\n', encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        load_candidate_profile(p)


def test_save_candidate_profile_round_trips(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    save_candidate_profile(p, CandidateProfile(
        candidate_name="Jane Doe", candidate_email="jane@example.com",
        candidate_phone="+91-90000-00000", linkedin_url="https://linkedin.com/in/jane"))
    reloaded = load_candidate_profile(p)
    assert reloaded.candidate_name == "Jane Doe"
    assert reloaded.linkedin_url == "https://linkedin.com/in/jane"


def test_save_candidate_profile_creates_a_file_that_does_not_exist(tmp_path):
    p = tmp_path / "new.toml"
    save_candidate_profile(p, CandidateProfile(
        candidate_name="X", candidate_email="x@y.com", candidate_phone="1"))
    assert load_candidate_profile(p).candidate_name == "X"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -k candidate -v`
Expected: FAIL with `ImportError` (`CandidateProfile`/`load_candidate_profile`/`save_candidate_profile` don't exist yet).

- [ ] **Step 3: Refactor `save_brief`'s write-then-rename logic into a shared helper, then add `CandidateProfile`**

In `src/career_agent/config.py`, replace the whole `save_brief` function with:

```python
def _save_toml(path: Path, values: dict) -> None:
    """Write-then-rename TOML save, shared by save_brief and
    save_candidate_profile. Preserves comments, key order, and formatting on
    an existing file. Write-then-rename, not write_text: write_text
    truncates in place, and readers (the worker's guard(), every dashboard
    route) call the matching load_* function on every tick and every
    request. A reader landing in that truncate window gets half a TOML and
    raises. os.replace is atomic on Windows and POSIX; same directory keeps
    it a rename."""
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    for field, value in values.items():
        if value is None:
            # TOML has no null; absent is how "unset" is spelled, and the
            # matching load_* will fall back to the pydantic default.
            if field in doc:
                doc.pop(field)
        elif field not in doc or doc[field] != value:
            # Assigning unconditionally would replace the item wholesale,
            # discarding its original formatting (e.g. a manually wrapped
            # multi-line array) even when the value didn't change.
            doc[field] = value

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_brief(path: Path, brief: CareerBrief) -> None:
    """The file is version controlled and its comments explain non-obvious
    consequences ("adding a city multiplies daily Actor runs"), so a
    round-trip write is the only acceptable kind."""
    _save_toml(path, brief.model_dump())
```

Then add, below `Board`:

```python
class CandidateProfile(BaseModel):
    """PII, deliberately kept out of CareerBrief/career_brief.toml, which is
    version-controlled. See candidate_profile.toml.example."""
    candidate_name: str = Field(min_length=1)
    candidate_email: str = Field(min_length=1)
    candidate_phone: str = Field(min_length=1)
    linkedin_url: str | None = None
    portfolio_url: str | None = None


def load_candidate_profile(path: Path) -> CandidateProfile:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy candidate_profile.toml.example to "
            f"{path.name} and fill in your details -- required before any "
            "real Greenhouse submission can run.")
    with open(path, "rb") as f:
        return CandidateProfile(**tomllib.load(f))


def save_candidate_profile(path: Path, profile: CandidateProfile) -> None:
    _save_toml(path, profile.model_dump())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS, including every pre-existing `test_save_brief_*` test (the refactor must not change `save_brief`'s external behavior).

- [ ] **Step 5: Add the gitignore entry and the example file**

In `.gitignore`, in the `# --- Secrets & personal data ---` section, right after `!.env.example`:

```
!.env.example
candidate_profile.toml
!candidate_profile.toml.example
```

Create `candidate_profile.toml.example`:

```toml
# Copy this file to candidate_profile.toml and fill in your real details.
# candidate_profile.toml is gitignored -- unlike career_brief.toml, this
# file holds PII (name, email, phone), not search preferences, so it never
# gets committed.

candidate_name = "Jane Doe"
candidate_email = "jane@example.com"
candidate_phone = "+91-90000-00000"
linkedin_url = ""
portfolio_url = ""
```

- [ ] **Step 6: Commit**

```bash
git add src/career_agent/config.py .gitignore candidate_profile.toml.example tests/test_config.py
git commit -m "feat: add CandidateProfile config, kept out of tracked career_brief.toml"
```

---

### Task 2: qa_bank store functions

**Files:**
- Modify: `src/career_agent/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `qa_normalize(question: str) -> str`, `qa_lookup(conn, question: str) -> sqlite3.Row | None`, `qa_upsert(conn, question: str, answer: str, is_volatile: bool) -> None`. Task 3's `resolve_answers` calls `qa_lookup`; Task 7's `/answer` route calls `qa_upsert`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`:

```python
def test_qa_normalize_collapses_punctuation_and_case(conn):
    assert store.qa_normalize("Notice period?") == store.qa_normalize("notice period")


def test_qa_lookup_returns_none_for_an_unseen_question(conn):
    assert store.qa_lookup(conn, "Notice period?") is None


def test_qa_upsert_then_lookup_round_trips(conn):
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=True)
    row = store.qa_lookup(conn, "notice period")
    assert row["answer"] == "30 days"
    assert row["is_volatile"] == 1
    assert row["last_confirmed_at"] is not None


def test_qa_upsert_on_an_existing_question_overwrites_and_reconfirms(conn):
    store.qa_upsert(conn, "Notice period?", "30 days", is_volatile=True)
    first = store.qa_lookup(conn, "notice period")["last_confirmed_at"]
    store.qa_upsert(conn, "Notice period?", "60 days", is_volatile=True)
    row = store.qa_lookup(conn, "notice period")
    assert row["answer"] == "60 days"
    assert conn.execute("SELECT COUNT(*) n FROM qa_bank").fetchone()["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_store.py -k qa_ -v`
Expected: FAIL with `AttributeError: module 'career_agent.store' has no attribute 'qa_normalize'`.

- [ ] **Step 3: Implement**

At the top of `src/career_agent/store.py`, add `import re` alongside the existing `import sqlite3`. Then add, after `insert_resume`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: PASS, all tests including pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/store.py tests/test_store.py
git commit -m "feat: add qa_bank reads and writes to store.py"
```

---

### Task 3: NeedsAnswer, FormField, and resolve_answers (pure logic)

**Files:**
- Modify: `src/career_agent/apply/ats.py`
- Test: `tests/test_apply_ats.py`

**Interfaces:**
- Consumes: `store.qa_lookup` (Task 2).
- Produces: `ats.NeedsAnswer` (exception, carries `.question`), `ats.FormField` (pydantic model: `label: str`, `locator: str`), `ats.resolve_answers(questions: list[FormField], conn) -> dict[str, str]`. Task 4's `_default_compute_answers` calls `resolve_answers`; Task 6's `apply_tick` catches `NeedsAnswer` via `submit()`'s `needs_answer` result key (Task 5).

This task adds `resolve_answers` as a free-standing, fully pure function — no Playwright, no `job`/`brief`/`profile` (those are resolved directly by Task 4's caller, before `resolve_answers` ever runs; this function only handles the custom questions a form asks beyond the standard fields). It doesn't wire into `submit()` yet — that's Task 5.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_apply_ats.py` (new imports: `import datetime as dt` at the top; the existing `from career_agent import db` and `from career_agent.apply import ats as ats_apply` stay):

```python
def test_resolve_answers_uses_a_qa_bank_hit(conn):
    store.qa_upsert(conn, "Why this company?", "Great mission fit", is_volatile=False)
    questions = [ats_apply.FormField(label="Why this company?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "Great mission fit"}


def test_resolve_answers_raises_needs_answer_with_no_qa_bank_entry(conn):
    questions = [ats_apply.FormField(label="Notice period?", locator="#q1")]
    with pytest.raises(ats_apply.NeedsAnswer) as exc:
        ats_apply.resolve_answers(questions, conn)
    assert exc.value.question == "Notice period?"


def test_resolve_answers_uses_a_fresh_volatile_answer(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "12 LPA"}


def test_resolve_answers_treats_a_stale_volatile_answer_as_missing(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    conn.execute("UPDATE qa_bank SET last_confirmed_at = datetime('now', '-31 days')"
                 " WHERE question_normalized = ?", (store.qa_normalize("Current CTC?"),))
    conn.commit()
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    with pytest.raises(ats_apply.NeedsAnswer):
        ats_apply.resolve_answers(questions, conn)


def test_resolve_answers_accepts_a_volatile_answer_confirmed_29_days_ago(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    conn.execute("UPDATE qa_bank SET last_confirmed_at = datetime('now', '-29 days')"
                 " WHERE question_normalized = ?", (store.qa_normalize("Current CTC?"),))
    conn.commit()
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "12 LPA"}
```

Add `from career_agent import store` to `tests/test_apply_ats.py`'s imports (test code has no circular-import concern — only `ats.py` importing `store` at module level does).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_apply_ats.py -k resolve_answers -v`
Expected: FAIL with `AttributeError: module 'career_agent.apply.ats' has no attribute 'FormField'`.

- [ ] **Step 3: Implement**

In `src/career_agent/apply/ats.py`, add near the top (after the existing imports, before `RESUME_VERSION`):

```python
import datetime as dt

from pydantic import BaseModel
```

After `class CaptchaEncountered`, add:

```python
class NeedsAnswer(Exception):
    """A form question has no candidate-profile field and no usable
    qa_bank entry. Raised by resolve_answers; callers must park the job
    rather than retry immediately -- see worker.apply_tick's handling."""
    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


class FormField(BaseModel):
    """One custom question read off a live Greenhouse form. Standard
    fields (name/email/phone/resume/links) never appear here -- they're
    answered directly from CandidateProfile before resolve_answers runs."""
    label: str
    locator: str


QA_VOLATILE_WINDOW_DAYS = 30


def _confirmed_within_days(last_confirmed_at: str, days: int) -> bool:
    """last_confirmed_at is a SQLite datetime('now') string: naive, UTC.
    Comparing against dt.datetime.utcnow() (also naive UTC) keeps both
    sides in the same clock -- this project has hit local-vs-UTC datetime
    mismatches as a recurring bug before, so this stays naive-UTC on
    purpose rather than using a timezone-aware "now"."""
    confirmed = dt.datetime.strptime(last_confirmed_at, "%Y-%m-%d %H:%M:%S")
    return (dt.datetime.utcnow() - confirmed) <= dt.timedelta(days=days)


def resolve_answers(questions: list[FormField], conn) -> dict[str, str]:
    """Pure decision logic, no Playwright: for each custom question, use a
    qa_bank hit if it exists and (when volatile) was confirmed inside the
    reconfirmation window; otherwise raise NeedsAnswer. This project does
    not attempt to answer a question from the facts store automatically
    (see the plan's "Deviations from the spec" note) -- every custom
    question goes through qa_bank only."""
    from career_agent import store  # local: store.py imports this module
                                     # (for RESUME_VERSION), so a top-level
                                     # import here would be circular.
    answers = {}
    for q in questions:
        row = store.qa_lookup(conn, q.label)
        if row is None:
            raise NeedsAnswer(q.label)
        if row["is_volatile"] and not _confirmed_within_days(
                row["last_confirmed_at"], QA_VOLATILE_WINDOW_DAYS):
            raise NeedsAnswer(q.label)
        answers[q.locator] = row["answer"]
    return answers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_apply_ats.py -v`
Expected: PASS, all tests including pre-existing ones (this task adds functions without touching `submit()` yet).

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/apply/ats.py tests/test_apply_ats.py
git commit -m "feat: add resolve_answers, a pure qa_bank-driven answer resolver"
```

---

### Task 4: The real Greenhouse filler (DOM-reading and DOM-writing)

**Files:**
- Modify: `src/career_agent/apply/ats.py`

**Interfaces:**
- Consumes: `resolve_answers`, `FormField` (Task 3); `CandidateProfile` (Task 1); `Job` (existing, `models.py`).
- Produces: `_default_compute_answers(url, job, brief, profile, conn) -> dict`, `_default_fill_and_submit(url, answers, resume_path) -> None`, `_generic_stub_compute_answers(url, job, brief, profile, conn) -> dict` (renamed from today's `_default_filler`, same behavior, new signature to match the other two). Task 5's `submit()` wires all three in as the per-source defaults.

No automated test for this task — see "Deviations from the spec" at the top of this plan. `_split_name` is the one pure helper here and gets a real test.

- [ ] **Step 1: Write the one test this task can have**

Add to `tests/test_apply_ats.py`:

```python
def test_split_name_handles_a_single_and_a_multi_word_name():
    assert ats_apply._split_name("Jane") == ("Jane", "")
    assert ats_apply._split_name("Jane Van Doe") == ("Jane", "Van Doe")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_apply_ats.py -k split_name -v`
Expected: FAIL with `AttributeError: module 'career_agent.apply.ats' has no attribute '_split_name'`.

- [ ] **Step 3: Implement**

In `src/career_agent/apply/ats.py`, replace the existing `_default_filler` function (and the `SUBMISSION_IMPLEMENTED`-adjacent comment block above it, which gets rewritten in Task 5) with:

```python
def _split_name(candidate_name: str) -> tuple[str, str]:
    """Greenhouse always asks for first and last name separately.
    Ponytail: a naive single-space split, wrong for a name with more than
    one middle/last word grouping intent -- fine for the common case,
    revisit if it matters."""
    parts = candidate_name.strip().split(" ", 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


# Verify these against a real live Greenhouse posting before flipping
# SUBMISSION_IMPLEMENTED -- they were not confirmed against a rendered
# page from this environment, and Greenhouse has iterated its embed
# markup before. This dict is the one place to fix them if they're wrong.
GREENHOUSE_STANDARD_FIELD_SELECTORS = {
    "first_name": "#first_name",
    "last_name": "#last_name",
    "email": "#email",
    "phone": "#phone",
    "resume": "#resume",
    "linkedin_url": "#job_application_urls_linkedin",
    "portfolio_url": "#job_application_urls_portfolio",
}


async def _check_for_captcha(page, url: str) -> None:
    if await page.locator("iframe[src*='recaptcha'], .h-captcha").count():
        await page.screenshot(path=f"screenshots/captcha-{abs(hash(url))}.png")
        raise CaptchaEncountered(url)


async def _read_custom_questions(page) -> list[FormField]:
    """Scan the form for label+input pairs beyond Greenhouse's standard
    fields. Not unit tested -- see the plan's "Deviations from the spec"
    note; verify against a real posting before trusting this."""
    standard = set(GREENHOUSE_STANDARD_FIELD_SELECTORS.values())
    fields = []
    for container in await page.locator(".field:has(label)").all():
        input_el = container.locator("input, select, textarea").first
        if await input_el.count() == 0:
            continue
        field_id = await input_el.get_attribute("id")
        if not field_id or f"#{field_id}" in standard:
            continue  # a standard field, already answered from the profile
        label_el = container.locator("label").first
        label = (await label_el.inner_text()).strip()
        fields.append(FormField(label=label, locator=f"#{field_id}"))
    return fields


async def _default_compute_answers(url: str, job, brief, profile,
                                   conn) -> dict:
    """The real Greenhouse filler's read-only half: visit the form, decide
    every answer, and return them WITHOUT clicking anything. Raises
    NeedsAnswer (via resolve_answers) for a question nothing can answer,
    and CaptchaEncountered if the page shows one. profile is required here
    (only reached for job.source == 'ats' -- see submit()'s routing)."""
    if profile is None:
        raise RuntimeError(
            "candidate_profile.toml not found -- see"
            " candidate_profile.toml.example. Required before a Greenhouse"
            " draft or send can run.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        try:
            await page.goto(url)
            await _check_for_captcha(page, url)

            first_name, last_name = _split_name(profile.candidate_name)
            sel = GREENHOUSE_STANDARD_FIELD_SELECTORS
            answers = {
                sel["first_name"]: first_name,
                sel["last_name"]: last_name,
                sel["email"]: profile.candidate_email,
                sel["phone"]: profile.candidate_phone,
            }
            if profile.linkedin_url and await page.locator(sel["linkedin_url"]).count():
                answers[sel["linkedin_url"]] = profile.linkedin_url
            if profile.portfolio_url and await page.locator(sel["portfolio_url"]).count():
                answers[sel["portfolio_url"]] = profile.portfolio_url

            questions = await _read_custom_questions(page)
            answers.update(resolve_answers(questions, conn))
            return answers
        finally:
            await browser.close()


async def _default_fill_and_submit(url: str, answers: dict, resume_path) -> None:
    """The real Greenhouse filler's write half: type in answers already
    decided by _default_compute_answers, attach the résumé, and click
    Submit. Makes no decisions of its own."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        try:
            await page.goto(url)
            await _check_for_captcha(page, url)

            for locator, value in answers.items():
                await page.locator(locator).fill(str(value))
            await page.locator(
                GREENHOUSE_STANDARD_FIELD_SELECTORS["resume"]
            ).set_input_files(str(resume_path))
            await page.locator("button[type=submit], input[type=submit]").first.click()
        finally:
            await browser.close()


async def _generic_stub_compute_answers(url: str, job, brief, profile,
                                        conn) -> dict:
    """Non-Greenhouse jobs (job.source != 'ats'): there is no known form
    structure to read, so this proves the page loads and hands back a
    placeholder -- the same behavior every source got before this file's
    v3 changes (this is _default_filler, renamed to match the other two
    functions' signature and to name what it's actually for now that a
    real filler exists alongside it)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        try:
            await page.goto(url)
            await _check_for_captcha(page, url)
            return {"note": "filled from facts store and qa_bank"}
        finally:
            await browser.close()
```

Note: `async_playwright` is imported lazily inside the old `_default_filler` today ("Imported lazily so tests never need a browser"). Keep that exact convention: add `from playwright.async_api import async_playwright` as the first line inside the `async with` block's enclosing scope of each of `_default_compute_answers`, `_default_fill_and_submit`, and `_generic_stub_compute_answers` above — i.e. each function starts with that one-line local import before its `async with async_playwright() as p:` line. Three near-identical one-line imports is simpler than a shared helper for something this small.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_apply_ats.py -v`
Expected: PASS, all tests (this task doesn't touch `submit()`, so nothing else changes behavior yet).

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/apply/ats.py tests/test_apply_ats.py
git commit -m "feat: add the real Greenhouse filler (compute_answers / fill_and_submit)"
```

---

### Task 5: Rewire submit() — provider routing, the SUBMISSION_IMPLEMENTED gate, and the answer-reuse correctness fix

**Files:**
- Modify: `src/career_agent/apply/ats.py`
- Test: `tests/test_apply_ats.py` (full rewrite of the `filler=`-based tests)

**Interfaces:**
- Consumes: `_default_compute_answers`, `_default_fill_and_submit`, `_generic_stub_compute_answers` (Task 4); `NeedsAnswer` (Task 3).
- Produces: `submit(conn, job_id, dry_run, brief=None, profile=None, compute_answers=None, fill_and_submit=None, resume_version=None) -> dict`. The result dict gains a `needs_answer: str` key on the hold case. Task 6's `worker.apply_tick` and Task 7's `app.py` routes call this new signature.

This is a breaking signature change from today's `submit(conn, job_id, dry_run, filler=None, resume_version=None)`. Every existing test in `tests/test_apply_ats.py` that passes `filler=` is being replaced in this task, not preserved.

- [ ] **Step 1: Replace the old filler-based tests with the new contract**

Replace the entire contents of `tests/test_apply_ats.py` with:

```python
import pytest

from career_agent import db, store
from career_agent.apply import ats as ats_apply
from career_agent.config import CandidateProfile


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    c.execute("INSERT INTO job (fingerprint, source, external_id, company,"
              " company_normalized, title, title_normalized, url)"
              " VALUES ('fp','ats','1','Acme','acme','AI Engineer',"
              " 'aiengineer','https://x/apply')")
    c.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
    c.commit()
    return c


PROFILE = CandidateProfile(candidate_name="Jane Doe",
                           candidate_email="jane@example.com",
                           candidate_phone="+91-90000-00000")


async def _compute_ok(url, job, brief, profile, conn):
    return {"note": "filled"}


async def _fill_ok(url, answers, resume_path):
    pass


def test_split_name_handles_a_single_and_a_multi_word_name():
    assert ats_apply._split_name("Jane") == ("Jane", "")
    assert ats_apply._split_name("Jane Van Doe") == ("Jane", "Van Doe")


def test_resolve_answers_uses_a_qa_bank_hit(conn):
    store.qa_upsert(conn, "Why this company?", "Great mission fit", is_volatile=False)
    questions = [ats_apply.FormField(label="Why this company?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "Great mission fit"}


def test_resolve_answers_raises_needs_answer_with_no_qa_bank_entry(conn):
    questions = [ats_apply.FormField(label="Notice period?", locator="#q1")]
    with pytest.raises(ats_apply.NeedsAnswer) as exc:
        ats_apply.resolve_answers(questions, conn)
    assert exc.value.question == "Notice period?"


def test_resolve_answers_uses_a_fresh_volatile_answer(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "12 LPA"}


def test_resolve_answers_treats_a_stale_volatile_answer_as_missing(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    conn.execute("UPDATE qa_bank SET last_confirmed_at = datetime('now', '-31 days')"
                 " WHERE question_normalized = ?", (store.qa_normalize("Current CTC?"),))
    conn.commit()
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    with pytest.raises(ats_apply.NeedsAnswer):
        ats_apply.resolve_answers(questions, conn)


def test_resolve_answers_accepts_a_volatile_answer_confirmed_29_days_ago(conn):
    store.qa_upsert(conn, "Current CTC?", "12 LPA", is_volatile=True)
    conn.execute("UPDATE qa_bank SET last_confirmed_at = datetime('now', '-29 days')"
                 " WHERE question_normalized = ?", (store.qa_normalize("Current CTC?"),))
    conn.commit()
    questions = [ats_apply.FormField(label="Current CTC?", locator="#q1")]
    answers = ats_apply.resolve_answers(questions, conn)
    assert answers == {"#q1": "12 LPA"}


async def test_a_real_send_on_a_greenhouse_job_is_refused_while_the_flag_is_off(conn):
    """SUBMISSION_IMPLEMENTED is False by default -- see the plan's Global
    Constraints. This is the regression guard: a future change must not
    silently flip real sends on."""
    out = await ats_apply.submit(conn, 1, dry_run=False, profile=PROFILE)
    assert out["ok"] is False
    assert out["unsupported"] is True
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_a_real_send_on_a_non_greenhouse_job_is_refused_even_with_the_flag_on(
        conn, monkeypatch):
    conn.execute("UPDATE job SET source = 'linkedin' WHERE id = 1")
    conn.commit()
    monkeypatch.setattr(ats_apply, "SUBMISSION_IMPLEMENTED", True)
    out = await ats_apply.submit(conn, 1, dry_run=False, profile=PROFILE)
    assert out["ok"] is False
    assert out["unsupported"] is True


async def test_an_injected_fill_and_submit_bypasses_both_gates(conn):
    """The gates target the built-in defaults, not real submission in
    general -- an explicitly injected fill_and_submit is always allowed
    through, same as today's injected-filler escape hatch."""
    out = await ats_apply.submit(conn, 1, dry_run=True,
                                 compute_answers=_compute_ok)
    assert out["ok"] is True
    out = await ats_apply.submit(conn, 1, dry_run=False,
                                 fill_and_submit=_fill_ok)
    assert out["ok"] is True
    assert conn.execute(
        "SELECT status FROM application ORDER BY id DESC LIMIT 1"
    ).fetchone()["status"] == "submitted"


async def test_dry_run_routes_to_the_real_greenhouse_filler_by_default(
        conn, monkeypatch):
    """No compute_answers override, job.source == 'ats': submit() must pick
    _default_compute_answers on its own. Monkeypatches the module-level
    default rather than letting it run, so this never touches Playwright."""
    called = {}

    async def fake_default(url, job, brief, profile, conn):
        called["which"] = "greenhouse"
        return {}

    monkeypatch.setattr(ats_apply, "_default_compute_answers", fake_default)
    out = await ats_apply.submit(conn, 1, dry_run=True, profile=PROFILE)
    assert out["ok"] is True
    assert called["which"] == "greenhouse"


async def test_dry_run_routes_to_the_generic_stub_for_non_ats_jobs(
        conn, monkeypatch):
    conn.execute("UPDATE job SET source = 'linkedin' WHERE id = 1")
    conn.commit()
    called = {}

    async def fake_stub(url, job, brief, profile, conn):
        called["which"] = "stub"
        return {}

    monkeypatch.setattr(ats_apply, "_generic_stub_compute_answers", fake_stub)
    out = await ats_apply.submit(conn, 1, dry_run=True, profile=PROFILE)
    assert out["ok"] is True
    assert called["which"] == "stub"


async def test_dry_run_records_a_draft_and_does_not_send(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT * FROM application WHERE job_id = 1").fetchone()
    assert row["status"] == "draft"
    assert row["submitted_at"] is None


async def test_a_real_send_reuses_the_drafts_answers_without_recomputing(conn):
    calls = []

    async def counting_compute(url, job, brief, profile, conn):
        calls.append(1)
        return {"#q1": "yes"}

    captured = {}

    async def capturing_fill(url, answers, resume_path):
        captured["answers"] = answers

    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=counting_compute)
    await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=capturing_fill)

    assert len(calls) == 1, "compute_answers must not run a second time on the real send"
    assert captured["answers"] == {"#q1": "yes"}


async def test_a_draft_does_not_block_a_real_submission(conn):
    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    assert out["ok"] is True
    statuses = {r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1")}
    assert statuses == {"draft", "submitted"}


async def test_second_real_submission_is_refused(conn):
    await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    assert out["ok"] is False
    assert "already" in out["reason"]
    assert conn.execute(
        "SELECT COUNT(*) n FROM application WHERE job_id = 1").fetchone()["n"] == 1


async def test_captcha_during_draft_holds_and_records_no_application(conn):
    async def boom(url, job, brief, profile, conn):
        raise ats_apply.CaptchaEncountered("recaptcha frame present")

    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=boom)
    assert out["held"] is True
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0
    types = {e["type"] for e in conn.execute("SELECT type FROM event")}
    assert "captcha_held" in types


async def test_captcha_during_real_send_holds_and_removes_the_in_flight_row(conn):
    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)

    async def boom(url, answers, resume_path):
        raise ats_apply.CaptchaEncountered("recaptcha frame present")

    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=boom)
    assert out["held"] is True
    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1")]
    assert statuses == ["draft"]  # the in_flight attempt was removed


async def test_needs_answer_is_reported_and_records_no_application(conn):
    async def needs(url, job, brief, profile, conn):
        raise ats_apply.NeedsAnswer("Notice period?")

    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=needs)
    assert out["needs_answer"] == "Notice period?"
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_three_failures_become_failed_permanent(conn):
    await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)

    async def fail(url, answers, resume_path):
        raise RuntimeError("form error")

    for _ in range(ats_apply.MAX_ATTEMPTS):
        out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=fail)
        assert out["ok"] is False

    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM application WHERE job_id = 1 ORDER BY id")]
    assert statuses[-1] == "failed_permanent"

    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok)
    assert out["ok"] is False


def test_stale_in_flight_becomes_held_unknown(conn):
    conn.execute("INSERT INTO application (job_id, resume_version, status,"
                 " started_at) VALUES (1, 'v1', 'in_flight',"
                 " datetime('now', '-30 minutes'))")
    conn.commit()
    assert ats_apply.sweep_stale_in_flight(conn, minutes=15) == 1
    row = conn.execute("SELECT status FROM application").fetchone()
    assert row["status"] == "held_unknown"


async def test_dry_run_stores_the_given_resume_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == "tailored-1-r1"


async def test_dry_run_falls_back_to_the_default_version(conn):
    out = await ats_apply.submit(conn, 1, dry_run=True, compute_answers=_compute_ok)
    assert out["ok"] is True
    row = conn.execute("SELECT resume_version FROM application").fetchone()
    assert row["resume_version"] == ats_apply.RESUME_VERSION


async def test_real_submission_refuses_when_no_resume_is_on_record(conn):
    out = await ats_apply.submit(conn, 1, dry_run=False, fill_and_submit=_fill_ok,
                                 resume_version="tailored-1-r1")
    assert out["ok"] is False
    assert "résumé" in out["reason"] or "resume" in out["reason"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_apply_ats.py -v`
Expected: FAIL — `submit()` still has the old signature (`filler=` doesn't exist as `compute_answers`/`fill_and_submit`; no source-based routing; no `needs_answer` key).

- [ ] **Step 3: Implement**

In `src/career_agent/apply/ats.py`, add the imports needed for typing (`from pathlib import Path` already present; add `from career_agent.config import CandidateProfile, CareerBrief` and `from career_agent.models import Job` near the top, alongside the existing `import json`/`import logging`/`import sqlite3` block — these are safe top-level imports since `config.py` and `models.py` don't import `ats.py`).

Replace the entire `submit()` function with:

```python
async def submit(conn: sqlite3.Connection, job_id: int, dry_run: bool,
                 brief: CareerBrief | None = None,
                 profile: CandidateProfile | None = None,
                 compute_answers=None, fill_and_submit=None,
                 resume_version: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"job {job_id} not found"}
    is_greenhouse = row["source"] == "ats"
    job = Job(source=row["source"], external_id=row["external_id"],
              company=row["company"], title=row["title"],
              location=row["location"], is_remote=bool(row["is_remote"]),
              comp_min=row["comp_min"], comp_max=row["comp_max"],
              posted_at=row["posted_at"], url=row["url"],
              description=row["description"])

    # Refuse a real send with no injected override, before anything is
    # written. Greenhouse jobs are refused only while SUBMISSION_IMPLEMENTED
    # is False (the manual, later "trust it" decision -- see the spec's
    # Rollout section). Every other source is refused unconditionally: no
    # filler exists for "some other website" and none is built in this round.
    if not dry_run and fill_and_submit is None:
        if not is_greenhouse:
            return {"ok": False, "unsupported": True, "reason":
                    "Submission automation only exists for Greenhouse jobs"
                    " today. Apply on the site yourself, then record the"
                    " outcome."}
        if not SUBMISSION_IMPLEMENTED:
            return {"ok": False, "unsupported": True, "reason":
                    "The Greenhouse filler is built but not enabled yet."
                    " Apply on the site yourself, then record the outcome."}

    live = conn.execute(
        f"SELECT status FROM application WHERE job_id = ? AND status IN "
        f"({','.join('?' * len(BLOCKING))})", (job_id, *BLOCKING)).fetchone()
    if live:
        return {"ok": False,
                "reason": f"job {job_id} already has a {live['status']} attempt"}

    resume_version = resume_version or RESUME_VERSION

    def _default_compute_fn():
        return _default_compute_answers if is_greenhouse else _generic_stub_compute_answers

    if dry_run:
        compute_fn = compute_answers or _default_compute_fn()
        try:
            answers = await compute_fn(row["url"], job, brief, profile, conn)
        except CaptchaEncountered as exc:
            conn.execute("INSERT INTO event (job_id, type, payload)"
                         " VALUES (?, 'captcha_held', ?)", (job_id, str(exc)))
            conn.commit()
            return {"ok": False, "held": True,
                    "reason": "captcha encountered; held for review"}
        except NeedsAnswer as exc:
            return {"ok": False, "needs_answer": exc.question}
        conn.execute(
            "INSERT INTO application (job_id, resume_version, answers, status)"
            " VALUES (?, ?, ?, 'draft')",
            (job_id, resume_version, json.dumps(answers)))
        conn.commit()
        return {"ok": True, "job_id": job_id, "status": "draft"}

    # Real send: reuse the draft's own answers rather than recomputing --
    # what a human reviewed (or what auto mode drafted a moment earlier) is
    # exactly what must be sent. Only falls back to computing fresh when
    # submit() is called directly with no draft on record (not reachable
    # through apply_tick's normal path, but submit() stays safe to call
    # this way).
    draft = conn.execute(
        "SELECT answers FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    send_fn = fill_and_submit or _default_fill_and_submit
    if draft is not None and draft["answers"] is not None:
        answers = json.loads(draft["answers"])
    else:
        compute_fn = compute_answers or _default_compute_fn()
        try:
            answers = await compute_fn(row["url"], job, brief, profile, conn)
        except CaptchaEncountered as exc:
            conn.execute("INSERT INTO event (job_id, type, payload)"
                         " VALUES (?, 'captcha_held', ?)", (job_id, str(exc)))
            conn.commit()
            return {"ok": False, "held": True,
                    "reason": "captcha encountered; held for review"}
        except NeedsAnswer as exc:
            return {"ok": False, "needs_answer": exc.question}

    resume_row = conn.execute("SELECT path FROM resume WHERE version = ?",
                              (resume_version,)).fetchone()
    if resume_row is None:
        return {"ok": False,
                "reason": f"no résumé on record for version {resume_version!r}"}

    cur = conn.execute(
        "INSERT INTO application (job_id, resume_version, status, started_at)"
        " VALUES (?, ?, 'in_flight', datetime('now'))", (job_id, resume_version))
    app_id = cur.lastrowid
    conn.commit()

    try:
        await send_fn(row["url"], answers, Path(resume_row["path"]))
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

Delete the now-stale `SUBMISSION_IMPLEMENTED`-adjacent comment block that referenced `_default_filler` by name (it was already rewritten in Task 4); replace it with:

```python
# Real Greenhouse automation exists (see _default_compute_answers /
# _default_fill_and_submit below), but a real send stays refused until
# this is flipped by hand -- see the "Rollout" section of
# docs/superpowers/specs/2026-08-23-auto-submission-design.md. Flipping it
# has no effect on non-Greenhouse jobs, which submit() refuses
# unconditionally regardless of this flag.
SUBMISSION_IMPLEMENTED = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_apply_ats.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/apply/ats.py tests/test_apply_ats.py
git commit -m "feat: rewire submit() with provider routing and answer-reuse fix"
```

---

### Task 6: worker.py — brief/profile plumbing and the NeedsAnswer park

**Files:**
- Modify: `src/career_agent/web/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `submit()`'s new signature and `needs_answer` result key (Task 5); `load_candidate_profile` (Task 1).
- Produces: `apply_tick(conn, brief_path, profile_path)` (signature gains `profile_path`). Task 7's `app.py` call sites (`run_start`, `run_resume`, `apply_worker_loop`'s wiring, `_do_apply`, `send`) all pass the new argument.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_worker.py` (new fixture and imports: add `from career_agent.config import CandidateProfile` at the top):

```python
@pytest.fixture
def profile_path(tmp_path):
    p = tmp_path / "candidate_profile.toml"
    p.write_text('candidate_name = "Jane Doe"\n'
                 'candidate_email = "jane@example.com"\n'
                 'candidate_phone = "+91-90000-00000"\n')
    return p


async def test_apply_tick_passes_brief_and_profile_to_submit(
        conn, brief_path, profile_path, monkeypatch):
    _job(conn, "fp1")
    conn.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    captured = {}

    async def fake_submit(conn, job_id, dry_run, brief=None, profile=None,
                          resume_version=None, **kw):
        captured["brief"] = brief
        captured["profile"] = profile
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path, profile_path)

    assert captured["brief"] is not None
    assert captured["profile"].candidate_name == "Jane Doe"


async def test_apply_tick_tolerates_a_missing_candidate_profile(
        conn, brief_path, tmp_path, monkeypatch):
    """candidate_profile.toml not existing yet must not break drafting for
    a non-Greenhouse job, which never needs it."""
    _job(conn, "fp1")
    conn.execute("INSERT INTO resume (version, path) VALUES ('base-v1', 'r.docx')")
    conn.commit()
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    captured = {}

    async def fake_submit(conn, job_id, dry_run, brief=None, profile=None,
                          resume_version=None, **kw):
        captured["profile"] = profile
        return {"ok": True, "job_id": job_id, "status": "draft"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    missing_profile_path = tmp_path / "no_such_candidate_profile.toml"
    await worker.apply_tick(conn, brief_path, missing_profile_path)

    assert captured["profile"] is None


async def test_apply_tick_parks_on_needs_answer_instead_of_looping(
        conn, brief_path, profile_path, monkeypatch):
    job_id = _job(conn, "fp1")
    worker.set_run_state(conn, "apply", status="running", mode="manual")

    async def fake_submit(conn, job_id, dry_run, brief=None, profile=None,
                          resume_version=None, **kw):
        return {"ok": False, "needs_answer": "Notice period?"}

    monkeypatch.setattr(worker.ats_apply, "submit", fake_submit)

    await worker.apply_tick(conn, brief_path, profile_path)

    state = worker.get_run_state(conn, "apply")
    assert state["current_job_id"] == job_id, "must stay parked, not cleared"
    events = [e["type"] for e in conn.execute("SELECT type FROM event")]
    assert "needs_answer" in events

    # A second tick while parked must not re-attempt: current_job_id is
    # still set, so apply_tick's own early-return guard applies.
    calls = []
    async def counting_submit(*a, **kw):
        calls.append(1)
        return {"ok": False, "needs_answer": "Notice period?"}
    monkeypatch.setattr(worker.ats_apply, "submit", counting_submit)
    await worker.apply_tick(conn, brief_path, profile_path)
    assert calls == [], "a parked run must not re-pick or re-submit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_worker.py -k "brief_and_profile or missing_candidate or needs_answer" -v`
Expected: FAIL — `apply_tick()` doesn't accept a `profile_path` argument yet.

- [ ] **Step 3: Implement**

In `src/career_agent/web/worker.py`, add `from career_agent.config import load_brief, load_candidate_profile` (extend the existing `from career_agent.config import load_brief` import line). Then replace `apply_tick`:

```python
async def apply_tick(conn: sqlite3.Connection, brief_path, profile_path) -> None:
    """One step of the apply worker: pick a candidate, gate it, draft it,
    and in auto mode send it. Called by the control endpoints (for
    immediate feedback) and by the background loop (to keep going
    unattended). A no-op unless the apply run is 'running' and not
    already blocked on a manual-mode draft, or a needs-answer park,
    awaiting review."""
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
        resume_version = await tailor_for_apply(conn, job_id, brief_path)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    brief = load_brief(brief_path)
    try:
        profile = load_candidate_profile(profile_path)
    except FileNotFoundError:
        # Only a Greenhouse draft/send actually needs this -- see
        # ats._default_compute_answers, which raises a clear error itself
        # if it's reached with profile=None. Every other source drafts
        # fine without it, same as before this file needed a profile.
        profile = None

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True,
                                        brief=brief, profile=profile,
                                        resume_version=resume_version)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    if result.get("needs_answer"):
        # Leave current_job_id set: apply_worker_loop's outer check
        # (current_job_id is None) keeps this exact job from being
        # re-picked on the next tick, so a question with no answer is a
        # stop, not a spin. The dashboard's "Answer needed" card
        # (app.py/_run_status.html) is what clears this park.
        store.log(conn, job_id, "needs_answer", result["needs_answer"])
        return

    if not result["ok"]:
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
        set_run_state(conn, "apply", current_job_id=None)
        return

    if state["mode"] == "manual":
        return  # stays 'running' with current_job_id set: awaiting review

    try:
        result = await ats_apply.submit(conn, job_id, dry_run=False,
                                        brief=brief, profile=profile,
                                        resume_version=store.resume_version_for(
                                            conn, job_id))
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return
    if not result["ok"]:
        # captcha hold / permanent failure: reported, not raised
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
    set_run_state(conn, "apply", current_job_id=None)
```

`apply_worker_loop`'s signature already forwards `brief_path` positionally to `apply_tick`; extend it to also accept and forward `profile_path`:

```python
async def apply_worker_loop(conn_factory, brief_path, profile_path) -> None:
    """Keeps the apply run advancing without anyone polling -- the piece
    that makes Start actually mean 'walk away'. conn_factory is a
    zero-arg callable (web/app.py's _conn) so each iteration gets a
    fresh connection, matching the rest of the app's per-call pattern."""
    while True:
        conn = conn_factory()
        state = get_run_state(conn, "apply")
        if state["status"] == "running" and state["current_job_id"] is None:
            try:
                await apply_tick(conn, brief_path, profile_path)
            except Exception as exc:
                set_run_state(conn, "apply", status="error", current_job_id=None,
                              last_error=str(exc))
                store.log(conn, None, "run_error", str(exc))
            await asyncio.sleep(0.1)
        else:
            await asyncio.sleep(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS, all tests. `tests/test_worker.py` has 11 existing calls to `worker.apply_tick(conn, <brief-path-arg>)` with only two arguments (`grep -n "apply_tick(" tests/test_worker.py` to find them all) — every one fails with a `TypeError: apply_tick() missing 1 required positional argument: 'profile_path'` until fixed. For each: add `profile_path` as a parameter to its containing test function (it's a fixture from Step 1, so pytest injects it automatically once named as a parameter) and change the call to `worker.apply_tick(conn, <same-brief-path-arg>, profile_path)` — note one call site (around line 407) uses a locally-renamed brief-path variable (not the `brief_path` fixture directly); only the third argument changes for that one, the first two stay as they are. There is also one direct call to `worker.apply_worker_loop(lambda: conn, brief_path)` (around line 465) — add `profile_path` as that call's third argument too, and `profile_path` as a parameter on its containing test.

- [ ] **Step 5: Commit**

```bash
git add src/career_agent/web/worker.py tests/test_worker.py
git commit -m "feat: thread candidate profile through apply_tick, park on NeedsAnswer"
```

---

### Task 7: app.py and the dashboard — profile plumbing, the answer route, and the "Answer needed" card

**Files:**
- Modify: `src/career_agent/web/app.py`
- Modify: `src/career_agent/web/templates/_run_status.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `apply_tick`/`apply_worker_loop`'s new `profile_path` parameter (Task 6); `store.qa_upsert` (Task 2).
- Produces: `POST /answer/{job_id}` route. `_run_status_context`'s dict gains a `needs_answer_question` key.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`. Note the module is imported there as `from career_agent.web import app as web` — every reference to the app module inside this test file is `web.*`, never `app.*`. First, check the existing `client` fixture (it already stubs `tailor.TEMPLATE_PATH`/`OUTPUT_DIR`, `run_module.verify_auth`, `run_module._ask` per the autouse pattern from `test_worker.py` and `test_web.py`'s own setup) — add candidate profile handling to it: locate the fixture's `tmp_path`-based brief file setup and add a sibling `candidate_profile.toml` write, plus point `web.CANDIDATE_PROFILE_PATH` at it via `monkeypatch.setattr`:

```python
monkeypatch.setattr(web, "CANDIDATE_PROFILE_PATH", tmp_path / "candidate_profile.toml")
(tmp_path / "candidate_profile.toml").write_text(
    'candidate_name = "Jane Doe"\ncandidate_email = "jane@example.com"\n'
    'candidate_phone = "+91-90000-00000"\n')
```

(Insert this alongside wherever the fixture currently does `monkeypatch.setattr(web, "BRIEF_PATH", ...)`.)

Then add:

```python
def test_answer_route_saves_to_qa_bank_and_unparks(client):
    conn = db.connect(web.DB_PATH)
    job_id = conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized)"
        " VALUES ('fp9','ats','9','Acme','acme','AI Engineer','aiengineer')"
    ).lastrowid
    conn.execute("INSERT INTO assessment (job_id, stage, weighted_score,"
                 " verdict, rationale, model, prompt_version)"
                 " VALUES (?, 'scored', 90, 'submit', 'r', 'm', 'v1')", (job_id,))
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=job_id)
    conn.commit()

    r = client.post(f"/answer/{job_id}", data={
        "question": "Notice period?", "answer": "30 days", "is_volatile": "on"})
    assert r.status_code == 200

    row = store.qa_lookup(conn, "Notice period?")
    assert row["answer"] == "30 days"
    assert row["is_volatile"] == 1
    assert worker.get_run_state(conn, "apply")["current_job_id"] is None


def test_run_status_shows_the_answer_needed_card(client):
    conn = db.connect(web.DB_PATH)
    job_id = conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized)"
        " VALUES ('fp9','ats','9','Acme','acme','AI Engineer','aiengineer')"
    ).lastrowid
    worker.set_run_state(conn, "apply", status="running", mode="manual",
                         current_job_id=job_id)
    store.log(conn, job_id, "needs_answer", "Notice period?")
    conn.commit()

    r = client.get("/run/status")
    assert "Answer needed" in r.text
    assert "Notice period?" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k "answer_route or answer_needed_card" -v`
Expected: FAIL with 404 (no `/answer/{job_id}` route yet) and a missing "Answer needed" string.

- [ ] **Step 3: Implement**

In `src/career_agent/web/app.py`:

1. Add `CANDIDATE_PROFILE_PATH = Path("candidate_profile.toml")` right after `BRIEF_PATH = Path("career_brief.toml")`.

2. Extend the `config` import line to also pull in `CandidateProfile`, `load_candidate_profile`, `save_candidate_profile` (used here and in Task 8):

```python
from career_agent.config import (MODEL_LABELS, SCORING_MODELS,
                                 CandidateProfile, CareerBrief, load_brief,
                                 load_candidate_profile, save_brief,
                                 save_candidate_profile)
```

3. In `lifespan`, update the background task creation to pass `CANDIDATE_PROFILE_PATH`:

```python
task = asyncio.create_task(
    worker.apply_worker_loop(_conn, BRIEF_PATH, CANDIDATE_PROFILE_PATH))
```

4. Add a small helper (near `_unpark`) both `_do_apply` and the new route will use:

```python
def _load_candidate_profile_or_none(path: Path) -> CandidateProfile | None:
    try:
        return load_candidate_profile(path)
    except FileNotFoundError:
        return None
```

5. In `_do_apply`, thread brief and profile into the `submit()` call:

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

    brief = load_brief(BRIEF_PATH)
    profile = _load_candidate_profile_or_none(CANDIDATE_PROFILE_PATH)
    try:
        result = await ats_apply.submit(conn, job_id, dry_run=True,
                                        brief=brief, profile=profile,
                                        resume_version=resume_version)
    except Exception as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')
    if result.get("needs_answer"):
        store.log(conn, job_id, "needs_answer", result["needs_answer"])
        return HTMLResponse(
            f'<span class="denied">Answer needed: '
            f'{escape(result["needs_answer"])} — see the status card below.</span>')
    if not result["ok"]:
        return HTMLResponse(f'<span class="denied">{escape(result["reason"])}</span>')
    return HTMLResponse('<span class="done">Applied</span>')
```

(This changes the body of the existing function; the two `@app.post` routes calling it, `apply` and `override`, are unchanged.)

6. In `send()`, thread brief and profile similarly:

```python
@app.post("/send/{job_id}", response_class=HTMLResponse)
async def send(job_id: int):
    conn = _conn()
    draft = conn.execute(
        "SELECT id FROM application WHERE job_id = ? AND status = 'draft'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if draft is None:
        return HTMLResponse(
            '<span class="denied">No draft to send yet. Click Apply first.</span>')

    denial = worker.guard(conn, job_id, allow_skip=True, brief_path=BRIEF_PATH)
    if denial:
        return HTMLResponse(f'<span class="denied">{denial}</span>')

    store.log(conn, job_id, "human_confirmed_send")

    brief = load_brief(BRIEF_PATH)
    profile = _load_candidate_profile_or_none(CANDIDATE_PROFILE_PATH)
    try:
        result = await ats_apply.submit(
            conn, job_id, dry_run=False, brief=brief, profile=profile,
            resume_version=store.resume_version_for(conn, job_id))
    except Exception as exc:
        return HTMLResponse(f'<span class="denied">{escape(str(exc))}</span>')
    if not result["ok"]:
        if result.get("unsupported"):
            _unpark(conn, job_id)
        return HTMLResponse(f'<span class="denied">{escape(result["reason"])}</span>')
    _unpark(conn, job_id)
    return HTMLResponse('<span class="done">Sent</span>')
```

7. Add the new route, right after `_unpark`:

```python
@app.post("/answer/{job_id}", response_class=HTMLResponse)
def answer_question(job_id: int, question: str = Form(...),
                    answer: str = Form(...),
                    is_volatile: str | None = Form(None)):
    conn = _conn()
    store.qa_upsert(conn, question, answer.strip(), is_volatile=bool(is_volatile))
    _unpark(conn, job_id)
    return HTMLResponse('<span class="done">Answer saved</span>')
```

8. Extend `_run_status_context` to expose the parked question:

```python
def _run_status_context(conn) -> dict:
    state = worker.get_run_state(conn, "apply")
    current_job = None
    needs_answer_question = None
    if state["current_job_id"]:
        current_job = conn.execute(
            "SELECT j.id AS job_id, j.company, j.title FROM job j"
            " WHERE j.id = ?", (state["current_job_id"],)).fetchone()
        has_draft = conn.execute(
            "SELECT 1 FROM application WHERE job_id = ? AND status = 'draft'",
            (state["current_job_id"],)).fetchone()
        if not has_draft:
            row = conn.execute(
                "SELECT payload FROM event WHERE job_id = ? AND type = 'needs_answer'"
                " ORDER BY id DESC LIMIT 1", (state["current_job_id"],)).fetchone()
            needs_answer_question = row["payload"] if row else None
    submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
    ).fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'failed_permanent'"
    ).fetchone()["n"]
    gate_skipped = conn.execute(
        "SELECT COUNT(*) n FROM job j JOIN assessment a ON a.job_id = j.id"
        " WHERE j.merged_into_job_id IS NULL AND a.verdict = 'skip'"
        "   AND a.id = (SELECT id FROM assessment a2 WHERE a2.job_id = j.id"
        "               ORDER BY a2.created_at DESC, a2.id DESC LIMIT 1)"
        "   AND NOT EXISTS (SELECT 1 FROM event e WHERE e.job_id = j.id"
        "                     AND e.type = 'human_override')"
        "   AND NOT EXISTS (SELECT 1 FROM application ap WHERE ap.job_id = j.id)"
    ).fetchone()["n"]
    stats = {
        "total_applied": submitted,
        "queued": worker.queue_count(conn),
        "in_progress": 1 if state["current_job_id"] else 0,
        "successful": submitted,
        "failed_skipped": failed + gate_skipped,
    }
    recent_events = conn.execute(
        "SELECT type, payload, occurred_at FROM event"
        " WHERE type NOT LIKE 'pipeline_%'"
        " ORDER BY id DESC LIMIT 10").fetchall()
    return {"run_state": state, "current_job": current_job, "stats": stats,
            "recent_events": recent_events,
            "needs_answer_question": needs_answer_question}
```

9. `run_start` and `run_resume` also call `apply_tick` directly (for immediate feedback rather than waiting on the background loop's next iteration) — both need the new argument too:

```python
@app.post("/run/start")
async def run_start(mode: str = Form(...)):
    conn = _conn()
    worker.set_run_state(conn, "apply", status="running", mode=mode,
                         last_error=None)
    conn.execute("UPDATE run_state SET started_at = datetime('now')"
                 " WHERE kind = 'apply'")
    conn.commit()
    store.log(conn, None, "run_started", mode)
    await worker.apply_tick(conn, BRIEF_PATH, CANDIDATE_PROFILE_PATH)
    return HTMLResponse("ok")


@app.post("/run/resume")
async def run_resume():
    conn = _conn()
    worker.set_run_state(conn, "apply", status="running")
    store.log(conn, None, "run_resumed")
    await worker.apply_tick(conn, BRIEF_PATH, CANDIDATE_PROFILE_PATH)
    return HTMLResponse("ok")
```

`pipeline_run_now` doesn't touch the apply worker, so it's unchanged.

- [ ] **Step 4: Extend `_run_status.html`'s current-job card**

Replace the `{% if current_job %}` block:

```html
{% if current_job %}
<div class="card now-processing">
  <b>{{ current_job["title"] }}</b> at {{ current_job["company"] }}
  {% if needs_answer_question %}
    <span class="rationale">Answer needed: {{ needs_answer_question }}</span>
    <form hx-post="/answer/{{ current_job['job_id'] }}"
          hx-target="#run-status" hx-swap="outerHTML">
      <input type="hidden" name="question" value="{{ needs_answer_question }}">
      <input type="text" name="answer" placeholder="Your answer" required>
      <label><input type="checkbox" name="is_volatile"> This may change later</label>
      <button class="btn" type="submit">Save answer</button>
    </form>
  {% elif s["mode"] == "manual" %}
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests.

- [ ] **Step 6: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/_run_status.html tests/test_web.py
git commit -m "feat: add the answer-needed dashboard flow and thread candidate profile"
```

---

### Task 8: Settings page — Candidate Profile panel

**Files:**
- Modify: `src/career_agent/web/app.py`
- Modify: `src/career_agent/web/templates/settings.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `CandidateProfile`, `load_candidate_profile`, `save_candidate_profile` (Task 1, already imported into `app.py` in Task 7).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web.py`:

```python
def test_settings_page_shows_blank_candidate_fields_on_first_run(client, tmp_path):
    missing = tmp_path / "no_candidate_profile.toml"
    web.CANDIDATE_PROFILE_PATH = missing
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Candidate Profile" in r.text
    assert "could not be read" not in r.text  # first run is not an error


def test_settings_page_saves_a_new_candidate_profile(client, tmp_path):
    target = tmp_path / "fresh_candidate_profile.toml"
    web.CANDIDATE_PROFILE_PATH = target
    r = client.post("/settings", data={
        "candidate_present": "1", "candidate_name": "Jane Doe",
        "candidate_email": "jane@example.com", "candidate_phone": "+91-1",
        "linkedin_url": "", "portfolio_url": "",
        "scoring_model": "claude-sonnet-5", "max_score_per_run": "25",
    })
    assert r.status_code == 200
    assert "Settings saved" in r.text
    assert target.exists()
    saved = load_candidate_profile(target)
    assert saved.candidate_name == "Jane Doe"


def test_settings_page_rejects_a_blank_candidate_name(client, tmp_path):
    target = tmp_path / "fresh_candidate_profile.toml"
    web.CANDIDATE_PROFILE_PATH = target
    r = client.post("/settings", data={
        "candidate_present": "1", "candidate_name": "",
        "candidate_email": "jane@example.com", "candidate_phone": "+91-1",
        "linkedin_url": "", "portfolio_url": "",
        "scoring_model": "claude-sonnet-5", "max_score_per_run": "25",
    })
    assert "Nothing was saved" in r.text
    assert not target.exists()
```

Add `from career_agent.config import load_candidate_profile` to `tests/test_web.py`'s imports (alongside the existing `from career_agent.config import load_brief`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k candidate_profile_page -v`
Expected: FAIL — no "Candidate Profile" text on the settings page yet, and the POST doesn't accept `candidate_name` etc.

- [ ] **Step 3: Implement**

In `src/career_agent/web/app.py`, extend `_settings_context`:

```python
def _settings_context(conn, *, form=None, errors=None, saved=False) -> dict:
    brief = None
    brief_error = None
    try:
        brief = load_brief(BRIEF_PATH)
    except Exception as exc:
        brief_error = f"{BRIEF_PATH} could not be read: {exc}"

    candidate = None
    candidate_error = None
    try:
        candidate = load_candidate_profile(CANDIDATE_PROFILE_PATH)
    except FileNotFoundError:
        pass  # first run: show blank fields, not an error
    except Exception as exc:
        candidate_error = f"{CANDIDATE_PROFILE_PATH} could not be read: {exc}"

    settings = store.get_settings(conn)
    today_submitted = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND date(submitted_at) = date('now')").fetchone()["n"]
    return {"active_nav": "settings", "brief": brief,
            "brief_error": brief_error,
            "candidate": candidate, "candidate_error": candidate_error,
            "daily_cap": brief.daily_cap if brief else "-",
            "today_submitted": today_submitted,
            "settings": settings, "scoring_models": SCORING_MODELS,
            "model_labels": MODEL_LABELS, "form": form or {},
            "errors": errors or {}, "saved": saved}
```

Extend `settings_save`'s form parameters and body:

```python
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
                  daily_cap: str = Form(""),
                  gate_threshold: str = Form(""),
                  staleness_days: str = Form(""),
                  scoring_model: str = Form(...),
                  max_score_per_run: str = Form(""),
                  brief_present: str | None = Form(None),
                  candidate_present: str | None = Form(None),
                  candidate_name: str = Form(""),
                  candidate_email: str = Form(""),
                  candidate_phone: str = Form(""),
                  linkedin_url: str = Form(""),
                  portfolio_url: str = Form("")):
    conn = _conn()
    form = {"target_titles": target_titles, "title_families": title_families,
            "search_locations": search_locations, "locations": locations,
            "work_authorization": work_authorization,
            "excluded_companies": excluded_companies,
            "non_negotiables": non_negotiables, "remote_ok": remote_ok,
            "salary_floor_inr": salary_floor_inr, "daily_cap": daily_cap,
            "gate_threshold": gate_threshold, "staleness_days": staleness_days,
            "scoring_model": scoring_model,
            "max_score_per_run": max_score_per_run,
            "candidate_name": candidate_name, "candidate_email": candidate_email,
            "candidate_phone": candidate_phone, "linkedin_url": linkedin_url,
            "portfolio_url": portfolio_url}
    errors: dict[str, str] = {}

    max_score_per_run_n = _parse_int(
        max_score_per_run, "max_score_per_run", errors)

    daily_cap_n = gate_threshold_n = staleness_days_n = None
    if brief_present:
        daily_cap_n = _parse_int(daily_cap, "daily_cap", errors)
        gate_threshold_n = _parse_int(gate_threshold, "gate_threshold", errors)
        staleness_days_n = _parse_int(staleness_days, "staleness_days", errors)

    brief = None
    if brief_present and None not in (daily_cap_n, gate_threshold_n,
                                      staleness_days_n):
        brief = _validate_brief(
            target_titles, title_families, search_locations, locations,
            work_authorization, excluded_companies, non_negotiables,
            remote_ok, salary_floor_inr, daily_cap_n, gate_threshold_n,
            staleness_days_n, errors)

    candidate = None
    if candidate_present:
        try:
            candidate = CandidateProfile(
                candidate_name=candidate_name, candidate_email=candidate_email,
                candidate_phone=candidate_phone,
                linkedin_url=linkedin_url or None,
                portfolio_url=portfolio_url or None)
        except ValidationError as exc:
            for err in exc.errors():
                field = err["loc"][0] if err["loc"] else "form"
                errors[str(field)] = err["msg"]

    if scoring_model not in SCORING_MODELS:
        errors["scoring_model"] = f"unknown scoring model: {scoring_model}"
    if max_score_per_run_n is not None and max_score_per_run_n < 0:
        errors["max_score_per_run"] = "cannot be negative"

    if errors:
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context=_settings_context(conn, form=form, errors=errors))

    if brief is not None:
        save_brief(BRIEF_PATH, brief)
    if candidate is not None:
        save_candidate_profile(CANDIDATE_PROFILE_PATH, candidate)
    store.save_settings(conn, scoring_model, max_score_per_run_n)
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context=_settings_context(conn, saved=True))
```

Note `candidate_present` always being sent as `"1"` by the template (a hidden field, unconditional — unlike `brief_present`, which is only rendered when the brief loaded, the candidate panel always renders since a missing file is a normal first-run state, not a hidden-on-error state).

- [ ] **Step 4: Update `settings.html`**

Move the `{% macro textfield %}` definition (currently inside the `{% if brief %}` block) to just after `<form method="post" action="/settings">`, so both panels can use it:

```html
<form method="post" action="/settings">
  {% macro textfield(name, label, value, hint="") %}
  <div class="field">
    <label for="{{ name }}">{{ label }}</label>
    <input type="text" id="{{ name }}" name="{{ name }}" value="{{ value }}">
    {% if hint %}<span class="hint">{{ hint }}</span>{% endif %}
    {% if errors.get(name) %}<span class="err">{{ errors[name] }}</span>{% endif %}
  </div>
  {% endmacro %}

  {% if brief %}
  <div class="card panel">
    <div class="panel-head"><h2>Career Brief</h2>
      <span class="run-meta">saves to career_brief.toml</span></div>
    <input type="hidden" name="brief_present" value="1">

    {{ textfield("target_titles", "Target titles", ...) }}
    ...
```

(Delete the old macro definition from inside the `{% if brief %}` block — everything else in that block is unchanged.)

Add a new panel after the `{% if brief %}...{% endif %}` block, before the "Agent Settings" panel:

```html
  <div class="card panel" style="margin-top:11px">
    <div class="panel-head"><h2>Candidate Profile</h2>
      <span class="run-meta">saves to candidate_profile.toml (gitignored, never committed)</span></div>
    {% if candidate_error %}
    <div class="denied">{{ candidate_error }}</div>
    {% endif %}
    <input type="hidden" name="candidate_present" value="1">

    {{ textfield("candidate_name", "Full name",
                 form.candidate_name if form else (candidate.candidate_name if candidate else ""),
                 "Used to fill Greenhouse's name fields.") }}
    {{ textfield("candidate_email", "Email",
                 form.candidate_email if form else (candidate.candidate_email if candidate else "")) }}
    {{ textfield("candidate_phone", "Phone",
                 form.candidate_phone if form else (candidate.candidate_phone if candidate else "")) }}
    {{ textfield("linkedin_url", "LinkedIn URL (optional)",
                 form.linkedin_url if form else (candidate.linkedin_url if candidate else "")) }}
    {{ textfield("portfolio_url", "Portfolio URL (optional)",
                 form.portfolio_url if form else (candidate.portfolio_url if candidate else "")) }}
  </div>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests.

- [ ] **Step 6: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/settings.html tests/test_web.py
git commit -m "feat: add the Candidate Profile panel to the Settings page"
```

---

### Task 9: Rollout callout, README, and full-suite verification

**Files:**
- Modify: `src/career_agent/web/app.py`
- Modify: `src/career_agent/web/templates/applications.html`
- Modify: `README.md`
- Test: `tests/test_web.py`

**Interfaces:** none new — this task is the spec's "Rollout" section made visible, plus closing documentation.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_web.py`:

```python
def test_applications_page_shows_no_rollout_note_while_flag_is_off(client):
    r = client.get("/applications")
    assert "outcomes recorded" not in r.text


def test_applications_page_shows_the_rollout_note_once_the_flag_is_on(
        client, monkeypatch):
    monkeypatch.setattr(web.ats_apply, "SUBMISSION_IMPLEMENTED", True)
    r = client.get("/applications")
    assert "outcomes recorded" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -k rollout_note -v`
Expected: The "flag is off" test passes trivially (nothing to show yet); the "flag is on" test fails since nothing renders the note yet.

- [ ] **Step 3: Implement**

In `src/career_agent/web/app.py`'s `applications()` route, add to the context:

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

    applied = {}
    for r in conn.execute(
            "SELECT id, job_id FROM application WHERE status = 'submitted'"):
        applied[r["job_id"]] = {
            "application_id": r["id"],
            "outcome": outcomes.effective_outcome(conn, r["id"]),
        }

    tailored_sent = conn.execute(
        "SELECT COUNT(*) n FROM application WHERE status = 'submitted'"
        " AND resume_version LIKE 'tailored-%'").fetchone()["n"]
    outcomes_recorded = conn.execute("SELECT COUNT(*) n FROM outcome").fetchone()["n"]

    return templates.TemplateResponse(
        request=request, name="applications.html",
        context={"jobs": rows if show != "skipped" else all_rows,
                 "skipped_jobs": [r for r in all_rows if r["verdict"] == "skip"],
                 "show": show, "scheduled": scheduled_task_installed(),
                 "active_nav": "applications", "brief": brief,
                 "daily_cap": brief.daily_cap,
                 "today_submitted": today_submitted,
                 "applied": applied,
                 "manual_types": outcomes.MANUAL_TYPES,
                 "outcome_labels": overview.CALLBACK_LABELS,
                 "today": _utc_today().isoformat(),
                 "submission_implemented": ats_apply.SUBMISSION_IMPLEMENTED,
                 "tailored_sent": tailored_sent,
                 "outcomes_recorded": outcomes_recorded,
                 **_run_status_context(conn)})
```

In `src/career_agent/web/templates/applications.html`, add right after the mode toggle `</span>` (before the `<div id="run-status-poller" ...>` line):

```html
{% if submission_implemented %}
<p class="rationale">
  {{ tailored_sent }} tailored applications sent so far, {{ outcomes_recorded }}
  outcomes recorded — the original plan for this feature said to wait for real
  evidence tailored applications convert before trusting this.
</p>
{% endif %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Add the README section**

In `README.md`, add a short section (following whatever heading level the existing "Tailored resumes" section from v2 uses):

```markdown
## Real Greenhouse submission (v3)

`career_agent/apply/ats.py` includes a real Greenhouse form-filler, but
`SUBMISSION_IMPLEMENTED` ships `False` — nothing sends for real until you
flip that constant by hand, after you've verified it against real postings.

Setup:

1. Copy `candidate_profile.toml.example` to `candidate_profile.toml` and
   fill in your real name, email, and phone (this file is gitignored).
2. Use manual mode's draft step (Apply, not Send) against a few real
   Greenhouse postings first. Drafting is safe with the flag off — it runs
   the real filler and shows you exactly what it would send, with zero
   real applications going out.
3. Check `career_agent/apply/ats.py`'s `GREENHOUSE_STANDARD_FIELD_SELECTORS`
   dict against what you actually see in those drafts. These selectors were
   not verified against a live Greenhouse posting during development —
   Greenhouse has changed its embed markup before, and this is the one
   place to fix it if the field ids are wrong.
4. Only once you trust the drafts, flip `SUBMISSION_IMPLEMENTED = True`.

Questions the filler can't answer (no `qa_bank` entry, or a stale volatile
one) show up as "Answer needed" on the Applications page's status card —
answer once, and it's remembered for every future application via `qa_bank`.
```

- [ ] **Step 6: Run the full test suite**

Run: `pytest -v`
Expected: every test in the suite passes (this is the final task — confirm nothing from Tasks 1-9 regressed anything from v1/v2).

- [ ] **Step 7: Commit**

```bash
git add src/career_agent/web/app.py src/career_agent/web/templates/applications.html README.md tests/test_web.py
git commit -m "feat: add the rollout evidence callout and v3 setup docs"
```
