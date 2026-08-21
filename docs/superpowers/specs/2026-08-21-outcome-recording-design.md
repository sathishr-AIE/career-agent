# Outcome recording: completing v1

Date: 2026-08-21
Revision: 1
Status: open for revision.

Scope: close the last gap in v1 by making it possible to record that you
applied to a job yourself, and to record what came back. Discovery, the
hard filter, the gate, and the dashboard are unchanged.

## Why

The v1 design lists `outcome tracking` among what v1 delivers, and makes
the v1 → v2 gate *"The gate agrees with your judgment, **and callback
data exists**."* Today the dashboard can display outcomes but cannot
record one, so that gate can never be met and v2 (tailoring) and v3
(auto-submission) stay blocked behind it.

The gap is wider than a missing form. Measured against the live database:

```
applications:  draft 9        <- nothing is 'submitted'
callback_rate: (0, 0)         <- denominator is zero
outcome rows:  0
```

`outcome.application_id` is a foreign key to `application`, and
`outcomes.callback_rate` counts only applications with
`status = 'submitted'`. The only code path that has ever set
`'submitted'` is `ats.submit(dry_run=False)` — the fake submitter, now
correctly refused (`ats.SUBMISSION_IMPLEMENTED = False`) because
`_default_filler` opens the page and returns a hardcoded dict without
touching the form.

So there is no submitted application to attach an outcome to, and no way
to create one. The original spec assumed the agent would produce
submitted rows; with apply manual, the human has to. **Recording that you
applied is therefore part of this change, not a prerequisite someone else
supplies.**

## Non-goals

- Not implementing real submission. That is v3, gated on tailoring plus
  outcome evidence, and Naukri is discovery-only at every version.
- Not building the Outcomes nav page. The controls go on the existing
  Applications screen; the nav stub stays a stub.
- Not editing or deleting outcome rows. The table is append-only and
  supersession handles corrections (below).
- Not automating outcome capture. Reading email and classifying replies
  is v2's response loop.
- No new dependencies, no new tables, no schema changes at all.

## Decisions

### Recording an outcome needs a submitted application first

Two writes, in order:

1. **Mark applied** — the human says "I applied to this on the site."
   Produces an `application` row with `status = 'submitted'` and a
   `submitted_at` date. This is the callback-rate denominator.
2. **Record outcome** — the human says what came back. Produces an
   `outcome` row with `derived = 0`.

### `'submitted'` means "this application exists in the world"

Marking applied reuses the existing `'submitted'` status rather than
adding a new one. The status answers "did this application reach the
company", which is what `callback_rate` needs; *who* performed the
submission is provenance, and the `event` table already carries that
(`human_applied`, `human_override`, `human_confirmed_send`). Adding a
`manually_submitted` status would mean touching the CHECK constraint, the
partial unique index, `LIST_SQL`'s `terminal_status`, the worker's
candidate query, and `callback_rate` — a schema change rippling through
five call sites to record something an event row already records.

A new event type, `human_marked_applied`, is logged so the distinction is
recoverable.

### `no_response` stays underivable by hand

The spec's reasoning holds and the UI must enforce it: *"nobody writes to
say they are ignoring you... Left manual, the table would fill with
rejections and screens and stay silent on the majority case."* The four
manual types are `rejected`, `screen`, `interview`, `offer`. The schema's
CHECK would accept `no_response`, so the write path rejects it explicitly
rather than relying on the form not offering it.

`derive_no_response` continues to write it automatically after 30 days,
which is exactly why the `submitted_at` date must be right — see below.

### Corrections work by superseding, not editing

`outcomes.effective_outcome` already resolves "latest `occurred_at` wins,
derived loses ties". Recording a corrected outcome therefore overrides the
earlier one without deleting it, and the history stays intact — the spec's
stated intent: *"A reply arriving on day 40 overrides the day-30
`no_response` without deleting it, and the history of 'we assumed silence,
then heard back' stays intact."*

No delete or edit UI. Un-marking a mistakenly-applied job is deferred; see
Deferred.

### The applied date is editable, defaulting to today

`derive_no_response` counts 30 days from `submitted_at`, so a wrong date
silently shifts when a job is treated as ignored. Backfilling a job you
applied to last week must be possible. The field defaults to today so the
common case stays one click.

## Data model

**No schema changes.** Both writes use existing tables and existing
columns. The only new value anywhere is one `event.type` string
(`human_marked_applied`), and `event.type` is free-text by design.

Two constraints already in the schema do useful work here and must be
respected rather than worked around:

- `one_live_application_per_job` — a partial unique index over
  `status IN ('in_flight','submitted','held_unknown','failed_permanent')`.
  It makes double-marking a job structurally impossible: a second
  mark-applied on the same job raises `IntegrityError`. The write path
  catches that and reports it as a readable message rather than a 500.
- `outcome.type`'s CHECK — a backstop under the explicit `no_response`
  rejection above.

## Write paths

Two functions, each in the module that already owns that concern.

**`store.mark_applied(conn, job_id, when) -> int`** — application
lifecycle, so it sits beside the other `application` writers in
`store.py`. Returns the application id.

If the job has a `draft` row (the normal case, from "Open & track"), that
row is promoted: `status = 'submitted'`, `submitted_at = when`. Promoting
rather than inserting keeps one row per application attempt and preserves
whatever the draft recorded.

If the job has no application row at all — you applied directly from the
job board without tracking it first — one is inserted with
`resume_version = ats.RESUME_VERSION` and **`answers = NULL`**. Null is
the honest value: for a manual application the system genuinely does not
know what was sent. It must not inherit `_default_filler`'s placeholder
string, which would assert knowledge that does not exist.

Logs `human_marked_applied`.

**`outcomes.record(conn, application_id, type_, occurred_at, notes)`** —
`outcomes.py` already owns `effective_outcome`, `callback_rate`, and the
`derive_no_response` writer, so the manual writer belongs there too.
Inserts with `derived = 0`. Raises on `no_response` and on any type
outside the four manual ones.

## Routes

Both on the existing Applications screen, following the file's
established pattern — open a connection via `_conn()`, return an
`HTMLResponse` fragment for htmx to swap.

| Route | Body | Effect |
|---|---|---|
| `POST /applied/{job_id}` | `when` (date, defaults to today) | `store.mark_applied`. Refuses with a readable message if the job already has a live application. |
| `POST /outcome/{application_id}` | `type`, `occurred_at` (defaults to today), `notes` (optional) | `outcomes.record`. Refuses `no_response` and unknown types. |

Validation mirrors the Settings screen's lesson: date and type fields are
accepted as `str` and parsed in the handler, never declared as typed Form
parameters. A typed parameter makes FastAPI reject a malformed value with
a raw 422 *before* the handler runs, bypassing the friendly error path —
a bug this codebase has already shipped and fixed once.

## Screen

The **All Applications** tab gains one column. What it shows depends on
where the job is:

- **No application row, or a draft** — a "Mark applied" control: a date
  input defaulting to today and a submit button. This is the same row that
  currently offers "Open & track" / "Send", so the sequence reads
  naturally: open the posting, apply on the site, mark it.
- **Submitted** — the current effective outcome as a label (or "Awaiting
  response" when no outcome row exists yet), plus a small form to record
  one: a `<select>` over the four manual types, a date defaulting to
  today, an optional notes field.
- **Held or failed** — unchanged; those states already render their own
  message.

The effective outcome per job comes from `outcomes.effective_outcome`,
called per displayed row. That is an N+1 query, matching what
`overview.recent_outcomes` already does, and bounded by the page's row
count — consistent with the existing code rather than a new pattern.

The "Send" button is removed from this tab. It is now dead weight: it
only ever reports that submission is not implemented, and beside a real
"Mark applied" control it invites exactly the confusion this change
exists to remove.

## States and errors

- **Double-marking a job** — caught via `IntegrityError` from the partial
  unique index, reported as "This job already has a live application."
- **`no_response` submitted by hand** — refused with an explanation that
  it is derived after 30 days, not entered.
- **Outcome on a non-submitted application** — refused; the UI does not
  offer the control in that state, so this guards a forged request.
- **A malformed date** — a field error on the same page, nothing written.
- **An outcome recorded before its application's `submitted_at`** —
  allowed. Clock skew and backfilling are real, and refusing would be
  more annoying than the inconsistency is harmful.

## Testing

- `tests/test_store.py` — `mark_applied` promotes an existing draft rather
  than inserting a second row; inserts a fresh row with `answers IS NULL`
  when no draft exists; logs `human_marked_applied`; raises on a job that
  already has a live application.
- `tests/test_outcomes.py` — `record` writes `derived = 0`; refuses
  `no_response`; refuses an unknown type; a later manual outcome
  supersedes an earlier one via `effective_outcome`; a manual outcome
  recorded after a derived `no_response` wins (the spec's day-40 case).
- **The gate-closing test**: mark a job applied, record a `screen`, and
  assert `outcomes.callback_rate(conn) == (1, 1)`. This is the whole point
  of the change — callback data existing at all — and it fails against
  today's code, where the denominator cannot leave zero.
- `tests/test_web.py` — both routes persist; both refuse their bad inputs
  with a 200 and a readable message rather than a 422; the All
  Applications tab renders the mark-applied control for a draft and the
  outcome control for a submitted application.

After implementation, drive it in a browser against a copy of the real
database: mark one of the two `submit`-verdict jobs applied, record a
`screen`, and confirm the Overview page's Responses KPI and callback rate
move off zero.

## Deferred

- **Un-marking a mistakenly-applied job.** Rejected for this pass because
  the partial unique index makes the reverse transition fiddly and a
  misclick is recoverable by other means. Worth revisiting if it happens
  in practice.
- **An Outcomes page.** The nav stub stays dead until there is enough
  outcome history for a dedicated view to beat the inline controls.
- **Automatic capture from email.** v2's response loop.
- **The facts store.** Still 18 entries against the spec's stated 20–40
  prerequisite, and every scoring call logs `only 18 facts; credibility
  scores are probably depressed`. It gates verdict quality, not this
  change, and it needs evidence from the user rather than code.
