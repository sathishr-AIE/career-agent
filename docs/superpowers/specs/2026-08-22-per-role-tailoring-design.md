# Career Agent v2: per-role tailoring, design

Date: 2026-08-22
Status: draft, pending review

Scope of this document: the first slice of v2 named in
`docs/superpowers/specs/2026-08-18-career-agent-v1-design.md`'s phase table —
per-role tailoring and resume rendering. Recruiter-message handling, calendar
events, and the automatic response loop that populates `outcome` rows beyond
`no_response` are also named under v2 there, but they are a different risk
lane (inbound message parsing, calendar APIs) with no dependency on tailoring,
and are deliberately left for a later phase. This document does not schedule
them.

## Why this is v2's gate

The v1 → v2 gate in the governing spec is narrow: *"the gate agrees with your
judgment, and callback data exists."* Both closed with PR #1 (outcome
recording + the live run monitor). The v2 → v3 gate is equally narrow:
*"nothing claimed that you cannot defend."* Everything in this design is
organized around that one sentence — tailoring is a selection-and-phrasing
problem over already-verified facts, never a fact-creation problem.

## Current state

- The `resume` table exists in `db.py` but has never had a row written to it.
- `apply/ats.py` hardcodes `RESUME_VERSION = "base-v1"` for every application,
  regardless of job. Nothing distinguishes one application's resume from
  another's today.
- The `fact` table holds 18 verified claims — above `gate.MIN_FACTS_HARD` (10),
  below `gate.MIN_FACTS_WARN` (20). The same table the gate already scores
  `credibility` against.
- No resume file of any format exists in the repository. Tailoring and
  rendering are greenfield.
- Submission is not implemented (`SUBMISSION_IMPLEMENTED = False` in
  `ats.py`) and is not part of this document's scope. You apply on the real
  site yourself; the artifact this design produces is the file you take there.

## What v2 (this slice) delivers

For any job the gate marked `submit` or `hold`, clicking Apply produces a
real, job-specific DOCX resume — built by selecting and rephrasing entries
from your verified facts store, rendered into your own resume's layout — that
you take to the actual job site. Every version generated is kept and
browsable later, with per-bullet provenance back to the fact it came from.

**Explicitly not this slice**: PDF export (deferred; DOCX is the sole
artifact), an in-app resume upload/edit UI (the master template is a file you
manage directly), automatic re-tailoring on demand (a job is tailored once;
regenerating is not a v2 feature), and anything about recruiter messages,
calendars, or automatic outcome classification.

## The master template

A DOCX you provide, placed at `resume/master.docx` (new directory,
gitignored — this holds identifying personal content, same treatment as
`data/`). Not committed; a setup step, not a build task.

It needs two literal marker paragraphs, which rendering finds by exact text
match:

- `<<SUMMARY>>` — the paragraph whose text becomes the tailored 2–3 sentence
  summary. Its existing run formatting (font, size, weight) is preserved;
  only the text changes.
- `<<PROJECT_BULLET>>` — a single bullet-styled paragraph, cloned once per
  selected fact. Each clone keeps the marker paragraph's style; the original
  marker paragraph is removed after cloning.

Everything else in the template — header, contact info, education, section
headings, layout — is untouched. Tailoring only ever rewrites what's inside
those two markers.

## Data model

One additive column, via the existing `_add_column_if_missing` migration
pattern in `db.py`:

```sql
ALTER TABLE resume ADD COLUMN job_id INTEGER REFERENCES job(id);
```

Nullable — a future master-template row (there isn't one; see below) would
have no job. Every tailored generation inserts a **new** row; retailoring
(not built in v2, but the schema shouldn't block it later) is additive, not
an overwrite, per the v1 spec's own note on this table: "makes 'what exactly
did I send them' answerable months later."

`version` format: `tailored-{job_id}-r{n}`, where `n` is
`count(resume rows for that job_id) + 1`.

No seeded default row for the master template. The existing hardcoded
`ats.RESUME_VERSION = "base-v1"` constant stays as the fallback string for
any application that was never tailored (e.g. a job marked applied without
going through the Apply flow first). Nothing needs to be served for that
version, so it needs no `resume` table row.

Bullet provenance (`fact_ids` per bullet) is stored alongside the generated
content — see Tailoring output, below — so the Resumes page can show which
verified fact backs each line.

## Tailoring

New module `career_agent/tailor.py`, shaped like `gate.py` on purpose: both
solve "how much can this job's application claim," one for scoring, one for
generation.

```python
TAILOR_PROMPT_VERSION = "tailor-v1"  # bump with any prompt edit

async def tailor(job: Job, facts: list[str], ask) -> TailorResult:
    ...
```

Reuses `gate.MIN_FACTS_HARD` as a guard — refuses to tailor off a facts store
too thin to do so honestly, same threshold the gate already enforces at
scoring time.

**Prompt** (mirrors `gate.build_prompt`'s shape): career brief context + the
same `store.facts(conn)` list the gate scores credibility against + the job
description. Instructs the model to select the facts most relevant to this
job and rewrite them as resume bullets, plus a short tailored summary —
**select and rephrase, never invent** — the identical trust model already
governing the gate's `credibility` dimension ("credit nothing not listed").

**Output**, parsed with the same tolerant prose-wrapped-JSON handling as
`gate.parse_verdict`:

```json
{
  "summary": "2-3 sentence tailored summary",
  "bullets": [
    {"text": "rephrased bullet", "fact_ids": [12]}
  ]
}
```

`fact_ids` is what makes drift auditable rather than merely prompt-trusted:
every bullet traces to a specific row in `fact`, and that trace is stored and
shown, not discarded after generation.

**This audit trail is weaker than what `gate.score` gets**, and that gap is
worth naming plainly: `gate.score` is checked against a golden set
(`tests/golden/listings.json`) on every prompt change. `tailor()` gets no
equivalent — `fact_ids` lets you notice drift after a resume already went
out, not before. Two cheap, deterministic checks close part of that gap
without building a golden set for tailoring in this slice:

- **Every `fact_id` a bullet cites must exist in the current `fact` table.**
  Checked right after parsing, same place `gate.parse_verdict`'s JSON
  validation happens. A bullet citing a nonexistent fact id is treated the
  same way invalid JSON is — one retry with an explicit correction appended
  to the prompt, then a hard failure surfaced to the user rather than a
  silently-shipped unverifiable claim.
- **At least one bullet is required.** Ten facts is enough to clear
  `gate.MIN_FACTS_HARD`, but the model may still judge few of them relevant
  to a specific job, and an empty `bullets` list would otherwise render a
  resume with the `<<PROJECT_BULLET>>` marker simply deleted and nothing put
  in its place. Mirrors the input-side facts-sufficiency guard with an
  equivalent output-side one: zero bullets is refused, not shipped.

A full golden-set-style fidelity check for tailoring (e.g. a human-labeled
set of job/fact-selection pairs, checked on prompt changes the way the gate's
golden set works) is real future work, not built here — flagged in Deferred.

## Rendering

Deterministic Python, not the LLM — `render_docx(template_path, tailor_result,
out_path)` in `tailor.py`, using `python-docx` (new dependency; nothing in
`pyproject.toml` does document generation today).

1. Open `template_path` with `docx.Document`.
2. Find the `<<SUMMARY>>` paragraph; clear its runs and set the summary text
   on a new run cloned from the original run's formatting.
3. Find the `<<PROJECT_BULLET>>` paragraph; for each bullet, deep-copy that
   paragraph's underlying XML element, set its text (formatting carried over
   by the copy), and insert it immediately before the marker via
   `addprevious`. Remove the original marker paragraph once all bullets are
   inserted.
4. Save to `out_path` (convention: `resume/generated/{version}.docx`).

This is one implementation risk worth naming: python-docx's clone-and-restyle
approach is well-documented, but exact behavior against your specific
template's paragraph styles is the part most likely to need a short
iteration loop once tried for real.

**A second risk, about failure rather than fidelity**: the `resume` row is
inserted only *after* step 4 saves successfully — never before, and never
in the same statement as a render that might still throw. There is no
"Retailor" action in v2 (by design), so a `resume` row pointing at a
broken or missing file would hand that same broken version to every future
request for the job with no recovery path — `resume_version_for` doesn't
know a version's file is bad, it just returns the latest one. Ordering the
insert strictly after a confirmed-written file means a failed render leaves
no row at all, and the next Apply click naturally retries tailoring from
scratch (the "does a row already exist" check correctly says no).

## Trigger and integration

**Trigger: on Apply click**, not during the pipeline run. Tailoring costs one
model call; running it automatically for every `submit`/`hold` job during
`run_once` would spend that call on jobs you never open. Triggering at Apply
keeps the pipeline's cost and duration exactly as v1 left them.

If a `resume` row already exists for the job's `id`, its most recent version
is reused rather than regenerating — no "Retailor" action in v2.

**Concurrent Apply clicks on the same job** (a double-fire is not
hypothetical here — this app has already shipped fixes for exactly this
class of htmx double-submit race). A plain read-then-decide ("does a row
exist? no? tailor.") lets two near-simultaneous requests both tailor,
both render, and both try to insert — and since `resume.version` is
`UNIQUE`, the loser fails on `sqlite3.IntegrityError` instead of gracefully
reusing the winner's row. Handle it the same way `store.upsert_jobs`
already handles the equivalent race on `job.fingerprint`: attempt the
insert, catch `sqlite3.IntegrityError`, and re-read the now-existing row
for that job instead of surfacing the error.

**Shared lookup, not per-caller logic.** New `store.resume_version_for(conn,
job_id) -> str`: returns the latest `resume.version` for that job if one
exists, else `ats.RESUME_VERSION`. `ats.py` cannot import `store` (the
dependency already runs the other way — `store.py` imports `ats`), so the
lookup happens in each caller, not inside `ats.submit`:

- `ats.submit()` gains `resume_version: str | None = None`, defaulting to the
  existing constant so any caller that doesn't pass one keeps today's
  behavior.
- `store.mark_applied()` swaps its hardcoded `ats.RESUME_VERSION` for
  `resume_version_for(conn, job_id)`.

**Web route** (`app.py`'s `_do_apply`): before calling `ats_apply.submit`,
call `verify_auth()` (the same guard `run_once` uses — fail loudly rather
than silently falling back), then tailor-if-needed, render, insert the
`resume` row, and pass the resulting version into
`submit(..., resume_version=version)`.

**This request is not cheap today, and this design does not make it
cheap — it adds to an existing cost.** `_do_apply` already calls
`ats_apply.submit(conn, job_id, dry_run=True)` unconditionally, and
`dry_run=True` only skips the `SUBMISSION_IMPLEMENTED` guard (`ats.py`'s
`submit`, the `if not dry_run and ...` check) — it still falls through to
`filler = filler or _default_filler` and awaits a real, visible Chromium
launch that navigates to the job URL. That happens on every Apply click in
v1, today, independent of anything in this document. The tailoring LLM call
is genuinely one call, awaited directly with no background thread — that
part of the original reasoning holds — but it is stacked onto an
already-slow handler, not the primary cost in it. Framing it as "the"
expensive step would have been wrong; call it an added few seconds on top of
a browser launch the handler already pays for.

**Explicit ordering, because it matters for failure isolation**: tailor,
render, and insert the `resume` row *before* calling `ats_apply.submit`.
That means if the subsequent browser step hits a captcha or fails outright,
the resume row and file already exist and are unaffected — resume
generation succeeds or fails on its own terms, never rolled back by what the
stub browser does afterward. (Moving tailoring off the request entirely,
polling the way the live run monitor does for the multi-minute pipeline run,
was considered and rejected: that idiom exists there because *minutes* of
pipeline work would otherwise block a page render; a few extra seconds on a
handler that already blocks on a browser launch synchronously does not meet
that bar. Revisit if tailoring latency turns out worse than expected once
built.)

## Dashboard

**Applications page** (`applications.html`'s `action_cell` macro): a download
link next to any tracked application whose `resume_version` starts with
`tailored-`.

**New Resumes page** (`/resumes`, new nav entry in `base.html` alongside the
existing stub links): shows the master template (filename, last-modified,
tagged as primary — read-only, no in-app upload for v2) and every tailored
version ever generated, grouped by job (company, title, version, created-at,
download link, and each bullet's fact provenance). This is the full browsable
history; the Applications-page link is the quick path from a specific job's
row.

Both link to the same new route: `GET /resume/{version}`, which resolves the
version to its `resume.path` and serves the file via FastAPI's
`FileResponse`.

## Testing

- `tailor.py`'s prompt-build/parse round-trip, table-driven, mirroring
  `test_gate.py`'s shape.
- `render_docx` against a small fixture template checked into `tests/`:
  asserts both markers are gone from the output and the bullet count matches
  the number of selected facts.
- `resume_version_for` falls back to `ats.RESUME_VERSION` when no tailored
  row exists for a job, and returns the correct latest row when one does.
- `tailor()` raises the same `gate.InsufficientFacts`-shaped guard when the
  facts store is too thin, at the same threshold the gate already enforces.
- `tailor()` retries once and then fails clearly on a bullet citing a
  `fact_id` absent from the current `fact` table, and refuses a zero-bullet
  result.
- **A route-level test of `_do_apply`'s actual new sequence**: tailor →
  render → insert `resume` row → `submit(..., resume_version=...)`, with the
  browser filler swapped for a test double the way `test_web.py`'s existing
  apply tests already do. This project's own recurring bug class — the
  UTC/local clock mismatch, the typed-`Form`-field 422 trap, htmx state
  destroyed by a poll swap — is integration seams, not unit logic. This is
  the newest seam (tailoring landing in front of an existing, already
  side-effecting handler) and unit tests on `tailor.py` and `render_docx` in
  isolation would not have caught the ordering issue this design doc itself
  got wrong on the first pass.
- A concurrent-Apply-clicks test asserting two near-simultaneous tailor
  attempts on the same job converge on one `resume` row, not an
  `IntegrityError`.

## Deferred

- A golden-set-style fidelity check for tailoring, equivalent to
  `gate.score`'s `tests/golden/listings.json` — human-labeled job/fact
  selections checked on every prompt change. v2 ships with cheaper
  point-checks instead (fact-id existence, non-empty bullets); this is the
  next layer up if drift shows up in practice.
- PDF rendering from the tailored DOCX.
- In-app upload/edit of the master template.
- A "Retailor" action to regenerate a job's resume on demand.
- Recruiter message handling, calendar events, and the automatic
  outcome-classification response loop (named under v2 in the v1 spec, but a
  separate risk lane — inbound parsing, calendar APIs — with no dependency on
  tailoring).
- Auto-submission: still v3, still gated on tailoring plus outcome evidence.
