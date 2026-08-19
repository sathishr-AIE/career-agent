# Application dashboard: design

Date: 2026-08-19
Revision: 1
Status: open for revision.

Scope of this document: redesigning the existing read-only, per-job dashboard
(`src/career_agent/web/`) into a live, controllable application run — a
Start/Pause/Resume/Stop worker that walks the scored-job queue, plus the
screen that drives and observes it. Discovery and scoring (`career-agent
run`) are unchanged and out of scope.

## Why

Today `career-agent run` only discovers and scores jobs. Applying happens
one job at a time, human-clicked, through `src/career_agent/web/app.py`'s
`/apply`, `/override`, `/send`, `/dismiss` routes. There is no concept of a
queue, a running process, or an auto-apply mode — every submission requires
a person at the keyboard. The goal is a dashboard where starting a run walks
the queue unattended (or with per-job review, in manual mode), while
exposing enough live state that the person driving it always knows what's
running, what's next, and what needs their attention.

## Non-goals

- Changing discovery, scoring, or the hard filter.
- Multi-user auth, remote access, or anything beyond the existing
  single-user localhost dashboard.
- Real-time push (SSE/WebSocket). Polling via htmx, already loaded in
  `index.html`, is sufficient for one local user and keeps the dependency
  footprint unchanged.
- A separate SPA/JSON API. The existing htmx + server-rendered-fragment
  pattern is kept and extended.

## Data model additions

Two additions to `src/career_agent/db.py`'s schema; everything else
(`job`, `assessment`, `application`, `event`) is reused unchanged.

```sql
CREATE TABLE IF NOT EXISTS run_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    status         TEXT NOT NULL DEFAULT 'idle'
                    CHECK (status IN
                     ('idle','running','paused','stopped','error')),
    mode           TEXT NOT NULL DEFAULT 'manual'
                    CHECK (mode IN ('auto','manual')),
    current_job_id INTEGER REFERENCES job(id),
    started_at     TEXT,
    last_error     TEXT,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE application ADD COLUMN priority INTEGER;  -- NULL = score order
```

`run_state` always has exactly one row (seeded on `init_schema`, id=1).
`application.priority` is a manual reorder override: lower sorts first;
`NULL` falls back to today's `weighted_score DESC` order. The `event` table
gains no new columns, only new `type` values (below).

## The worker

A single background `asyncio` task, started with the FastAPI app and never
duplicated (module-level singleton). It is the only writer of `run_state`
besides the control endpoints setting `status`/`mode`.

**Loop**, while `status == 'running'`:

1. Pick the next queue candidate: `assessment.verdict IN ('submit','hold')`,
   no terminal `application.status`, `job.merged_into_job_id IS NULL`,
   ordered by `application.priority ASC NULLS LAST, assessment.weighted_score
   DESC`. If none, set `status = 'completed'`... no such enum value exists;
   completion is represented as `status = 'idle'` with `current_job_id =
   NULL` and an `event(type='run_completed')` row, since "completed" is a
   terminal *result*, not a distinct control state — Idle already means
   "nothing running," and this keeps the status enum exactly the five
   values in the CHECK constraint.
2. Set `current_job_id`, run the existing `_guard()` (pause flag, daily
   cap) unchanged.
   - If `_guard` denies for daily cap: set `status = 'paused'`,
     `last_error = None`, log `event(type='run_autopaused', payload=denial)`.
   - If `_guard` denies for any other reason (gate skip without override,
     global pause): treat as a hard stop condition for this job — log and
     skip to the next job. Global-pause denial should not occur, since a
     paused run never reaches the loop.
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
| `GET /run/status` | HTML fragment (for htmx polling) with `run_state` + current-job summary. |

Existing routes `/apply`, `/override`, `/send`, `/dismiss` are unchanged —
manual per-job action outside of a run still works exactly as it does
today.

## Screen layout

**Top control bar** (sticky). One button whose label/icon/color track
`run_state.status`: `idle`/`stopped`/`error` → **Start** (blue, play icon);
`running` → **Pause** (amber, pause icon) with a smaller red **Stop** beside
it; `paused` → **Resume** (blue) with the same **Stop** beside it. A status
pill next to it echoes `status` in matching color (Idle/Running/Paused/
Stopped/Error). An **Auto ↔ Manual** toggle sits alongside, disabled
whenever `status == 'running'` (mode cannot change mid-job).

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
`run_completed`, `job_skipped`). Polled on the same interval as the stats
row.

**Toasts**: top-right, fired by diffing the activity log's newest `event`
row against the last one the client has seen — no separate notification
table. Triggered on `run_completed`, a job reaching `failed_permanent`,
`run_autopaused`, and a draft becoming ready for review in manual mode.

**Responsive** (below ~768px): stats row becomes a 2-column grid, Queue
rows and job cards stack to one column, the activity log becomes a
bottom drawer toggled by a button instead of a fixed side panel.

## States

**Run** (`run_state.status`): `idle` (nothing started, or a run just
finished — see completion note above) → `running` (auto-applying, or
manual-mode paused-on-one-job awaiting Send/Skip) → `paused` (user-paused,
or auto-paused on daily cap — the UI distinguishes the two by the most
recent activity-log event, `run_paused` vs `run_autopaused`, not by
`run_state` itself: both leave `last_error` unset) → `stopped` (user-stopped; queue and job state untouched, can Start again)
→ `error` (worker raised; `last_error` holds the message; only exit is
Start, which clears it and re-enters `running`).

**Job** (`application.status` + `assessment.verdict`, unchanged from
today): candidate (no `application` row) → `draft` (manual-mode, awaiting
Send) → `in_flight` → terminal: `submitted` (success), `held_unknown`
(needs manual confirmation — existing case, `sweep_stale_in_flight` already
handles the timeout), `failed` (retryable, under `MAX_ATTEMPTS`) /
`failed_permanent` (retries exhausted), or gate `verdict='skip'` /
`human_dismissed` (excluded from the queue).

**Empty/loading/error UI**: empty queue shows "Nothing to apply to — run
discovery, or check Skipped" instead of a blank table (mirrors the existing
"no scheduled run installed" message pattern already in `index.html`);
first load shows skeleton rows instead of a flash of empty content; `error`
run-state shows a persistent banner with `last_error` and a Start-to-retry
button rather than the UI just going quiet.

## Testing

Extend `tests/test_web.py`'s existing pattern (it already exercises
`_guard`, `submit`, and the htmx routes):

- Worker state transitions: start → running → pause → resume → stop, and
  the error path (forced exception from a stubbed `submit`) landing in
  `error` with `last_error` set.
- Queue selection order: score order by default, `priority` override
  respected, terminal-status jobs excluded.
- Manual-mode blocking: worker stops after draft and only continues after
  `/send` or `/queue/{id}/skip`.
- Daily cap mid-run: worker auto-pauses rather than erroring.
- New endpoints: retry only accepts `failed` (not `failed_permanent`),
  priority swap only affects immediate neighbor.

After implementation, use the `run` skill to drive the actual dashboard in
a browser — start a run, pause it, let a manual-mode draft sit for review,
watch stats/activity log/toasts update — before calling this complete.
