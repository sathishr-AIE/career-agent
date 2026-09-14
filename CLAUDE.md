# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Career Agent is a quality-gated AI job-search agent: it discovers roles, scores them
against a "career brief" before anything is done with them, tailors a resume per role,
and (gated behind a manual kill switch) submits the application. See
[`README.md`](README.md) for the product framing,
[`docs/superpowers/specs/2026-08-18-career-agent-v1-design.md`](docs/superpowers/specs/2026-08-18-career-agent-v1-design.md)
for the original phased design, and
[`docs/superpowers/specs/2026-09-13-chat-first-agent-design.md`](docs/superpowers/specs/2026-09-13-chat-first-agent-design.md)
for the current chat-first interaction model.

## Repo split: `backend/` + `frontend/`

The Python package, tests, and all config/data files live under `backend/` — every
command below runs from there, not the repo root. `frontend/` is a separate Vite + React
SPA that talks to the backend over `/api/*` (see below); it has its own `package.json`
and is not part of the Python package at all. `docs/`, `README.md`, and this file stay at
the repo root since they cover the whole project, not one side of the split.

The dashboard used to be FastAPI serving Jinja2+htmx directly on `:8000`. That Jinja app
still exists and still works (`career-agent serve`), and every one of its pages (`/`,
`/applications`, `/resumes`, `/settings`) now has a React counterpart. The React portal
is chat-first; the old pages render beside the chat as drawer panels. Both frontends
still call the same `context.py`/`actions.py` functions, so don't assume
`backend/src/career_agent/web/templates/` is dead code.

## Commands

```bash
cd backend
uv venv
uv pip install -e ".[dev]"
cp .env.example .env               # CLAUDE_CODE_OAUTH_TOKEN + APIFY_TOKEN + CREDENTIAL_KEY
```

- **Run the full test suite:** `cd backend && pytest` (uses the `[dev]` extras: pytest + pytest-asyncio; `asyncio_mode = "auto"` in `pyproject.toml`, so async tests need no marker)
- **Run one file:** `pytest tests/test_apply_ats.py -v`
- **Run one test:** `pytest tests/test_worker.py -k test_apply_tick_parks_on_needs_answer -v`
- **CLI, one-shot discovery+score run:** `career-agent run` (writes to `backend/data/career.db`; this is what a scheduled task calls — see `scripts/install-scheduler.ps1`)
- **CLI, backend:** `career-agent serve` → http://localhost:8000 — the JSON API at `/api/*` plus the legacy Jinja pages
- **Frontend dev server:** `cd frontend && npm install && npm run dev` → http://localhost:5173, proxies `/api` and `/resume/*` to `:8000` (see `frontend/vite.config.ts`) — needs the backend running to have anything to talk to
- No linter or type checker is configured for the backend. The frontend has `npm run build` (`tsc -b && vite build`) as its type check; `vite build` itself needs a native rolldown binary that this machine's Application Control policy (WDAC) blocks, so `tsc -b` alone is the practical type-check command here — `npm run dev` is unaffected and is what you actually run.

`career-agent run` and `serve` both read `career_brief.toml` and `ats_boards.toml` from
the current directory (`backend/`; `--brief`/`--boards`/`--db` flags override the paths).
Real submission additionally needs `candidate_profile.toml` (copy from
`candidate_profile.toml.example`) — both TOML files holding secrets/PII are gitignored —
plus the `claude` CLI and `npx` on PATH and a real Chrome install (the apply agent drives
Chrome over CDP via a Playwright MCP server; see README's apply-path setup).

## Architecture

### The web layer: one data path, two frontends, chat first

`web/context.py` (page data) and `web/actions.py` (mutations — Apply, Skip, Save
Settings, answering a card, ...) hold every route's actual logic as plain functions
returning dicts, not `HTMLResponse`s. `web/app.py`'s Jinja routes and the JSON routers
(`web/api.py`, `web/api_chat.py`, ...) are thin wrappers around the same functions.
Changing behavior almost always means editing `context.py`/`actions.py`, not a route
file, or the frontends drift.

The API routers reach shared state (`DB_PATH`, `BRIEF_PATH`, `_conn()`,
`_background_tasks`) through a deferred, call-time import of `app.py` (their `_app()`
helper) rather than a top-level one — `app.py` imports the routers to mount them, so a
top-level import back would be circular. This also keeps those path constants a single
source of truth: `tests/test_web.py` monkeypatches them as `web.DB_PATH` etc., and the
routers pick up the same patched value.

Chat is the primary UI (`frontend/src/routes/Chat.tsx` at `/` and `/chat/:id`, polling
every 3 s; `components/chat/` holds the message list, composer, `PromptCard`,
`ConfirmCard` and `Drawer`). `chat.py` owns the `conversation` (one `home`, one per job),
`message` and `agent_prompt` tables; `web/api_chat.py` serves them. A Home message goes
to `actions.home_message`, which classifies it with `web/intent.py`'s `route` (a tool-less
one-shot `claude -p --model haiku --json-schema`, run off the loop). Help, queue and
status answer at once. Pause, resume and stop act directly, but resume only from
`paused` and pause only from `running`. `find_jobs` and `apply_to` spend credits, so they
only open an `approve` card (`origin: home`, dispatched through the `HOME_ACTIONS`
allowlist, expiring after `chat.HOME_PROMPT_TTL` or when a newer card supersedes it).
`POST /api/chat/prompts/{id}/answer` splits by card. A Home card is answered on the event
loop, because an approval starts background tasks. Every other answer runs in
`run_in_threadpool`, because a password fill uses Playwright's sync API, which refuses to
run inside a running event loop (`secret_fill._require_no_running_loop` fails loudly if
that regresses). The pre-chat pages, plus Memory, Logins and Profile, open as drawer
panels.

### Two independent state machines, one SQLite DB

`db.py`'s `run_state` table (PK `kind`) tracks two loops that run independently and
never block each other:

- **`pipeline`** — the discover → hard-filter → score run (`run.run_once`, driven from
  the CLI, the Run Now button, or an approved Home `find_jobs` card).
- **`apply`** — the apply queue worker (`web/worker.py`'s `apply_tick` /
  `apply_worker_loop`), Start/Pause/Resume/Stop-controlled, in either `auto` or `manual`
  mode.

Every route re-runs `db.init_schema()` per request (`web/app.py`'s `_conn()`), so schema
changes go through `_add_column_if_missing` (idempotent `ALTER TABLE`) or
`CREATE TABLE IF NOT EXISTS`. WAL mode is on specifically so the apply run's threads and
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

### The agentic apply engine and the `SUBMISSION_IMPLEMENTED` gate

`ats.submit(conn, job_id, mode, brief, profile, resume_version, run_agent=None,
conn_factory=None, resume=False)` runs one live session per job. `agent.run_session`
spawns `build_cmd`'s `claude -p` with `--input-format stream-json --output-format
stream-json` and an explicit `--session-id`, driving a real Chrome (`apply/chrome.py`)
over CDP through a Playwright MCP server pinned to `@playwright/mcp@0.0.80`.
`apply/runner.py`'s `AgentRun` keeps stdin open. A reader thread turns the stream into
`RunEvents` callbacks, which `ats._chat_events` narrates into the job's conversation (a
connection per event, since they fire off-thread). One writer thread owns stdin through
a queue, so a blocked pipe never stalls the reader or an HTTP answer. The live run sits
in `agent.RUNS[job_id]`, which is how the answer API reaches it.

**Protocol.** Every line is stamped with a per-run nonce, and only lines with that nonce
count. The agent emits `ASK:<nonce>:{json}` (kinds `choice`, `text`, `approve`,
`approve_account`, `need_password`), `CONFIRM:<nonce>:{"fields": [...], ...}` or
`RESULT:<nonce>:<code>`, then ends its turn. `AgentRun._end_of_turn` takes RESULT over
CONFIRM over ASK (the last line of a kind wins), and nudges a turn that has none, up to
`MAX_NUDGES` times. The backend writes `ANSWER:`, `DECISION:` (`approve`/`change`/`cancel`)
or, on a resume, `CONTINUE:`. `timeout_s` counts work time only: the clock pauses while
`run.waiting` is set, and each wait gets `answer_wait_s` before an `answer_timeout` kill.

**Modes.** `mode` is `manual` (every CONFIRM waits for the human's DECISION) or `auto`
(`_chat_events` approves the CONFIRM on the spot, the only approval not given through
`answer_prompt`). `can_submit = SUBMISSION_IMPLEMENTED or run_agent is not None` decides
what an approval means: click Submit and report `APPLIED`, or stop at `DRAFT_READY`.
`SUBMISSION_IMPLEMENTED` still **ships `False`** and gates a real send for every source.

**Cards.** Humans answer cards in the job chat through `actions.answer_prompt`. It
refuses unless the job's run is live and waiting, the card is newer than the run's
`prompt_baseline` and is the job's newest open card, and the answer validates for the
card's kind. It claims the row before sending and reopens it if `run.send` refuses, so an
answer the agent never received never reads as given. A CONFIRM approve writes
`checkpoint.mark_approve_sent` before the DECISION goes out. Only the backend answers
`need_password` (`auto=True`). A secret never goes through a question card: a
`choice`/`text` ASK marked `sensitive`, or whose question or `memory_key` matches
`store._SECRET_QA_RE` (`store.is_secret_card`, which also reads `why`), opens no card — `on_ask` answers `none`
and posts a notice — and `answer_prompt` refuses such a card with 422, as it does a
CONFIRM change to a secret-shaped label (`store.is_secret_label`; ConfirmCard shows those
rows read-only). The old
needs-answer park survives as a fallback: on `RESULT:NEEDS_ANSWER`, `submit` opens a text
card with `origin: needs_answer`, the worker keeps `current_job_id` set, and answering
the card writes `qa_bank` and unparks the job. That is the only park: a draft does not
park the run (its in-session CONFIRM was the review), so a manual worker moves on and
Apply/Continue/Home `apply_to` are not blocked. `ats.PENDING` (process-local) claims a job
from Apply/Continue/worker tick until its run returns, so a second start is refused; each
site releases only a claim it added, and the worker's candidate and auto-resume picks skip
claimed jobs.

**Outcomes** (`_record_outcome`; every non-submit update is guarded on
`status = 'in_flight'`):

- `APPLIED` with `can_submit` becomes `submitted`. If the latest CONFIRM wasn't approved,
  the row is flagged `submitted_without_decision`.
- `APPLIED` without `can_submit` becomes `held_unknown` (`applied_during_draft`).
- `DRAFT_READY` becomes `draft`. CAPTCHA and NEEDS_ANSWER leave no row.
- `PERMANENT_REASONS`, including a human `cancelled`, become `failed_permanent`.
- The unknown-state reasons (`agent_error`, `timeout`, `no_result_line`,
  `unrecognized_result`), and an `answer_timeout` after an approve, become `held_unknown`
  **only when `can_submit`**. Only then could Submit have been clicked.
- Anything else is `failed`, promoted to `failed_permanent` at `MAX_ATTEMPTS`.

`failure_reason` and `transcript_path` record the result.

**Checkpoints and resume** (`apply/checkpoint.py`, `apply_checkpoint`, one row per job).
The row is written at start, on every card and answer, and at the end.

- **Resumable stops.** A `RESUMABLE_REASONS` stop (`timeout`, `answer_timeout`,
  `agent_error`) drops its `in_flight` row and marks the checkpoint `resumable` without
  using an attempt. This only happens if `_might_have_sent` is false: with `can_submit`,
  only a stop while parked on a card (`answer_timeout`, or a cancel while `run.waiting`)
  with no approve sent is resumable (an auto run counts as approved from the start); `timeout`, `agent_error` and a cancel mid-turn hold
  as `held_unknown`, since "no Submit without approve" is only a prompt rule. `QUEUE_WHERE` keeps
  the job out of the queue meanwhile.
- **Resuming.** `submit(resume=True)` reuses the session id and nonce with `--resume`, and
  sends a `CONTINUE` line plus PREVIOUSLY ANSWERED.
  - A session that says nothing within `resume_output_s` falls back to a fresh session
    with the same answers pinned (`checkpoint.restart`).
  - A checkpoint from a different mode or `can_submit` starts fresh.
  - Past `MAX_RESUMES` the job is recorded as a `resume_limit` failure.
- **Continue and auto-resume.** The job chat's Continue calls `actions.resume_job`, which
  resumes in manual mode after `worker.guard`. The auto worker resumes only `mode='auto'`
  checkpoints, once each (`auto_resumed`, re-armed by a human Continue).
- **Startup sweep.** `app.lifespan` runs `worker.startup_sweep` on a plain connection
  before the first `_conn()`. `sweep_orphans` makes an orphaned running or waiting
  checkpoint `resumable`, with two exceptions that are swept `done`: a session that could
  have submitted (`can_submit`, and `approve_sent`, a non-manual mode, or status
  `running` — a crash mid-turn), and a job whose latest application is terminal.
  Orphaned `in_flight` rows are dropped when nothing could have been sent: the
  checkpoint's recorded `can_submit` was 0 (with no checkpoint, the current switch
  decides), or the session was swept resumable. Others stay for `held_unknown`. Each interrupted
  job's chat and Home get a notice, and the resumable count shows in Home `status` and
  the Applications stats.

**Memory.** Remembering an answer stores it under its literal question
(`store.qa_remember`). If the card's `memory_key` is valid snake_case, it also writes a
canonical keyed row. The literal row's `twin_key` points at that keyed row, so a
`qa_update` edit to the keyed row carries to its twins. Answer precedence is APPLICANT
PROFILE, then PREFERENCES (keyed rows, `agent._preferences_section`), then KNOWN ANSWERS
(literal rows), then ASK. A stale volatile row (30-day window) is asked again with its
value as the default. `_SECRET_QA_RE` is a backstop: a question or key shaped like a
password, SSN, PAN or OTP is never stored, whatever the card's `sensitive` flag says.

**Site credentials.** Passwords live in the `site_credential` table, Fernet-encrypted
with `CREDENTIAL_KEY` (`security.py` reads it at call time; `credentials.put/get/list_`).
**The agent never holds a password.** The prompt's KNOWN LOGINS lists domain and email
only.

- **Creating an account.** A human approves an `approve_account` card. `_account_refusal`
  requires an `account_domain` and a `login_url` on it, and tells the agent `exists` if a
  login is already saved. `_create_login` then generates a password.
  `secret_fill.fill_and_submit` fills the field, submits the form and clears it in one CDP
  session while the agent is blocked. The row is stored only after a proven submit
  (`after_submit`).
- **Signing in.** `need_password` is answered by the backend from `on_ask`: it fills the
  saved login the same way (`max_fields=1`), and anything short of a submit answers
  `none`.
- **Fill rules.** `fill_and_submit` fills only https frames whose real URL `host_matches`
  the domain, re-checked per field. It fills only when exactly one form qualifies
  (`ambiguous_form` otherwise). A submit counts only on navigation or field detach.
  Afterwards it scrubs any input on the domain still holding the value, and checks again
  after 500 ms; a value that persists means not submitted.
- **What the agent sees.** The answer is `{"submitted": true}`, never the secret.
  `run.secrets` also redacts it from narration and transcripts.
- **Shared hosts.** `credentials.account_domain` refuses bare labels, IPs, localhost,
  public or shared-platform suffixes, wildcard DNS, shared path-based ATS hosts
  (`boards.greenhouse.io`, `jobs.lever.co`, ...) and non-tenant Workday hosts.
- **Tool lockdown.** `build_cmd` disallows `browser_run_code_unsafe`, `browser_evaluate`
  (it could read `.value`), `browser_network_request(s)` (the login POST body) and
  `browser_take_screenshot`. `chrome.py`'s profile clone skips `Login Data`.

### Config split: brief vs. profile vs. boards vs. settings vs. secrets

Configuration is split by who owns it and how sensitive it is — don't conflate the parts:

- `career_brief.toml` (`CareerBrief`, `config.py`) — version-controlled search/apply
  preferences (target titles, locations, daily cap, gate threshold). Written back with
  `save_brief`, which round-trips through `tomlkit` to preserve comments/formatting —
  never `write_text`.
- `candidate_profile.toml` (`CandidateProfile`, `config.py`) — gitignored PII: name,
  email, phone, plus `gender` (default `"decline"`), `[address]`, `[[work_history]]` and
  `[[education]]`, which the agent uses to fill those form sections. Same `_save_toml`
  round-trip helper as the brief, nested arrays of tables included.
- `ats_boards.toml` (`Board`, `config.py`) — which company ATS boards to scrape directly.
- `setting` table (`store.py`, via the Settings page) — operational choices
  (`scoring_model`, `max_score_per_run`) that aren't part of the career brief and don't
  need version control.
- `.env` — tokens plus `CREDENTIAL_KEY`, the Fernet key for the `site_credential`
  table. Losing it makes every stored login unreadable (`CredentialKeyError`).

## Testing conventions

- `tests/conftest.py`'s autouse `_no_live_apply_agent` fails any test that reaches the
  live engine: `ats._live_run_agent`, `chrome.launch_chrome`, a `subprocess.Popen` of
  `claude`/`npx`/`chrome`, or a CDP connection (`secret_fill._live_connect`,
  `BrowserType.connect_over_cdp`). Hits are asserted at teardown, not only raised,
  because `ats._run` swallows exceptions into `agent_error`. Use the injected seams
  instead:
  - `submit(run_agent=...)`, called as `(prompt, job_id, nonce, events, session_id=None, resume=False)`
  - `run_session(popen=...)` and `AgentRun(popen=...)`, for a fake child over pipes
  - `secret_fill.fill_and_submit(connect=...)` and `page_urls(connect=...)`
  - `intent.route(runner=...)` and `actions.home_message(runner=...)`

  This project has deliberately avoided needing a live browser or spending API credits
  in CI.
- `tests/golden/test_golden.py` checks `gate.score` against hand-labeled real listings —
  run it after any `gate.py` prompt change.
- Datetime comparisons against SQLite's naive-UTC `datetime('now')` strings stay naive
  UTC throughout (never local time, never a timezone-aware "now") — this project has hit
  local-vs-UTC mismatches as a recurring bug class.
