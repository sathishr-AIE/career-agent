# Career Agent v3: real Greenhouse auto-submission — design

Date: 2026-08-23
Status: draft, pending review.

Scope of this document: v3 as named in
`docs/superpowers/specs/2026-08-18-career-agent-v1-design.md`'s phase table —
"Auto-submission within limits." It assumes v1 and v2 as already shipped and
merged (`main` at the time of writing).

## Why this document exists now, ahead of its stated gate

The v1 spec gates v3 on *"Tailoring proven, and outcomes show tailored
applications convert."* Tailoring (v2) merged the day before this document was
written. No real-world outcome data exists yet showing tailored applications
convert — that evidence takes weeks of real usage to accumulate, and cannot be
produced by writing code.

The decision made here, deliberately: **build the capability now, decide
separately and later whether to trust it.** The "Rollout" section below is
the mechanism that keeps those two decisions apart. This document does not
claim the v1 gate is met; it routes around the fact that the gate is
empirical and the code isn't.

## What already exists

More of v3 shipped already than the v1 spec's phase table implies, built
alongside v1's outcome-recording and live-run-monitor work
(`worker.py`, `app.py`):

- A `run_state` table (`kind = 'apply'`) with `status` (`running` / `idle` /
  `paused` / `error` / `stopped`) and `mode` (`manual` / `auto`), driven from
  the Applications page's mode toggle.
- `worker.apply_tick` / `worker.apply_worker_loop`: pick the next queued job,
  run the existing guardrails (`worker.guard` — verdict, daily cap, pause
  flag), draft it, and in `auto` mode immediately attempt a real send. In
  `manual` mode it stops after drafting, parked for a human "Send" click.
- `ats.sweep_stale_in_flight`: crash recovery, moves a stuck `in_flight` row
  to `held_unknown` after 15 minutes.
- `ats.submit`'s retry/failure ladder (`failed` → `failed_permanent` after 3
  attempts) and captcha handling (`CaptchaEncountered`, screenshot, hold).

What's explicitly stubbed: `ats.py`'s `SUBMISSION_IMPLEMENTED = False` is a
deliberate kill-switch, and `_default_filler` opens the job page and returns a
hardcoded answers dict without touching the form. Every real send is
categorically refused today, regardless of mode. `qa_bank` exists as a table
(`db.py`) with no reads or writes anywhere in the codebase.

**v3 is the real filler and `qa_bank`'s first real behavior — not the
orchestration around them, which is done.**

## What v3 delivers

A real, tested Greenhouse form-filler that replaces `_default_filler`, wired
through the orchestration above, plus `qa_bank`'s first real use. Lever and
Ashby stay categorically refused — no boards are configured for either
provider today (`ats_boards.toml` has Greenhouse entries only, and
`sources/ats.py` has no fetcher for either). `SUBMISSION_IMPLEMENTED` ships
`False`; flipping it to `True` is a manual, later decision (see "Rollout"
below).

## Candidate profile

Greenhouse's standard fields (name, email, phone, resume upload, and
sometimes LinkedIn/portfolio URLs) need candidate identity that exists
nowhere in the codebase today. `career_brief.toml` is the wrong place for
it: that file is deliberately version-controlled (the v1 spec's rationale is
a diffable history of how the search itself changes), and name/email/phone
are PII, not a search preference. This follows the project's existing
precedent for anything sensitive — `.env` (the OAuth token) is gitignored
with a tracked `.env.example` template.

A new file, `candidate_profile.toml`, gitignored the same way:

```
# --- Secrets & personal data ---
candidate_profile.toml
!candidate_profile.toml.example
```

(alongside the existing `.env`/`.env.example` pair in the same `.gitignore`
section). A tracked `candidate_profile.toml.example` documents the shape:

```toml
candidate_name = "Jane Doe"
candidate_email = "jane@example.com"
candidate_phone = "+91-90000-00000"
linkedin_url = ""
portfolio_url = ""
```

`config.py` gains a parallel model and loader, mirroring `CareerBrief`/
`load_brief`/`save_brief` exactly:

```python
class CandidateProfile(BaseModel):
    candidate_name: str
    candidate_email: str
    candidate_phone: str
    linkedin_url: str | None = None
    portfolio_url: str | None = None

def load_candidate_profile(path: Path) -> CandidateProfile
def save_candidate_profile(path: Path, profile: CandidateProfile) -> None
```

A fresh checkout has no `candidate_profile.toml` — `load_candidate_profile`
raises clearly (naming the missing file and pointing at the `.example`),
matching the project's existing "fail loudly, never fall back silently"
posture for missing auth. This only matters on the real-send path; nothing
in v1 or v2 needs this file, so its absence is silent everywhere else.

The Settings page form gains a section for these fields, saved through
`save_candidate_profile` — same round-trip pattern as the rest of the
Settings form, just backed by a different, gitignored file.

These fields are filled directly on the form — no `qa_bank` lookup, since
they never vary per job. `compute_answers`'s signature (below) takes the
loaded `CandidateProfile` alongside the `CareerBrief` it already needs.

## qa_bank: reactive population, no management page

`qa_bank`'s columns already exist: `question_normalized` (UNIQUE), `answer`,
`is_volatile`, `last_confirmed_at`. `store.py` gains its first functions
against it:

```python
def qa_normalize(question: str) -> str:
    # lowercase, collapse whitespace, strip trailing punctuation -- just
    # enough that "Notice period?" and "notice period" collide.

def qa_lookup(conn, question: str) -> sqlite3.Row | None
def qa_upsert(conn, question: str, answer: str, is_volatile: bool) -> None
    # upsert sets last_confirmed_at = datetime('now') unconditionally --
    # confirming an existing answer is itself a reconfirmation.
```

**Field resolution order**, per custom question on the form:

1. **Candidate profile fields** — filled directly (see above), no lookup.
2. **`qa_bank` hit, not volatile** — fill silently.
3. **`qa_bank` hit, volatile, `last_confirmed_at` within 30 days** — fill
   silently.
4. **`qa_bank` hit, volatile, older than 30 days** — treated as case 5: an
   answer that might now be wrong is never auto-filled, matching the v1
   spec's "never auto-fill a stale value" rule for volatile answers.
5. **No usable `qa_bank` entry, and the facts store can't answer it either**
   — do not guess. `compute_answers` raises `NeedsAnswer(question)`, and the
   application holds using the exact same parking mechanism `apply_tick`
   already uses for manual mode's "drafted, awaiting your review" pause:
   `run_state.current_job_id` is left set rather than cleared, so
   `apply_worker_loop`'s outer check (`current_job_id is None`) keeps the
   worker from re-picking this same job on the next tick. This matters in
   both modes — `compute_answers` can raise mid-draft whether the tick was
   started in `auto` or `manual`.

   An `event` row is written (`type = 'needs_answer'`, `payload` = the
   question text, keyed to the job), and the dashboard's existing "current
   job" status card — today used to show a drafted application awaiting a
   Send click — gains a second display mode for this case: "Answer needed:
   `<question>`", a text box, and an "this may change later" checkbox.
   Submitting it calls `qa_upsert`, clears `current_job_id`, and lets the
   worker re-pick the job on its next tick — this time with the question
   answered, so `compute_answers` reaches the qa_bank hit instead of raising
   again.

   Without the parking step, the naive version of this (log and move on)
   would leave the job with no application row, meaning nothing marks it as
   handled — `next_candidate()` would return the exact same job on the very
   next tick, a fraction of a second later, and hit the same missing
   question again. Parking is what turns "no answer exists" into a stop
   instead of a spin.

A question the facts store can answer (e.g. "years of ML experience") is
resolved from `fact` rows the same way `tailor()` already does, ahead of the
`qa_bank` fallback — `qa_bank` is for practical/logistics questions
(notice period, work authorization, current compensation, "why this
company"), not defensible experience claims.

**30-day window**, chosen to match the existing `staleness_days`/
`no_response`-window precedent already established in `career_brief.toml`
and the v1 spec.

No standalone `qa_bank` browse/edit page ships in this round — the reactive
hold-and-answer flow above is the entire UI. A management page is listed
under Deferred.

## The Greenhouse filler

Replaces `ats._default_filler`. Fixes a correctness gap along the way: today,
`auto` mode's `apply_tick` calls `ats.submit` twice per job — once
(`dry_run=True`) to build the draft, once (`dry_run=False`) to send for
real — and each call computes its own answers independently. Nothing
guarantees the second call's answers match the first, so what a human
reviewed in manual mode is not provably what gets sent, and even in `auto`
mode the job page is visited and re-decided twice for no benefit.

**New filler contract**, replacing the single `_default_filler(url) -> dict`:

```python
async def compute_answers(page, job, brief, profile, conn) -> dict:
    # Visit the form, resolve every field via the order above, WITHOUT
    # submitting. profile is the loaded CandidateProfile (name/email/phone/
    # links). Returns the answers dict used for the draft. Raises
    # NeedsAnswer(question) for the hold case (qa_bank rule 5).

async def fill_and_submit(page, url: str, answers: dict, resume_path: Path) -> None:
    # Visit the form again, type the given answers into their matching
    # fields, upload the résumé at resume_path, and click Submit. Makes no
    # decisions -- everything it types was already decided by compute_answers.
```

`ats.submit()` changes to match:

- **Draft** (`dry_run=True`, unchanged trigger): calls `compute_answers`,
  stores the result as the `draft` row's `answers` column, exactly as today.
- **Real send** (`dry_run=False`): if a `draft` row already exists for this
  job, reuse *its* stored `answers` — never recompute. `fill_and_submit` is
  called with those answers and the résumé path for `resume_version`. Only
  if no draft row exists yet (not reachable through `apply_tick`'s normal
  path, but `submit()` stays safe to call directly) does it fall back to
  calling `compute_answers` fresh.

**Field matching.** Greenhouse's standard fields (name, email, phone, resume
upload, LinkedIn, portfolio) are matched by Greenhouse's stable HTML `id`
attributes, which are consistent across every Greenhouse-hosted posting.
Custom questions are matched by their visible label text, normalized with
`qa_normalize` — the same normalization `qa_bank` keys on, so a label and a
stored question collide correctly.

**Provider routing — the "some other website" case.** v3 only ever attempts
real automation on jobs whose `job.source == 'ats'`. Every job discovered
through that lane was fetched directly from Greenhouse's own board API
(`sources/ats.py`'s `fetch_greenhouse`, the only ATS-lane fetcher that
exists), so its URL is always a page Greenhouse itself serves and controls
the form on — even when a company wraps it under their own domain
(`jobs.company.com` instead of `boards.greenhouse.io/company`), since that's
Greenhouse's embed serving the same DOM structure under a different address.

Jobs discovered through LinkedIn or Naukri can lead anywhere on "Apply" —
a company's own custom site, Workday, anything. There is no way to build a
general filler for "anywhere," so v3 doesn't try: `job.source != 'ats'`
routes to the same categorical refusal that exists today
(`ats.py`'s `"Submission is not implemented yet..."` message), unchanged.

**Assumption to revisit later:** `job.source == 'ats'` is treated as
equivalent to "this is a Greenhouse job" because Greenhouse is the only
ATS-lane fetcher implemented. The day a Lever or Ashby fetcher is added, this
equivalence breaks, and a `job.provider` column becomes necessary to
distinguish them — not needed now, and not built now.

**Captcha handling** is unchanged: `compute_answers` and `fill_and_submit`
reuse the existing recaptcha/hCaptcha detection and `CaptchaEncountered`
flow already in `_default_filler`.

## Rollout: build now, trust it later

`SUBMISSION_IMPLEMENTED` ships `False`. The real filler exists and is fully
tested, but `auto` mode's real-send step still hits the same categorical
refusal it does today until this flag is flipped by hand — no new config or
database state needed for that gate; it is the existing flag, doing the job
it already does.

While it's `False`, the feature is still fully exercisable: manual mode's
**draft** step (`compute_answers`, no submission) already runs through the
real filler and shows exactly what it would send, including field-matching,
résumé upload, and the `qa_bank` hold flow, against real Greenhouse postings,
with zero real applications going out. Only the final `fill_and_submit` +
click-Submit step is gated by the flag.

**Dashboard callout**, matching the existing pattern (the pipeline page's
"No automatic applications" note, the Applications page's "no scheduled run"
note): once `SUBMISSION_IMPLEMENTED` is `True`, the Applications page shows a
note beside the Auto/Manual toggle: *"N tailored applications sent so far, M
outcomes recorded — the original plan for this feature said to wait for real
evidence tailored applications convert before trusting this."* This is
informational, not a block. The decision to build ahead of the evidence gate
was made explicitly (see the top of this document); the software's job from
here is to keep that evidence visible when auto mode is about to use it, not
to enforce a threshold nobody asked it to enforce.

## Data model

Deliberately small — no schema migration beyond what already exists:

| Change | Where | Notes |
|---|---|---|
| New `CandidateProfile` model + `candidate_profile.toml` (gitignored) | `config.py` | Config only, no DB table; kept out of `career_brief.toml` because it's PII, not a search preference |
| First real reads/writes | `qa_bank` | Table and columns already exist (v1 schema) |
| New `event.type` value: `needs_answer` | `event` | Existing generic table, no schema change |
| Provider routing | `job.source == 'ats'` | Existing column, read not written differently; see "assumption to revisit" above |

`application.answers` is unchanged in shape (JSON text column); what changes
is *when* it's read versus recomputed (the correctness fix above).

## Testing

- `load_candidate_profile`/`save_candidate_profile` round-trip, and a clear
  error (naming the missing file and the `.example` template) when
  `candidate_profile.toml` doesn't exist.
- `qa_normalize` collision ("Notice period?" vs "notice period"); `qa_lookup`
  / `qa_upsert` round-trip; the 30-day volatility expiry (an answer confirmed
  31 days ago is treated as missing, not reused).
- The core correctness fix: across a draft-then-real-send sequence,
  `compute_answers` runs exactly once, not twice — the real send reuses the
  draft's stored answers.
- A job with `source != 'ats'` (LinkedIn, Naukri) still gets the categorical
  refusal on a real send even after `SUBMISSION_IMPLEMENTED = True`.
- The hold flow: a question with no `qa_bank` entry and no matching fact
  raises `NeedsAnswer`, leaves `run_state.current_job_id` set (parked, not
  cleared), and writes `needs_answer` — and a second `apply_tick` call while
  still parked does *not* re-pick or re-attempt the same job (the
  loop-safety property the parking mechanism exists for). Answering it via
  the dashboard clears the park and lets the next tick produce a draft.
- Greenhouse field-matching against a saved synthetic form page (no real
  network) — standard fields by element id, custom questions by normalized
  label text.
- Captcha detection still raises `CaptchaEncountered` from the new filler
  functions, same as `_default_filler` today.
- Regression guard: with `SUBMISSION_IMPLEMENTED = False` (the shipped
  default), an `auto`-mode job with verdict `submit` still gets refused at
  the real-send step exactly like before this feature existed — so a future
  change can't silently turn real sends on.

## Deferred

- Lever and Ashby real fillers, and the `job.provider` column that becomes
  necessary once either exists.
- A dedicated `qa_bank` browse/edit page — reactive hold-and-answer is the
  entire UI this round.
- PDF résumé rendering (already deferred from v2).
- Any auto-send-specific safety cap beyond the existing daily cap and pause
  flag — not requested, not built.
- Naukri submission — no Actor exists, no connector planned. Discovery-only,
  per the v1 spec, unconditionally.
