# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Career Agent is a quality-gated AI job-search agent: it discovers roles, scores them
against a "career brief" before anything is done with them, tailors a resume per role,
and (gated behind a manual kill switch) submits the application. See
[`README.md`](README.md) for the product framing and
[`docs/superpowers/specs/2026-08-18-career-agent-v1-design.md`](docs/superpowers/specs/2026-08-18-career-agent-v1-design.md)
for the original phased design.

## Repo split: `backend/` + `frontend/`

The Python package, tests, and all config/data files live under `backend/` — every
command below runs from there, not the repo root. `frontend/` is a separate Vite + React
SPA that talks to the backend over `/api/*` (see below); it has its own `package.json`
and is not part of the Python package at all. `docs/`, `README.md`, and this file stay at
the repo root since they cover the whole project, not one side of the split.

This split is recent: the dashboard used to be FastAPI serving Jinja2+htmx directly on
`:8000`. That Jinja app still exists and still works (`career-agent serve`) — pages are
being migrated to the React app one at a time, so both run side by side until the
migration finishes. Don't assume `backend/src/career_agent/web/templates/` is dead code.

## Commands

```bash
cd backend
uv venv
uv pip install -e ".[dev]"
playwright install chromium        # needed for the Greenhouse filler / real submission
cp .env.example .env               # CLAUDE_CODE_OAUTH_TOKEN + APIFY_TOKEN
```

- **Run the full test suite:** `cd backend && pytest` (uses the `[dev]` extras: pytest + pytest-asyncio; `asyncio_mode = "auto"` in `pyproject.toml`, so async tests need no marker)
- **Run one file:** `pytest tests/test_apply_ats.py -v`
- **Run one test:** `pytest tests/test_worker.py -k test_apply_tick_parks_on_needs_answer -v`
- **CLI, one-shot discovery+score run:** `career-agent run` (writes to `backend/data/career.db`; this is what a scheduled task calls — see `scripts/install-scheduler.ps1`)
- **CLI, dashboard (Jinja, legacy pages):** `career-agent serve` → http://localhost:8000 — also serves the JSON API other routes are migrating to, at `/api/*`
- **Frontend dev server:** `cd frontend && npm install && npm run dev` → http://localhost:5173, proxies `/api` and `/resume/*` to `:8000` (see `frontend/vite.config.ts`) — needs the backend running to have anything to talk to
- No linter or type checker is configured for the backend. The frontend has `npm run build` (`tsc -b && vite build`) as its type check; `vite build` itself needs a native rolldown binary that this machine's Application Control policy (WDAC) blocks, so `tsc -b` alone is the practical type-check command here — `npm run dev` is unaffected and is what you actually run.

`career-agent run` and `serve` both read `career_brief.toml` and `ats_boards.toml` from
the current directory (`backend/`; `--brief`/`--boards`/`--db` flags override the paths).
Real Greenhouse submission additionally needs `candidate_profile.toml` (copy from
`candidate_profile.toml.example`) — both TOML files holding secrets/PII are gitignored.

## Architecture

### The web layer: one data path, two frontends

`web/context.py` (page data) and `web/actions.py` (mutations — Apply, Send, Skip, Save
Settings, ...) hold every route's actual logic as plain functions returning dicts, not
`HTMLResponse`s. `web/app.py`'s Jinja routes and `web/api.py`'s `/api/*` JSON routes are
both thin wrappers around the same functions — `app.py` renders a template or wraps a
result in an HTML `<span>`, `api.py` returns the dict as JSON. Changing behavior almost
always means editing `context.py`/`actions.py`, not either route file, or the two
frontends drift.

`api.py` reaches shared state (`DB_PATH`, `BRIEF_PATH`, `_conn()`, `_background_tasks`)
through a deferred, call-time import of `app.py` (`api.py`'s `_app()` helper) rather than
a top-level one — `app.py` imports `api.py`'s router to mount it, so a top-level import
back would be circular. This also keeps those path constants a single source of truth:
`tests/test_web.py` monkeypatches them as `web.DB_PATH` etc., and reading them through
the app module from `api.py` picks up the same patched value.

### Two independent state machines, one SQLite DB

`db.py`'s `run_state` table (PK `kind`) tracks two loops that run independently and
never block each other:

- **`pipeline`** — the discover → hard-filter → score run (`run.run_once`, driven from
  the CLI or the dashboard's Run Now button, always synchronous-with-background-thread
  from the dashboard).
- **`apply`** — the apply queue worker (`web/worker.py`'s `apply_tick` /
  `apply_worker_loop`), Start/Pause/Resume/Stop-controlled from `/applications`, in
  either `auto` or `manual` mode.

Every route re-runs `db.init_schema()` per request (`web/app.py`'s `_conn()`), so schema
changes go through `_add_column_if_missing` (idempotent `ALTER TABLE`), never a bare
`CREATE TABLE`. WAL mode is on specifically so the apply worker's background thread and
concurrent dashboard polling don't deadlock on `database is locked`.

### The discovery → gate → apply pipeline

1. **Discovery** (`discovery.py`, `sources/apify.py`, `sources/ats.py`) — three lanes by
   risk: LinkedIn and Naukri via Apify actors, plus direct ATS board scraping
   (Greenhouse only today, `job.source == "ats"`). Naukri is discovery-only, permanently
   — no submission Actor exists or is planned for it, at any version.
2. **Hard filter** (`hardfilter.py`) — deterministic, free, no LLM call; disqualifying
   jobs are persisted via `store.save_hard_skip` so a future run never re-considers them.
3. **Scored gate** (`gate.py`) — the LLM call. Scores five weighted dimensions
   (`Verdict` in `models.py`) against the `CareerBrief`'s facts store, refusing to run at
   all below `MIN_FACTS_HARD` (10) facts. `PROMPT_VERSION` must bump in the same commit
   as any prompt edit — it invalidates every stored assessment and forces a re-score,
   which is what makes a prompt change measurable against `tests/golden/`.
4. **Tailoring** (`tailor.py`) — one resume render per job, memoized. `ensure_tailored`
   reuses the newest existing version rather than re-tailoring on every Apply click;
   `render_docx` fills a master template (`resume/master.docx`) at two literal marker
   paragraphs (`<<SUMMARY>>`, `<<PROJECT_BULLET>>`), never touching anything else in the
   document.
5. **Apply** (`apply/ats.py`, `web/worker.py`) — see below.

### The two-phase Greenhouse filler and the `SUBMISSION_IMPLEMENTED` gate

`apply/ats.py`'s `submit()` always drafts first (`dry_run=True`, writes an
`application` row with `status='draft'` and its computed `answers` JSON) and a later real
send (`dry_run=False`) reuses that draft's answers rather than recomputing — "what was
shown for review is exactly what gets sent."

The filler itself is split for testability: `resolve_answers` (pure, fully unit-tested —
given a form's custom questions, decide each from `qa_bank` or raise `NeedsAnswer`) vs.
the Playwright-driven shell around it (`_default_compute_answers` reads the live DOM,
`_default_fill_and_submit` types the decided answers back in). The shell has no automated
tests by design — verify it manually against a real posting, per the README's setup
steps, before trusting it.

`submit()` routes by `job.source`: only `"ats"` (Greenhouse) gets the real filler, gated
by the module-level `SUBMISSION_IMPLEMENTED` constant, which **ships `False`** — flipping
it is a deliberate manual step, not something any code path does automatically. Every
other source keeps an unconditional refusal regardless of the flag.

A form question with no `qa_bank` entry (or a stale one past the 30-day volatility
window) raises `NeedsAnswer`, and the apply worker parks the run (`current_job_id` stays
set) rather than looping — the dashboard's "Answer needed" card on `/applications` is
what unparks it, via `qa_bank.qa_upsert`.

### Config split: brief vs. profile vs. boards vs. settings

Four different places hold configuration, split by who owns them and how sensitive they
are — don't conflate them:

- `career_brief.toml` (`CareerBrief`, `config.py`) — version-controlled search/apply
  preferences (target titles, locations, daily cap, gate threshold). Written back with
  `save_brief`, which round-trips through `tomlkit` to preserve comments/formatting —
  never `write_text`.
- `candidate_profile.toml` (`CandidateProfile`, `config.py`) — gitignored PII (name,
  email, phone) needed only for a real Greenhouse send. Same `_save_toml` round-trip
  helper as the brief.
- `ats_boards.toml` (`Board`, `config.py`) — which company ATS boards to scrape directly.
- `setting` table (`store.py`, via the Settings page) — operational choices
  (`scoring_model`, `max_score_per_run`) that aren't part of the career brief and don't
  need version control.

## Testing conventions

- Playwright-touching code (the three `*_compute_answers`/`*_fill_and_submit` functions
  in `apply/ats.py`) is exercised only through injected test doubles, never a real or
  synthetic browser page — this project has deliberately avoided being the first to need
  `playwright install chromium` in CI.
- `tests/golden/test_golden.py` checks `gate.score` against hand-labeled real listings —
  run it after any `gate.py` prompt change.
- Datetime comparisons against SQLite's naive-UTC `datetime('now')` strings stay naive
  UTC throughout (never local time, never a timezone-aware "now") — this project has hit
  local-vs-UTC mismatches as a recurring bug class.
