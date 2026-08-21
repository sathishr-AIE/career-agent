# Live run monitor: design

Date: 2026-08-21
Revision: 1
Status: open for revision.

Scope: make a pipeline run observable while it happens — stages,
progress, counters, and a streaming activity feed — from a dismissible
modal on the Overview page. Driven by a working HTML prototype
(`preview (2).html`). The pipeline's behaviour is unchanged; only what it
reports about itself is new.

## Why

A real run takes 15–35 minutes: roughly twelve blocking Apify actor
calls, then up to `max_score_per_run` model calls. Today the dashboard
shows a disabled "Running…" button and nothing else for that entire
period. The user sat through a twenty-minute run unable to tell
discovery from scoring, or progress from a hang, and asked what the log
meant — which is the failure this addresses.

The information already exists. Every counter the prototype displays is
computed inside `run.run_once` right now and thrown away:

| Prototype counter | Already computed at |
| --- | --- |
| Jobs Found | `len(jobs)` from `discovery.run_discovery` |
| Duplicates removed | `len(jobs) - new_count` from `store.upsert_jobs` |
| Passed Filter | the hard-filter branch in the scoring loop |
| AI Scored | the `scored` counter in the same loop |
| Shortlisted | derivable from each verdict in that loop |

`run_once` reports none of it until the run ends. That is the gap.

## Non-goals

- Not changing what a run does. No new discovery sources, no scoring
  changes, no schema changes to `job`/`assessment`/`application`.
- **The whole discovery-sources panel is deferred, not just its
  animation.** The prototype fills a bar per source as LinkedIn, Naukri,
  and ATS report in. Discovery is one blocking
  `client.actor(...).call()` per query inside a plain `def`
  (`sources/apify.py`), and `discovery.run_discovery` returns a single
  flat list only once every source has finished — so *no* per-source
  number, final or partial, is available without threading a callback
  down through `run_discovery` into each source adapter. Since the
  per-source counts and the bars that display them come from the same
  missing plumbing, the panel goes out together with the animation. The
  aggregate `found` counter covers the same ground for now.
- Not adding pause/resume/cancel to the pipeline run. The spec's existing
  decision stands: it is one unattended sweep, and a stuck run is a bug
  to fix rather than a state to manage.
- No new dependencies. htmx polling, as everywhere else in this app.

## Decisions

### Progress is reported through a callback, not by `run.py` writing state

`run_once` lives in the CLI module and knows nothing about the web
layer's `run_state` table. It gains one optional parameter:

```python
async def run_once(args, progress=None) -> None:
```

`progress` is a callable taking keyword arguments (`stage=`, `found=`,
`scored=`, …). Omitted, it defaults to a no-op and `career-agent run`
behaves exactly as today. `pipeline.run_background` supplies one that
writes to `run_state`.

This mirrors the pattern already established by `gate.score(job, brief,
facts, ask)`, where the caller injects the side-effecting dependency. The
alternative — importing `web.worker` into `run.py` — would make the CLI
depend on the dashboard and is the wrong direction.

### The callback opens its own connection, inside the worker thread

`pipeline.run_background` runs `run_once` via
`asyncio.to_thread(asyncio.run, ...)`, so everything inside executes on a
worker thread. `sqlite3` connections default to `check_same_thread=True`
and cannot be shared across threads, so the callback must not close over
a connection created on the main thread.

It therefore opens its connection lazily, on first call, inside the
worker thread, and reuses it for the rest of the run. WAL is already
enabled (`db.connect`), so these writes do not block the dashboard's
readers — which matters, because the whole point is that the page stays
responsive while this writes.

### Progress lives on the existing `run_state` row

Six columns are added to `run_state`, used only by the `kind='pipeline'`
row:

```sql
stage        TEXT              -- discover | clean | filter | score | ready
found        INTEGER NOT NULL DEFAULT 0
duplicates   INTEGER NOT NULL DEFAULT 0
passed       INTEGER NOT NULL DEFAULT 0
scored       INTEGER NOT NULL DEFAULT 0
shortlisted  INTEGER NOT NULL DEFAULT 0
```

Added with `db._add_column_if_missing`, the helper already in `db.py` and
already used for `job.priority` — so this is additive and safe against
the populated production database, with no migration step.

`run_state` already carries columns meaningful to only one kind (`mode`
and `current_job_id` are apply-only, documented as such). Pipeline-only
columns follow that precedent, and keep the status endpoint a single-row
read rather than a join against a second table.

Counters are reset to zero when a run starts, not when it ends, so a
finished run's final numbers stay visible until the next one begins.

### Progress percentage is derived, not stored

Storing a percentage would mean `run_once` deciding what fraction of the
work each phase represents — a presentation judgement that would then be
frozen in the database. The template derives it: each of the five stages
carries a coarse band, and within `score` — where the run spends most of
its time — the fine position comes from `scored / max_score_per_run`.

That keeps the bar honest during the long stage instead of parking it at
"60%" for fifteen minutes.

### The activity feed reuses `event`, and must be filtered out of the apply log

Feed lines are `event` rows, which already carry `type`, `payload`, and
`occurred_at`, and where `pipeline_started` / `pipeline_completed` /
`pipeline_error` already live. Progress lines are logged as
`pipeline_progress` with the human-readable message as the payload.

**This breaks something if done naively.** The Applications page's
activity log renders `SELECT ... FROM event ORDER BY id DESC LIMIT 10`
with no type filter, so roughly ten new `pipeline_progress` rows per run
would flood it and push out the apply events it exists to show. That
query must be filtered to exclude `pipeline_%` types — which is the
correct scoping regardless: an apply activity log should show apply
activity.

## Emission points

`run_once` gains calls at the boundaries it already has, and one new
local counter (`shortlisted`) alongside the existing `scored` and
`skipped`:

| Point in `run_once` | Reported |
| --- | --- |
| after `verify_auth`, before discovery | `stage="discover"` |
| after `run_discovery` returns | `found=len(jobs)`, `stage="clean"` |
| after `upsert_jobs` | `duplicates=len(jobs) - new_count`, `stage="filter"` |
| first survivor of the hard filter | `stage="score"` |
| each loop iteration | `passed`, `scored`, `shortlisted` |
| after the loop, before `derive_no_response` | `stage="ready"` |

Per-iteration writes are one `UPDATE` against a single row — at most a
few hundred over a run, on a WAL database, from a background thread.

Each stage transition also logs one `pipeline_progress` event with a
sentence for the feed ("Hard filter is evaluating location, salary,
title, authorization, and exclusions").

## Screen

The prototype's modal, wired to real data. `GET /pipeline/status` grows
to return the full monitor fragment rather than just the button and
pill.

- **Pipeline track** — five stages (Discover, Clean, Filter, Score,
  Ready); stages before the current one render `done`, the current one
  `active`.
- **Progress bar** with percentage and the current phase label.
- **Four counters** — Jobs Found, Passed Filter, AI Scored, Shortlisted.
- **Live activity feed** — `pipeline_progress` events for the current
  run, newest last, scrolled to bottom.
- **Run summary** — shortlisted so far, elapsed time, duplicates removed,
  current stage. (The prototype's discovery-sources panel is not built —
  see Non-goals.)
- **The prototype's standing note is kept verbatim**: *"No automatic
  applications. This run discovers, filters, deduplicates, and scores
  jobs. Applying stays a deliberate human action from the Applications
  flow."* It is true, and it is the same correction recently made in the
  code and README.
- **Footer** — "Continue working" dismisses the modal while the run
  continues; "View results →" appears on completion.
- **Background toast** — shown when the modal is dismissed mid-run
  ("Career Agent is still running…"), and on completion.

### The modal shell must sit outside the polled element

Whether the modal is open is client state, and the poller replaces its
target every three seconds. If the shell lives inside the polled region,
each swap destroys and recreates it, slamming the modal shut — or
reopening one the user closed — every tick.

So the shell and its open/closed class live outside `#pipeline-status-poller`,
and only the *contents* are swapped. This is the same hazard, and the
same fix, as the Auto/Manual toggle on the Applications page, which is
deliberately outside its poller for exactly this reason. Elapsed time is
computed client-side from `started_at` so the timer ticks smoothly
between polls rather than jumping every three seconds.

## States

- **Idle, never run** — modal not shown; the Overview page shows the Run
  Now button as today.
- **Running** — modal opens on click; if the user dismisses it and clicks
  Run Now again while the run is live, the modal reopens against the
  in-progress run rather than starting a second one (the existing
  concurrent-run guard already refuses the second start).
- **Reloading the page mid-run** — the modal does not auto-open, since
  the user did not click anything; the toast shows instead, and clicking
  it opens the monitor. All progress is server-side, so nothing is lost.
- **Completed** — stages all `done`, bar at 100%, "View results →"
  appears, counters hold the final numbers.
- **Error** — the run's `last_error` is shown in the modal against the
  stage that was active when it failed, which is more useful than the
  bare message the Overview page shows today.

## Testing

- `tests/test_run.py` — `run_once` calls `progress` with each expected
  stage in order; the counters it reports match what the run actually
  did; omitting `progress` leaves behaviour unchanged (the CLI path).
- `tests/test_pipeline.py` — the callback writes to the `pipeline` row
  and not the `apply` row; counters reset at the start of a run rather
  than persisting from the previous one.
- `tests/test_db.py` — the six columns are added idempotently and the
  seeded rows keep their defaults.
- `tests/test_web.py` — `/pipeline/status` renders the stage track,
  counters, and feed; the modal shell is outside the polled element (a
  regression guard against reintroducing the swap-clobbers-state bug);
  **the Applications activity log excludes `pipeline_%` events**, which
  fails against today's unfiltered query.

Because a fake `run_once` cannot prove the emission points sit in the
right places, verify once against a real run: trigger Run Now, watch the
stage advance from Discover through Ready, and confirm the counters match
the `discovered N, M new` and `hard-filtered X, scored Y` lines the run
logs at the end.

## Deferred

- **Per-source progress within a source** — see Non-goals.
- **Cancelling a run** from the monitor. The footer has room for it, but
  cancellation means interrupting a blocking Apify call mid-flight, and
  the pipeline has no cancellation story today.
- **Persisting run history** — the monitor shows the current run only.
  A list of past runs with their counters would need a `run` table and is
  a separate feature.
