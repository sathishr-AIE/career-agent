# Application dashboard: design

Date: 2026-08-19
Revision: 2
Status: open for revision.

Scope of this document: two pages under a shared app-shell — an **Overview**
analytics landing page (new this revision, driven by a working HTML
prototype) and an **Applications** page: the live, controllable application
run — a Start/Pause/Resume/Stop worker that walks the scored-job queue.
Discovery and scoring logic itself (`discovery.py`, `gate.py`,
`hardfilter.py`) is unchanged; only how it's triggered and observed from the
dashboard is in scope.

## Revision history

**Revision 2.** A working HTML prototype (`preview (1).html`) was built
independently, covering a different screen than Revision 1 assumed: a
pipeline analytics overview (discover → hard-filter → shortlist → apply →
outcome funnel, KPIs, source performance, score distribution, recent
discoveries/outcomes) with its own left-nav app shell, rather than the
Applications queue/worker screen. Resolved as **both**: the prototype
becomes the Overview page reached via a new nav shell; the Revision-1
worker design is kept intact as the Applications page, one level under that
same nav. Added: navigation/IA, the Overview page's real-data mapping for
every prototype element, a `run_state.kind` discriminator so the pipeline's
Run Now button reuses the same run-state mechanism as the apply worker
instead of a second table.

**Revision 1.** Baseline: the Applications queue/worker design (data model,
worker loop, control endpoints, screen layout, states, testing) as detailed
below.

## Why

Today `career-agent run` only discovers and scores jobs, from the command
line or a scheduled task, with no dashboard visibility into that pipeline
beyond its output rows. Applying happens one job at a time, human-clicked,
through `src/career_agent/web/app.py`'s `/apply`, `/override`, `/send`,
`/dismiss` routes, with no queue, running-process, or auto-apply concept.
Two gaps, two pages: an **Overview** that shows what the pipeline actually
found and how it's trending, launchable on demand instead of only overnight;
and an **Applications** page where starting a run walks the apply queue
unattended (or with per-job review, in manual mode), always showing what's
running, what's next, and what needs attention.

## Non-goals

- Changing discovery, scoring, or the hard-filter *logic* — only how their
  results are surfaced and how a run is triggered.
- Multi-user auth, remote access, or anything beyond the existing
  single-user localhost dashboard.
- Real-time push (SSE/WebSocket). Polling via htmx, already loaded in
  `index.html`, is sufficient for one local user.
- A separate SPA/JSON API. The existing htmx + server-rendered-fragment
  pattern is kept and extended.
- Building out every nav item. The prototype's sidebar lists Discoveries,
  Shortlisted, Pipeline, Calendar, Outcomes, and Settings alongside
  Dashboard and Applications — only **Dashboard** (Overview) and
  **Applications** are designed and built this round. The rest are
  placeholder links (present in the shell, `href="#"`, no route behind
  them yet) so the nav reads as a real product without promising pages
  that don't exist. Building one out is a follow-up, not part of this spec.
- Adding fields to `career_brief.toml` that don't already exist there (the
  prototype's mocked "Experience: 1–3 years" and a brief "version" number
  have no backing field — see the Overview section below).

## Navigation / app shell

Every page (Overview, Applications, and the placeholder stubs) shares a
left sidebar: brand mark, nav list, a "Today's Progress" panel, a "Career
Brief" summary panel, and a system-health line. This replaces the current
`index.html`'s two-link top `<nav>` (`Queue` / `Skipped`); those become
tabs *inside* the Applications page instead of top-level nav items (see
Revision 1's Screen layout section, unchanged).

Nav items: **Dashboard** (Overview, this revision's new page, default
route `/`), Discoveries, Shortlisted, **Applications** (Revision 1's
queue/worker page, route `/applications`), Pipeline, Calendar, Outcomes,
Settings. Only Dashboard and Applications resolve to real pages.

## Overview page (new)

Everything below maps one of the prototype's mocked elements to real data
already in the schema — no new tables, one new small endpoint (Run Now).

- **Today's Progress ring**: the prototype mocks "Daily Cap: 150,
  Completed: 102" — an invented number; `career_brief.toml`'s real
  `daily_cap` is 5, and it already gates submissions via `_guard()`. The
  ring shows the *real* thing already enforced: today's submitted-application
  count (`application.status='submitted' AND date(submitted_at)=date('now')`,
  the same query `_guard()` runs) over `brief.daily_cap`.
- **Career Brief panel**: reads `load_brief()`'s real fields only —
  target titles, locations, `remote_ok`, `salary_floor_inr`, `daily_cap`,
  `gate_threshold`. No "Experience" or version fields; those aren't part
  of `career_brief.toml` today (see Non-goals).
- **Five KPI cards** (today's date, each with a 7-day sparkline from
  `GROUP BY date(...)`):
  - *Discovered*: `job.discovered_at` = today.
  - *After Hard Filter*: jobs discovered today with an `assessment` row
    where `stage='scored'` (survived `hardfilter.check`; hard-filter skips
    are `stage='hard'`, per `store.save_hard_skip`).
  - *Shortlisted*: of those, `verdict IN ('submit','hold')`.
  - *Applied*: `application.status='submitted'`, today.
  - *Responses*: `outcomes.callback_rate()`'s callback count (`screen`,
    `interview`, `offer` — that function and its `CALLBACK_TYPES` already
    exist in `src/career_agent/outcomes.py` and are reused as-is).
- **Recent Discoveries table**: latest `job` rows joined to their
  `assessment`, with a Pass/Review/Fail gate badge derived from
  `stage`+`verdict`: hard-filter skip (`stage='hard'`) → Fail; scored
  skip (`stage='scored', verdict='skip'`) → Review, not Fail — the
  existing Skipped tab already lets a human override it via "Apply
  anyway," so it isn't a dead end the way a hard-filter skip is; `submit`/
  `hold` → Pass. Plus the numeric score.
- **Pipeline funnel**: the same five KPI counts restated as a funnel,
  five stages wide.
- **Outcome summary**: Applied / Responses / Interviews / Offers counts
  this month, plus Callback Rate and Interview Rate — both derived the
  same way `outcomes.callback_rate()` already computes callback rate,
  generalized to per-outcome-type rates.
- **Source performance table**: per `job.source`, discovered count, hard-filter
  pass rate, shortlist rate, applied count, response rate — new grouped
  queries, no new columns; every value already exists per-job.
- **Score distribution donut**: `assessment.weighted_score` bucketed into
  four bands (80–100/60–79/40–59/0–39) among `stage='scored'` rows.
- **Recent Outcomes list**: latest `outcome` rows, status label from
  `outcomes.effective_outcome()` (`screen`→"Response", `interview`→
  "Interview", `offer`→"Offer", `rejected`→"Rejected", `no_response`→
  "No Response"; an application with no outcome row yet shows "Applied"),
  age from `occurred_at`.
- **"Ready to apply?" CTA**: shortlisted count, links to `/applications`.
  Does not itself apply to anything, matching the prototype's "review and
  apply manually" framing — actual applying, auto or manual, is entirely
  the Applications page's job.
- **Run Now button**: triggers discovery + hard-filter + score only
  (equivalent to `career-agent run`, no applying) via a new
  `POST /pipeline/run-now`, running `run.run_once`'s body as a background
  `asyncio` task the same way the Applications worker runs (below). Button
  states (idle/running/completed/error) come from polling
  `GET /pipeline/status`. No pause/resume — it's one unattended sweep, not
  a walkable queue, so there's nothing mid-run to pause.

## Data model additions

One shared table plus one column; everything else (`job`, `assessment`,
`application`, `event`) is reused unchanged.

```sql
CREATE TABLE IF NOT EXISTS run_state (
    kind           TEXT PRIMARY KEY CHECK (kind IN ('pipeline','apply')),
    status         TEXT NOT NULL DEFAULT 'idle'
                    CHECK (status IN
                     ('idle','running','paused','stopped','error')),
    mode           TEXT CHECK (mode IN ('auto','manual')),      -- 'apply' only
    current_job_id INTEGER REFERENCES job(id),                  -- 'apply' only
    started_at     TEXT,
    last_error     TEXT,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
-- seeded with two rows on init_schema: ('pipeline', 'idle', ...), ('apply', 'idle', ...)

ALTER TABLE job ADD COLUMN priority INTEGER;  -- NULL = score order
```

One table, keyed by `kind`, instead of two near-identical ones. The
`pipeline` row only ever uses `status` and `last_error`; like the apply
worker's completion (below), a finished pipeline run goes back to `idle`
plus an `event(type='pipeline_completed')` row rather than a distinct
"completed" status, since the CHECK constraint's five values are shared
across both kinds. `mode` and `current_job_id` stay `NULL` for the
`pipeline` row — they're meaningless outside `apply`. `job.priority` and
the `event` table are unchanged from Revision 1.

## The pipeline runner (Overview page)

A background `asyncio` task, distinct from the apply worker below, started
on `POST /pipeline/run-now` (only valid when the `pipeline` row's
`status` is `idle`/`error`). It runs the same steps as `run.run_once` —
discovery, upsert/dedupe, hard filter, scoring, `outcomes.derive_no_response`
— against the dashboard's own `DB_PATH`/`BRIEF_PATH`, updating the
`pipeline` row's `status`/`last_error` as it goes, and logs
`event(type='pipeline_started'|'pipeline_completed'|'pipeline_error')`. No
control endpoints beyond start — it either finishes or errors; a stuck
pipeline is a bug to fix, not a state to pause.

## The apply worker (Applications page)

Unchanged from Revision 1: a single background `asyncio` task, started
with the FastAPI app, the only writer of the `apply` row in `run_state`
besides its control endpoints.

**Loop**, while the `apply` row's `status == 'running'`:

1. Pick the next queue candidate: `assessment.verdict IN ('submit','hold')`,
   no terminal `application.status` (none at all, or `failed`, which is
   retryable), `job.merged_into_job_id IS NULL`, ordered by
   `job.priority ASC NULLS LAST, assessment.weighted_score DESC` — priority
   lives on `job`, not `application`, because most candidates have no
   `application` row yet to attach it to. If none, set `status = 'idle'`,
   `current_job_id = NULL`, and log
   `event(type='run_completed')` — "completed" is a terminal *result*, not
   a distinct control state; idle already means "nothing running," and
   this keeps the status enum exactly the five values in the CHECK
   constraint.
2. Set `current_job_id`, run the existing `_guard()` (pause flag, daily
   cap) unchanged.
   - If `_guard` denies for daily cap: set `status = 'paused'`, log
     `event(type='run_autopaused', payload=denial)`.
   - If `_guard` denies for any other reason (gate skip without override,
     global pause): log and skip to the next job. Global-pause denial
     should not occur, since a paused run never reaches the loop.
3. Call `ats_apply.submit(conn, job_id, dry_run=True)` (identical to
   today's `/apply`).
   - **Auto mode**: immediately call `submit(conn, job_id, dry_run=False)`
     (identical to today's `/send`), then continue the loop.
   - **Manual mode**: stop after the draft. Leave `status = 'running'` but
     `current_job_id` pointed at this job with no further progress — the
     "Now Processing" banner reads this as "awaiting your review." The loop
     itself blocks (awaits an `asyncio.Event`) until `/send/{job_id}` is
     called from the UI (existing route, unchanged) or `/queue/{id}/skip`
     is called, either of which wakes the worker to continue.
4. On any exception from `submit()`: catch it, set `status = 'error'`,
   `last_error = str(exc)`, log `event(type='run_error')`. The loop stops
   (it does not silently retry) — the existing `MAX_ATTEMPTS`/
   `failed_permanent` logic inside `ats_apply.submit` already handles
   per-job retry accounting; this is for the *worker* dying, which is a
   distinct failure a person must acknowledge.

**Control endpoints** (`src/career_agent/web/app.py`):

| Route | Effect |
|---|---|
| `POST /run/start` (body: `mode`) | Only valid from `idle`/`stopped`/`error`. Sets `mode`, `status='running'`, `started_at=now`, wakes the worker task. |
| `POST /run/pause` | Valid from `running`. Sets `status='paused'`. |
| `POST /run/resume` | Valid from `paused`. Sets `status='running'`, wakes the worker. |
| `POST /run/stop` | Valid from `running`/`paused`. Sets `status='stopped'`, `current_job_id=NULL`. Queue and all job state are untouched — stopping is not deleting. |
| `POST /queue/{id}/retry` | Only for `application.status='failed'` (not `failed_permanent`). Clears the failed row's blocking effect so the job is picked up again next loop iteration. |
| `POST /queue/{id}/skip` | Logs `event(type='job_skipped')`, wakes the worker if it was blocked awaiting review on this job, moves to the next candidate. Does not touch `assessment.verdict`. |
| `POST /queue/{id}/priority` (body: `direction: up\|down`) | Swaps this job's `priority` with its immediate neighbor's in queue order; assigns initial priorities lazily (current rank) the first time any job in the queue is reordered. |
| `GET /run/status` | HTML fragment (for htmx polling) with the `apply` row + current-job summary. |

Existing routes `/apply`, `/override`, `/send`, `/dismiss` are unchanged —
manual per-job action outside of a run still works exactly as it does
today.

## Applications page — screen layout

(Unchanged from Revision 1; reached via the `/applications` nav item
instead of being the dashboard's only page.)

**Top control bar** (sticky). One button whose label/icon/color track the
`apply` row's `status`: `idle`/`stopped`/`error` → **Start** (blue, play
icon); `running` → **Pause** (amber, pause icon) with a smaller red
**Stop** beside it; `paused` → **Resume** (blue) with the same **Stop**
beside it. A status pill next to it echoes `status` in matching color
(Idle/Running/Paused/Stopped/Error). An **Auto ↔ Manual** toggle sits
alongside, disabled whenever `status == 'running'` (mode cannot change
mid-job).

**Stats row**: five tiles — Total Applied (`application.status='submitted'`
count), Queued (candidates per the worker's selection query), In Progress
(1 if `current_job_id IS NOT NULL` else 0), Successful (same as Total
Applied), Failed/Skipped (`failed_permanent` count + skip-verdict jobs not
overridden). Polled every few seconds alongside `/run/status`.

**Now Processing banner**: while `current_job_id IS NOT NULL`, shows that
job's company/title, a thin progress bar ("Job 4 of 12 this run" — position
within the candidates selected when the run started), and an "Up Next"
preview (the next candidate in queue order). Collapses to nothing when
`current_job_id IS NULL`. In manual mode with a job awaiting review, the
banner additionally shows **Send** / **Skip** buttons inline (same actions
as the Queue tab's row actions).

**Main content — tabs**: `Queue` | `All Applications` | `Skipped`. Each has
a search box (company/title), status filter chips, and a sort dropdown
(score/date/company).

- *Queue tab*: ordered rows in worker-selection order — position number,
  priority controls (▲▼), status chip (queued / awaiting review /
  applying), and row actions: Skip and Remove always available; Retry only
  on `failed` rows; a Pause-this-job affordance only makes sense on the
  currently-processing row and is really just the top-bar Pause, so it is
  not duplicated per-row.
- *All Applications tab*: the job-card grid, one card per job — company,
  role, location, status badge (mirrors `index.html`'s existing
  `terminal_status` branches), timestamp, quick actions (View posting,
  Retry if failed, Dismiss).
- *Skipped tab*: today's `?show=skipped` view, unchanged, with the same
  "Apply anyway" override action.

**Activity log**: a collapsible panel streaming `event` rows newest-first
(existing types — `human_applied`, `human_override`, `human_confirmed_send`,
`human_dismissed`, `apply_failed` — plus new ones: `run_started`,
`run_paused`, `run_resumed`, `run_stopped`, `run_autopaused`, `run_error`,
`run_completed`, `job_skipped`, `pipeline_started`, `pipeline_completed`,
`pipeline_error`). Polled on the same interval as the stats row.

**Toasts**: top-right, fired by diffing the activity log's newest `event`
row against the last one the client has seen — no separate notification
table. Triggered on `run_completed`, `pipeline_completed`, a job reaching
`failed_permanent`, `run_autopaused`, and a draft becoming ready for
review in manual mode.

**Responsive** (below ~768px): stats row becomes a 2-column grid, Queue
rows and job cards stack to one column, the activity log becomes a
bottom drawer toggled by a button instead of a fixed side panel. The
Overview page's sidebar (nav + progress ring + brief panel) collapses to a
top bar with a 2-column nav grid, matching the prototype's own
`max-width:760px` behavior.

## States

**Apply run** (`run_state` row where `kind='apply'`): `idle` (nothing
started, or a run just finished) → `running` (auto-applying, or
manual-mode paused-on-one-job awaiting Send/Skip) → `paused` (user-paused,
or auto-paused on daily cap — the UI distinguishes the two by the most
recent activity-log event, `run_paused` vs `run_autopaused`, not by
`run_state` itself: both leave `last_error` unset) → `stopped`
(user-stopped; queue and job state untouched, can Start again) → `error`
(worker raised; `last_error` holds the message; only exit is Start, which
clears it and re-enters `running`).

**Pipeline run** (`kind='pipeline'`): `idle` → `running` → `idle` on
success (with a `pipeline_completed` event marking it) or `error` on
failure (`last_error` set; Run Now clears it and retries).

**Job** (`application.status` + `assessment.verdict`, unchanged from
today): candidate (no `application` row) → `draft` (manual-mode, awaiting
Send) → `in_flight` → terminal: `submitted` (success), `held_unknown`
(needs manual confirmation — existing case, `sweep_stale_in_flight` already
handles the timeout), `failed` (retryable, under `MAX_ATTEMPTS`) /
`failed_permanent` (retries exhausted), or gate `verdict='skip'` /
`human_dismissed` (excluded from the queue).

**Empty/loading/error UI**: an empty Applications queue shows "Nothing to
apply to — run discovery, or check Skipped" instead of a blank table
(mirrors the existing "no scheduled run installed" message pattern already
in `index.html`); an Overview page with zero jobs in the DB shows the same
kind of message instead of five zero-value KPI cards; first load shows
skeleton rows/cards instead of a flash of empty content; `error` run-state
(either kind) shows a persistent banner with `last_error` and a
Start/Run-Now-to-retry button rather than the UI just going quiet.

## Testing

Extend `tests/test_web.py`'s existing pattern (it already exercises
`_guard`, `submit`, and the htmx routes):

- Apply-worker state transitions: start → running → pause → resume →
  stop, and the error path (forced exception from a stubbed `submit`)
  landing in `error` with `last_error` set.
- Queue selection order: score order by default, `priority` override
  respected, terminal-status jobs excluded.
- Manual-mode blocking: worker stops after draft and only continues after
  `/send` or `/queue/{id}/skip`.
- Daily cap mid-run: worker auto-pauses rather than erroring.
- New queue endpoints: retry only accepts `failed` (not `failed_permanent`),
  priority swap only affects immediate neighbor.
- Pipeline run-now: only starts from `idle`/`error`; state reaches `idle`
  with a `pipeline_completed` event on success, `error` with `last_error`
  set on a stubbed failure.
- Overview queries: KPI counts, source-performance grouping, and score-
  distribution buckets against a small fixture DB with known job/assessment/
  application/outcome rows — the query, not the rendering, is what can
  silently drift from the schema.

After implementation, use the `run` skill to drive the actual dashboard in
a browser on both pages — trigger Run Now and watch it complete, start an
apply run, pause it, let a manual-mode draft sit for review, watch
stats/activity log/toasts update — before calling this complete.
