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

This is the one implementation risk worth naming: python-docx's
clone-and-restyle approach is well-documented, but exact behavior against
your specific template's paragraph styles is the part most likely to need a
short iteration loop once tried for real.

## Trigger and integration

**Trigger: on Apply click**, not during the pipeline run. Tailoring costs one
model call; running it automatically for every `submit`/`hold` job during
`run_once` would spend that call on jobs you never open. Triggering at Apply
keeps the pipeline's cost and duration exactly as v1 left them.

If a `resume` row already exists for the job's `id`, its most recent version
is reused rather than regenerating — no "Retailor" action in v2.

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
`submit(..., resume_version=version)`. This is the single on-demand LLM call
in the request path; it's awaited directly in the async route rather than
handed to a background thread — one job, one call, unlike the multi-minute
pipeline run that already needs `asyncio.to_thread`.

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

## Deferred

- PDF rendering from the tailored DOCX.
- In-app upload/edit of the master template.
- A "Retailor" action to regenerate a job's resume on demand.
- Recruiter message handling, calendar events, and the automatic
  outcome-classification response loop (named under v2 in the v1 spec, but a
  separate risk lane — inbound parsing, calendar APIs — with no dependency on
  tailoring).
- Auto-submission: still v3, still gated on tailoring plus outcome evidence.
