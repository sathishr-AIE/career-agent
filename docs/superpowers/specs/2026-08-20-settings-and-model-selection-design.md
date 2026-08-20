# Settings screen and scoring-model selection: design

Date: 2026-08-20
Revision: 1
Status: open for revision.

Scope: make the agent's configuration editable from the dashboard rather
than only from files and command-line flags, make the scoring model an
explicit choice (Sonnet or Haiku) instead of an unstated CLI default, and
fix the scoring budget so that choice actually gets exercised.

## Why

Three problems, discovered together while trying to get jobs scored:

1. **`--max-score` doesn't do what it says.** It is documented as "cap
   scored jobs per run", but is implemented as a `LIMIT` on rows *pulled*
   (`store.unscored_jobs`, `run.py:76`). Hard-filtered jobs consume that
   budget before any model call happens. Measured against the live
   database: of 1336 unassessed jobs, 1217 fail the deterministic hard
   filter (867 on location alone — US roles from the Anthropic and Stripe
   Greenhouse boards) and only 119 would reach the model. A run of 25
   therefore samples mostly jobs that never needed a model call and scores
   close to zero. The observed run logged `hard-filtered 25, scored 0`.
2. **The scoring model is unstated.** `run._ask` builds
   `ClaudeAgentOptions(tools=None)` with no `model`, so scoring silently
   uses whatever the Claude CLI defaults to. `assessment.model` records
   the placeholder string `"claude-agent-sdk"` (`run.py:16`), so the
   stored record cannot answer "which model produced this verdict" — the
   exact question calibration depends on.
3. **Nothing is configurable without editing files.** Every knob lives in
   `career_brief.toml` or a CLI flag. The dashboard's nav already shows a
   `Settings` link, but it is a dead `href="#"` stub
   (`base.html:106`).

## Non-goals

- No new dependencies beyond `tomlkit` (justified below).
- Not building the other nav stubs (Discoveries, Shortlisted, Pipeline,
  Calendar, Outcomes). They stay `href="#"`.
- Not adding auth. Single-user localhost, per the v1 spec's non-goals.
- Not changing the gate prompt, the five dimensions, their weights, or the
  verdict rules. Only *which model* runs the existing prompt changes.
- Not re-scoring existing assessments when settings change (see the
  decision below).
- Not making the hard filter's rules configurable beyond the brief fields
  it already reads. Seniority filtering, in particular, is out of scope —
  see Deferred.

## Decisions

These were settled before writing this spec; recorded here with their
reasoning so a later reader doesn't relitigate them.

### The career brief stays a file; only operational settings go in the DB

The v1 design is explicit: *"`career_brief.toml` is the single source of
truth... There is no `career_brief` table. Earlier revisions had both,
which is one definition too many. The file wins because it is version
controlled and diffable"*
(`docs/superpowers/specs/2026-08-18-career-agent-v1-design.md`, "The
career brief is a file, not a table"), and
`tests/test_db.py::test_no_career_brief_table` enforces it.

That decision holds. The split is by *kind of thing*, not by convenience:

| Setting | Lives in | Why |
| --- | --- | --- |
| Target titles, title families, search locations, accepted locations, `remote_ok`, salary floor, work authorization, `daily_cap`, `gate_threshold`, `staleness_days`, excluded companies, non-negotiables | `career_brief.toml` | This is the *career definition*. Its history is worth diffing — "you raised the salary floor three weeks before response rate dropped" is a conclusion only a diff supports. |
| Scoring model, jobs scored per run | `setting` table (SQLite) | These are *machine* settings: how the agent runs, not what you want from your career. They have no interpretive value in a git history, and they need to be changeable from a web form without touching the repo. |

The Settings screen presents both as one page. Two backing stores behind
one form is deliberate and is stated in the UI (each section names where
it saves), so the split is discoverable rather than surprising.

### Sonnet is the default scoring model, Haiku is selectable

`claude-sonnet-5` by default; `claude-haiku-4-5-20251001` available.
Scoring is nuanced judgement across five weighted dimensions against a
facts store, with a `credibility` floor that hard-fails a job — the
failure mode of a too-small model is silent, systematically wrong
verdicts, not an error. Sonnet is the safer default. Haiku stays
selectable for clearing a large backlog quickly, which is a real need
here (119 jobs pending).

Both are run through the subscription token (`CLAUDE_CODE_OAUTH_TOKEN`);
`run.verify_auth` already refuses to start if `ANTHROPIC_API_KEY` is set,
so model choice affects rate limits and wall-clock time, not per-token
billing.

### Changing settings does not re-score existing assessments

An assessment records the settings that produced it, and stays. Two
reasons: a single threshold tweak should not silently spend 100+ model
calls, and mixed-model history is *informative* — it is how you find out
whether Haiku's verdicts track Sonnet's.

What makes this honest is that `assessment.model` starts holding the real
model id instead of the `"claude-agent-sdk"` placeholder, so every stored
verdict is attributable. The existing re-score mechanism is unchanged and
still the right tool for a deliberate sweep: bump `gate.PROMPT_VERSION`
and `store.unscored_jobs` re-surfaces everything.

A manual "re-score all" button was considered and deferred — bumping
`PROMPT_VERSION` already does it, and a one-click "spend your whole quota"
button is a poor first version of that idea.

## Data model additions

One new table. `career_brief` is still *not* a table, and
`test_no_career_brief_table` must keep passing.

```sql
CREATE TABLE IF NOT EXISTS setting (
    id                INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    scoring_model     TEXT NOT NULL DEFAULT 'claude-sonnet-5',
    max_score_per_run INTEGER NOT NULL DEFAULT 25
                        CHECK (max_score_per_run >= 0),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Seeded in `db.init_schema` with `INSERT OR IGNORE INTO setting (id) VALUES
(1)`, matching how `run_state` is seeded — idempotent, and safe to re-run
on every request (which `web/app.py:_conn` does).

**`scoring_model` deliberately has no CHECK constraint.** Allowed values
are validated in Python against a module-level tuple, so adding or
retiring a model is a constant edit rather than a schema migration.
`max_score_per_run` keeps its CHECK because "not negative" is a
permanent truth about the column, not a list that will churn.

`max_score_per_run = 0` remains meaningful and keeps its existing
semantics: discovery and the hard filter run, scoring does not.

## The scoring budget fix

Without this, `max_score_per_run` is not a setting worth exposing — it
would still be a cap on rows examined rather than on model calls.

`store.unscored_jobs(conn, prompt_version, limit=None)` — `limit` becomes
optional and the `LIMIT` clause is only appended when one is given. All
five existing call sites in `tests/test_store.py` pass an explicit limit
and are unaffected.

`run.run_once` then hard-filters the whole unassessed pool and rations
only the model calls:

```python
for row in store.unscored_jobs(conn, gate.PROMPT_VERSION):
    job = _row_to_job(row)

    reason = hardfilter.check(job, brief)
    if reason:
        store.save_hard_skip(conn, row["id"], reason)
        skipped += 1
        continue

    if scored >= max_score:
        continue          # budget spent; this survivor carries to the next run

    verdict = await gate.score(job, brief, store.facts(conn), ask)
    store.save_assessment(conn, row["id"], verdict, model, gate.PROMPT_VERSION)
    scored += 1
```

`continue` rather than `break` is the substance of the fix. Hard filtering
is deterministic, local, and free, and its results are *persisted* — and
`store.unscored_jobs` already excludes rows with `stage = 'hard'`. So one
sweep permanently retires the 1217 jobs that can never pass, and every
later run finds real candidates immediately instead of re-walking the same
US listings forever. Breaking early would leave the pool dirty and
reproduce the bug on the next run.

This also makes an existing promise true: `--max-score 0` logs *"only
discovery and the hard filter will run"*, but today `LIMIT 0` returns no
rows, so the hard filter does not run either.

**Precedence.** The `setting` row is the source of truth.
`--max-score`'s argparse default becomes `None`; when it is passed
explicitly it overrides the stored value *for that run only* and is not
persisted. This keeps the flag useful for one-off runs without creating a
second competing definition.

## Model selection plumbing

```python
SCORING_MODELS = ("claude-sonnet-5", "claude-haiku-4-5-20251001")

async def _ask(prompt: str, model: str | None = None) -> str:
    # ... existing body unchanged; the only edit is the options argument ...
    async for message in query(prompt=prompt,
                               options=ClaudeAgentOptions(tools=None,
                                                          model=model)):
```

`run_once` reads the `setting` row from the connection it already holds,
binds the model with `functools.partial(_ask, model=scoring_model)`, and
passes that to `gate.score` — whose signature
(`Callable[[str], Awaitable[str]]`) is unchanged. `gate.py` does not
change at all.

The `MODEL_ID = "claude-agent-sdk"` constant is deleted, and the
configured model id is passed to `store.save_assessment` in its place.

Because both the brief and the settings are read inside `run_once`, a
settings change takes effect on the *next* run with no server restart. A
change made mid-run cannot tear state: the values are read once at the
top of the run.

`fallback_model` is left unset. It is a real SDK option and an obvious
future knob, but a silent fallback to a different model would undermine
the attributability that motivates recording `assessment.model` at all.

## Writing the brief back to TOML

`career_brief.toml` carries explanatory comments that are part of its
value:

```toml
# What we ASK each source for. Adding a city multiplies daily Actor runs
# by the number of target titles.
search_locations = ["Chennai"]
```

A naive `dump` destroys those. `tomlkit` is added as a dependency
specifically because round-trip TOML that preserves comments, key order,
and formatting is not something to hand-roll — it is the one rung on the
"can a few lines do it?" ladder where the answer is genuinely no.

New in `config.py`:

```python
def save_brief(path: Path, brief: CareerBrief) -> None:
    """Round-trip the file so comments and key order survive."""
```

It parses the existing file with `tomlkit`, assigns each field from the
validated `CareerBrief`, and writes it back. If the file does not exist,
it writes a fresh document — no comments to preserve in that case.

**Validation reuses the existing model.** The POST handler builds a dict
from the form and constructs `CareerBrief(**data)`. Pydantic already
encodes every rule worth enforcing — `target_titles` and
`search_locations` need at least one entry, `daily_cap >= 1`,
`0 <= gate_threshold <= 100`, `staleness_days >= 1`. A `ValidationError`
re-renders the form with the messages attached. No second validation
layer is written.

**Write ordering.** Validate everything (brief *and* settings) before
writing anything; then write the TOML; then the `setting` row. A
validation failure must leave both stores untouched, and a TOML write
failure must not leave the DB describing a state the file does not.

## Settings screen

Route `GET /settings` and `POST /settings` in `web/app.py`, rendering
`templates/settings.html`, which extends the existing `base.html` shell
with `active_nav = "settings"`. The dead `href="#"` stub at
`base.html:106` becomes `href="/settings"` and picks up the existing
`.nav a.active` highlight.

Two sections, each stating where it saves:

**Career Brief** — *saves to `career_brief.toml`*
- Target titles, title families, search locations, accepted locations,
  work authorization, excluded companies, non-negotiables — list fields,
  edited as comma-separated text inputs. Comma-separated (not one input
  per item) keeps the form static HTML with no JS array widgets; entries
  are split on commas and stripped, and empty entries dropped.
- Remote OK — checkbox.
- Salary floor (INR), daily cap, gate threshold, staleness days — number
  inputs.

Only `target_titles` and `search_locations` are required by `CareerBrief`
(both `min_length=1`); every other field is optional with a default. So an
emptied list field submits as an empty list, and an emptied salary floor
submits as `None` (not `0`, which would mean "reject nothing" and is a
different statement). Emptying either required field is a validation
error, surfaced on that field rather than as a generic failure.

**Agent Settings** — *saves to the database*
- Scoring model — a `<select>` over `SCORING_MODELS`, labelled with what
  the choice means ("Sonnet — better judgement" / "Haiku — faster, lighter
  on rate limits") rather than bare model ids.
- Jobs scored per run — number input, `min=0`, with the note that 0 runs
  discovery and the hard filter only.

A single Save button posts the whole form. On success the page re-renders
with a confirmation banner and the newly-saved values re-read from their
stores (not echoed from the request), so what is displayed is what was
actually persisted. On failure it re-renders with the field errors and the
user's submitted values preserved, so nothing is retyped.

Styling reuses the existing `.card`, `.panel`, `.btn`, `.kv`, `.denied`,
and `.done` classes already in `base.html`. Any genuinely new class
(form rows, field errors) is added to `base.html`'s single `<style>`
block, consistent with how the rest of this app is styled.

## States and error handling

- **Settings never seen** — the seeded row means `GET /settings` always
  has values to show; there is no "unconfigured" state.
- **Invalid input** — form re-renders with per-field messages, both
  stores untouched.
- **`career_brief.toml` missing or unreadable** — `GET /settings` shows an
  error banner and the brief section is disabled rather than silently
  showing defaults that would overwrite the file on save.
- **Settings changed mid-run** — permitted, takes effect next run. The
  screen says so rather than blocking, since blocking would be a lie the
  moment the run finishes.
- **Model rejected by the SDK** (bad id, model retired) — surfaces the
  same way any scoring failure does today: the run lands in
  `run_state(kind='pipeline').status = 'error'` with `last_error`, shown
  on the Overview page.

## Testing

- `tests/test_config.py` — `save_brief` round-trips a file with comments
  and preserves them; a modified value is actually changed; an invalid
  brief is rejected by pydantic before any write happens.
- `tests/test_db.py` — the `setting` row is seeded, seeding is idempotent
  across repeated `init_schema` calls, and `test_no_career_brief_table`
  still passes.
- `tests/test_run.py` — **the budget fix**: a pool where most jobs fail
  the hard filter proves `max_score=N` yields N *scored*, not N examined
  (this test fails against today's code); hard skips are persisted for the
  whole pool even when the scoring budget runs out; the configured model
  reaches both `_ask` and `store.save_assessment`; an explicit
  `--max-score` overrides the stored setting while an omitted one uses it.
- `tests/test_web.py` — `GET /settings` renders current values from both
  stores; a valid POST persists to both; an invalid POST re-renders with
  an error and leaves `career_brief.toml` byte-identical; the nav link
  points at `/settings` and highlights.

After implementation, drive it in a browser: change the model to Haiku and
the per-run count, save, confirm the TOML diff shows only the intended
change with comments intact, then trigger Run Now and confirm the run
scores that many jobs and records the chosen model id in
`assessment.model`.

## Deferred

- **Seniority filtering.** All three jobs scored so far were skipped on
  `eligibility_soft` (30, 25, 10 — the gate skips any dimension below 40)
  because they were Senior/Expert/Principal roles against a 1–3 year
  candidate. Rejecting those in the hard filter would save model calls,
  but it risks discarding reachable "Senior" titles, and the right
  threshold is unknown until there is more scored data. Revisit once the
  119 pending jobs have verdicts.
- **The facts store is thin.** 18 facts, against `gate.MIN_FACTS_WARN =
  20`, which warns that "credibility scores are probably depressed" —
  credibility carries 30% weight and a hard floor of 60, and one scored
  job came in at exactly 55. This needs the user to supply evidence, not
  code, so it is not part of this change.
- **`fallback_model`**, and a manual re-score sweep — both discussed
  above.
