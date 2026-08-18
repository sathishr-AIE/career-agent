# Career Agent v1: design

Date: 2026-08-18
Revision: 8
Status: closed for revision. Build from this.

Scope of this document: the first shippable milestone. Later phases are named where
they constrain a v1 decision and otherwise left out.

## Revision history

**Revision 2** (external review, Asenia): auto-submission moved out of v1; gate split
into a deterministic hard filter and a scored stage; five dimensions named and
weighted; `resume`, `qa_bank`, and `outcome` tables added; two-phase submission;
spikes moved ahead of implementation.

**Revision 3** (second review round). Two spec bugs and one regression fixed:

- **`UNIQUE(job_id)` contradicted the status lifecycle.** Revision 2 described drafts
  and retries as non-blocking while keeping a constraint that permits one row per job.
  Both cannot hold. Replaced with a partial unique index.
- **Idempotency and resume was dropped between revisions 1 and 2.** Restored, with the
  `prompt_version` rule stated explicitly.
- **Cold start on an empty `fact` table** would have skipped the entire queue on day
  one, because `credibility` carries 30% weight and a hard floor.

Also added: per-source throughput measurement, human-decision calibration, volatility
flags on `qa_bank`, and a cost model.

**Revision 4** (third review round). One defect that would have corrupted the v3
decision, plus five smaller gaps:

- **The gate could never be caught being too harsh.** Skipped jobs were invisible, so
  calibration would have collected false positives and no false negatives. Fixed.
- **`no_response` was unenterable by hand**, which would have inflated callback rate
  by shrinking the denominator to applications you remembered to annotate. Now derived.
- Retry cap on `failed`; resolution actions for `possible_duplicate`; a sufficiency
  check rather than an emptiness check on `fact`; subscription-mode rate limits in the
  cost model; one denominator for Spike 2.
- **Candidate seniority resolved** at 1 to 3 years.

**Revision 5** (fourth review round). Two regressions revision 4 introduced, plus
cleanup:

- **The unified Spike 2 denominator silently recalibrated its own thresholds.** The
  8/80 rule was set against human-judged relevance; revision 4 attached it to
  hard-filter survivors, which are far more numerous. Now two numbers.
- **"Spread the run across the day" cancels the prompt caching the cost table
  assumes.** Cache TTL is minutes. Removed that mitigation.
- Sampling and overrides now distinguish hard-filter skips from scored skips; merge
  is a soft delete; `career_brief.toml` is the single source of truth; `daily_cap` has
  a value; outcome supersession has a query rule.
- **Agent loop resolved: deterministic pipeline.**

**Revision 6.** Discovery queries are now driven by the career brief rather than by a
command-line flag, and the preferred search location is set to Chennai. Revisions 1
through 5 filtered on location after fetching but never used it when asking, which
meant the agent could spend Actor runs collecting jobs the hard filter was guaranteed
to discard.

**Revision 7.** Scheduling is now opt-in. With no trigger installed the agent does
nothing on its own and runs only when you open the application.

## How the agent is triggered

The agent has two ways to start, and the quiet one is the default.

**On demand.** You open the application and run it. Discovery, filtering, and scoring
happen while you wait, and the dashboard shows the results. Nothing runs before you
ask, and nothing runs after you close it.

**Scheduled.** You install a Windows scheduled task, and the same pipeline runs daily
without you.

**No trigger is installed by default.** A fresh checkout has no scheduled task. Until
you run `scripts/install-scheduler.ps1`, the agent is inert between the moments you
choose to use it. This matters for a tool holding your career brief and driving a
browser: a background process you did not deliberately start is a background process
you will forget is running.

The two modes share one code path. `career-agent run` performs a single pass either
way, so the scheduled task is nothing more than that command on a timer. There is no
resident daemon in either mode.

`career-agent serve` starts the dashboard and, on launch, checks whether a scheduled
task exists. If none does, it says so and offers to run a pass now. That is the whole
on-demand experience: open it, see that nothing has run since last time, click once.

The `--send` flag is unrelated and unaffected. Submission still requires a human
click in both modes.

## What v1 delivers

Discovery across LinkedIn, Naukri, and ATS boards; a deterministic hard filter; an
LLM-scored gate; a dashboard; one-click manual apply; outcome tracking.

Every submission is a human click. Nothing fires unattended.

## Why nothing auto-submits yet

An application is a consumable resource. There is roughly one useful attempt per
company per role. A generic auto-submitted resume does not merely fail to convert; it
spends the attempt and closes the door on a better application later.

The time cost of a job search is dominated by scanning, not clicking. Discovery plus
a trustworthy gate recovers most of the hours at none of the irreversibility.

| Phase | Delivers | Gate to advance |
|---|---|---|
| **v1** | Discovery, hard filter, scored gate, dashboard, manual apply, outcome tracking | The gate agrees with your judgment, and callback data exists |
| **v2** | Per-role tailoring from the facts store, resume rendering | Nothing claimed that you cannot defend |
| **v3** | Auto-submission within limits | Tailoring proven, and outcomes show tailored applications convert |

## Prerequisites before the first scored run

Neither is optional. Both produce garbage if skipped.

**The facts store, 20 to 40 entries.** `credibility` carries 30% of the weighted
score and a hard floor of 60. On an empty `fact` table the model has nothing to
credit, scores credibility near zero, and every job skips. The queue would be empty
on day one and the cause would not be obvious.

Cover your main projects, measurable outcomes, and defensible technical claims. The
gate is only as good as this table.

Enforced by count, not by emptiness. Three facts produce the same failure as zero,
just more quietly, so the guard checks sufficiency:

- Fewer than 10 facts: `score_job` raises. The gate does not run.
- 10 to 19 facts: runs, but logs a warning on every scored job that credibility
  scores are probably depressed by a thin store.
- 20 or more: silent.

**The board list, 200 to 400 companies.** Greenhouse, Lever, and Ashby are not
globally searchable. Each is queried per company by board token, so the ATS lane
cannot run without a curated list. Stored as `ats_boards.toml`, version controlled.

```toml
[[board]]
provider = "greenhouse"
token = "anthropic"
company = "Anthropic"
tier = 1
```

This list is plausibly the most durable thing the project produces. A ranked set of
companies worth working at, with a live feed of their openings, outlives whatever
automation sits on top of it.

## Spikes, before implementation

**Spike 1: subscription auth. PASSED 2026-08-18.** Kept below for the record. Run `claude setup-token`, set
`CLAUDE_CODE_OAUTH_TOKEN`, make one trivial Agent SDK call. If the installed SDK
invokes the CLI in bare mode the token is ignored and billing reverts to
pay-per-token. This gates the economics of everything below and must not wait for
first run.

**Spike 2: measure throughput per source.** Total volume is not the useful number.
The build order depends on the split.

Run in two parts, because the ATS half depends on the board list:

- **2a, immediately:** LinkedIn and Naukri discovery only, one week.
- **2b, after the board list exists:** add ATS discovery, one week.

Do not apply the decision rule to 2a alone. A partial measurement compared against a
total threshold is worse than no measurement.

### Two numbers, not one

Revision 4 defined a relevant listing as one surviving the hard filter, so that the
throughput decision and the cost model would share a unit. That unified the unit and
broke the threshold.

The 8/80 rule was set against *genuinely* relevant listings, meaning ones you would
actually consider. The hard filter is deliberately coarse: location, comp floor where
stated, title family, staleness, exclusions. It passes a large volume of roles you
would dismiss in two seconds. The same search that yields 8 genuinely relevant might
produce 60 survivors, which under revision 4's wording reads as "volume handling
earns its place, build v3" when the opposite is true.

So measure two things per source:

| Number | How | Feeds |
|---|---|---|
| **Survivors/day** | Automatic count of hard-filter passes | The cost model, since this is what gets scored |
| **Genuinely relevant/day** | You thumbs-up or thumbs-down a sample of 20 survivors a day, roughly 30 seconds, extrapolated to the day's total | The 8/80 throughput decision |

The 8/80 rule attaches to the second number, which is what it was calibrated against.

This also restores 2a to being an actual spike. Counting hard-filter survivors would
have required the hard filter and a written `career_brief.toml`, making it a milestone
rather than a pre-implementation probe. Raw discovery plus your own eyeballs needs
neither.

Decision rules on the combined figures:

| Signal | Consequence |
|---|---|
| Around 8 genuinely relevant/day | The bottleneck was never clicking. Scope toward tailoring quality and warm introductions; treat the queue as research |
| Around 80 genuinely relevant/day | Volume handling earns its place; v3 is worth building |
| ATS under ~20% of survivors | The dashboard and manual-apply experience *are* the product. `qa_bank`, Playwright form filling, and v3 get deferred indefinitely |

That last row is the one to take seriously. The normalization rules in this document
(`pvt`, `bengaluru` to `bangalore`) and the use of Naukri describe an India-centric
search. Greenhouse, Lever, and Ashby are US-origin platforms used mainly by US and
Western companies, plus Indian offices of US firms and US-VC-backed Indian startups.
Indian employers at large use Naukri, which is discovery-only here with no submission
path. The ATS lane is the one that justifies `qa_bank`, form automation, and
eventually v3. If it turns out to be a tenth of the realistic listings, most of that
work serves a tenth of the value.

## Architecture

Three lanes, separated by risk. Discovery never touches an authenticated session.
Filtering and scoring never leave the machine. Only submission handles credentials,
and in v1 only a human starts it.

```
Task Scheduler
      |
      v
career-agent run
      |
      |  DISCOVERY (no login anywhere)
      |    Apify ──> LinkedIn Actor, Naukri Actor
      |    httpx ──> Greenhouse / Lever / Ashby, from ats_boards.toml
      |         |
      |         v  normalize, fingerprint, drop stale
      |    ┌─────────────┐
      |    │  SQLite     │
      |    └─────────────┘
      |         |
      |  HARD FILTER (deterministic, zero tokens)
      |    location, comp floor, title family, work auth, exclusions
      |         |  survivors only
      |  SCORED GATE (one LLM call per surviving job)
      |    five dimensions ──> submit / hold / skip
      |
career-agent serve ──> dashboard ──> [Apply] ──> Playwright ──> outcome recorded
```

## Discovery queries come from the brief

Every source is asked a question before it returns anything, and that question is
built from `career_brief.toml`, not from a command-line flag.

```toml
target_titles   = ["AI Engineer", "Machine Learning Engineer", "Applied AI Engineer"]
search_locations = ["Chennai"]
remote_ok        = true
```

The agent runs one query per title per location per source. With three titles, one
location, and two Apify sources, that is six Actor runs a day, plus one HTTP fetch per
company on the board list.

**Why this is worth stating separately.** Earlier revisions filtered on location after
fetching but never used it when asking. A run could pull fifty Bangalore listings and
then discard all fifty at the hard filter, having paid Apify for every one. Filtering
is not the same as searching, and doing only the first wastes the money and the time
the second would have saved.

### Locations and remote are different parameters

`search_locations` holds city names that go into the query. Remote is not a city, and
the Actors model it separately: the Naukri Actor takes `workMode`, and LinkedIn
encodes it in the search itself. So `remote_ok = true` adds a second pass per title
with the remote flag set, rather than sending the string "Remote" as a location and
hoping.

### Preferred location for v1

`search_locations = ["Chennai"]`.

Chennai is the primary market for this search. Adding a city is a one-line edit to the
brief, and the cost of doing so is linear: each added city multiplies the daily Actor
runs by the number of titles.

The hard filter still checks location on the way back, because a source can return a
job that does not match what was asked, and because a job discovered under
`remote_ok` still has to be genuinely remote. Searching narrows the ask; filtering
enforces the answer. Both stay.

## The gate, in two stages

### Stage 1: hard filter, deterministic

Runs in SQL and Python. Costs nothing. A failure here is a hard fail, never a low
score, and the job never reaches the model.

- Location outside the accepted set and the role is not remote
- `comp_max` below the salary floor, when compensation is stated
- Title outside the accepted title families
- Work authorization the candidate does not hold, where stated
- Company on the exclusion list
- Listing older than `staleness_days`

A role 40% under the salary floor must not be rescuable by an attractive tech stack.
That is the specific failure this stage prevents.

Jobs failing stage 1 are recorded with `verdict = 'skip'`, `stage = 'hard'`, and the
failing rule as the rationale, so the dashboard can still explain the absence.

### Stage 2: scored gate, one LLM call

Runs only on survivors. Five dimensions, each 0 to 100.

| Dimension | Judges | Weight |
|---|---|---|
| `role_fit` | Match to target titles, stack, domain, career direction | 0.30 |
| `credibility` | Whether verified facts support a strong application without exaggeration | 0.30 |
| `opportunity` | Company reputation, role clarity, growth, freshness, warning signs | 0.20 |
| `application_quality` | Whether a complete, non-conflicting application can be assembled | 0.15 |
| `eligibility_soft` | Residual eligibility stage 1 could not decide deterministically | 0.05 |

Verdict rules, in order:

1. Any dimension below 40, or `credibility` below 60: **skip**.
2. Weighted score at or above `gate_threshold`, default 72: **submit**.
3. Otherwise: **hold**.

`credibility` has its own floor because a low score there means the application would
misrepresent the candidate. That is not tradeable against a good stack.

## Calibration

Three sources, in increasing order of value.

**The golden set.** 20 hand-labelled listings, run on every prompt change, reporting
disagreements rather than asserting perfection. A smoke test. It measures whether the
gate imitates your snap judgment, which is not the same as measuring whether it is
right.

**Human decisions, free and continuous.** In v1 every apply is a human click, so
every click and every deliberate dismissal is a label. Recorded as `human_applied` or
`human_dismissed` events against the job's stored verdict. This costs nothing and
tracks how your judgment moves rather than freezing it at 20 fixed labels.

A dismissal needs its own button. Without one, a job you rejected and a job you never
opened look identical in the database, and half the signal is noise.

### Seeing the gate's false negatives

A dashboard that lists only `submit` and `hold` can never catch the gate being too
harsh. You can only click what the gate already approved, so the record fills with
false positives (it said submit, you disagreed) and contains no false negatives (it
said skip, you would have applied).

False negatives are the more dangerous error. They shrink your opportunity set
silently, and nothing in the system would ever surface one. The `credibility` floor
of 60 makes this concrete: early on, while the facts store is still thin, the gate
will skip roles you are genuinely qualified for, and under a submit-and-hold-only
dashboard you would never learn that.

Two mechanisms, both cheap:

- **Skipped jobs are visible behind a filter**, and applying anyway is permitted.
  Doing so records a `human_override` event. That override is the single most valuable
  label the system produces, because it is the gate erring in the expensive direction.
- **Deliberate sampling.** Ten random `stage = 'scored'` skips surfaced for review each
  week for the first month. Overriding roughly 3 of 10 means the threshold or the
  credibility floor is set wrong, not that you got unlucky.

**Sample scored skips only.** Hard-filter rejections are also stored as
`verdict = 'skip'`, and there are many more of them. A sample drawn from all skips
would be mostly roles in the wrong city or under the salary floor, which teaches
nothing about the gate. Scored skips carry dimension scores, and a `credibility < 60`
skip on a thin facts store is precisely the error this mechanism exists to catch.

**Two kinds of override, deliberately unequal.** Overriding a scored skip is the
normal calibration path described above. Overriding a *hard-filter* skip contradicts
that stage's stated purpose, so it is a separate and louder action: a confirmation
step, and the overridden rule is recorded on the event.

That recording matters. Regularly overriding the salary floor means the floor is
wrong and belongs edited in `career_brief.toml`, not bypassed job by job. A per-job
override that repeats is a configuration bug wearing a workaround.

Guardrail rule 1 is relaxed accordingly: a `skip` verdict may be submitted when the
origin is a dashboard override. It still cannot be submitted automatically.

**Outcomes, the real ground truth.** Callback rate is what the gate should predict.
The `outcome` table exists from day one because calibration data cannot be
retrofitted. Twenty labels is a smoke test. Forty callbacks is calibration.

## Data model

| Table | Holds | Key columns |
|---|---|---|
| `fact` | Verified, defensible experience | claim, evidence, project, metric, confidence |
| `qa_bank` | Canned answers to recurring application questions | question_normalized UNIQUE, answer, is_volatile, last_confirmed_at |
| `resume` | Resume versions and file locations | version UNIQUE, path, is_default, created_at |
| `job` | A normalized listing | source, external_id, company, company_normalized, title, title_normalized, location, comp_min, comp_max, posted_at, url, description, fingerprint UNIQUE, merged_into_job_id |
| `assessment` | Gate output | job_id, five dimension scores, weighted_score, verdict, rationale, stage, model, prompt_version |
| `application` | What was sent, and how far it got | job_id, resume_version, answers, status, started_at, submitted_at, confirmation |
| `outcome` | What came back | application_id, type, occurred_at, notes |
| `event` | Timeline entry | job_id, type, payload, occurred_at |

### The career brief is a file, not a table

`career_brief.toml` is the single source of truth for target titles, title families,
locations, remote preference, salary floor, work authorization, non-negotiables,
exclusions, `daily_cap`, `gate_threshold`, and `staleness_days`. There is no
`career_brief` table.

Earlier revisions had both, which is one definition too many. The file wins because it
is version controlled and diffable, and a document that changes as your search changes
is worth having a history for. Seeing that you raised the salary floor three weeks
before response rate dropped is the kind of thing only a diff tells you.

Values set for v1: `daily_cap = 5`, `gate_threshold = 72`, `staleness_days = 30`,
`search_locations = ["Chennai"]`, `remote_ok = true`.

`daily_cap` needs a number even though v1 submits only on a human click. It is a
backstop against a bad afternoon, not a target.

### qa_bank and volatility

Every ATS posting asks what the facts store cannot answer: notice period, current
compensation, work authorization, "why this company". These are answers needing
consistency, not claims needing evidence.

Without this table the never-guess rule routes almost every ATS job to manual, which
defeats the lane.

Some answers decay. Notice period changes when you resign. Current compensation
changes at review. Auto-filling a stale value is a real harm: a wrong notice period
misrepresents availability, and a wrong current CTC anchors your offer.

`is_volatile` marks these. A volatile answer is never auto-filled; it is presented
for confirmation, and confirming updates `last_confirmed_at`. Non-volatile answers
(work authorization, degree, years of experience) fill silently.

### outcome

`type` is one of `no_response`, `rejected`, `screen`, `interview`, `offer`.

Four of those five arrive as email and can be entered by hand. `no_response` cannot:
nobody writes to say they are ignoring you, so nothing ever prompts you to record it.
Left manual, the table would fill with rejections and screens and stay silent on the
majority case, and since callback rate is callbacks divided by *total* applications,
the denominator would shrink to whatever you remembered to annotate. The rate comes
out inflated, and the v3 go/no-go decision runs on it.

So it is derived, not entered. A nightly job writes `no_response` for any `submitted`
application with no outcome row after 30 days, marked `derived = 1`.

**Supersession rule**, since both rows can exist: for any application, the outcome
with the latest `occurred_at` wins, and a derived row loses ties against a manual one.
A reply arriving on day 40 therefore overrides the day-30 `no_response` without
deleting it, and the history of "we assumed silence, then heard back" stays intact.
Callback rate counts only winning rows.

The other four stay manual in v1; v2's response loop populates them automatically.

### resume

v1 holds a single row, the default base resume. v2's tailoring writes one row per
generated variant, which is what makes "what exactly did I send them" answerable
months later.

## Fingerprinting

Fingerprint over normalized company, normalized title, and normalized location.
Posted date is excluded: the same role on LinkedIn and on the company's Greenhouse
board carries different dates, so including it breaks deduplication at exactly the
case it exists to catch.

- Company: lowercase, strip legal suffixes (`inc`, `ltd`, `pvt`, `llc`, `gmbh`,
  `technologies`, `labs`), strip non-alphanumerics.
- Title: lowercase, expand abbreviations (`sr` to `senior`, `jr` to `junior`, `eng` to
  `engineer`), drop parenthetical and post-comma qualifiers, sort remaining tokens.
  "Sr. Backend Engineer" and "Senior Software Engineer, Backend" must collide.
- Location: lowercase, strip country suffixes, map aliases (`bengaluru` to
  `bangalore`).

Exact fingerprint match is the key. A near-match, meaning identical company and
location with title edit distance under a threshold, raises a `possible_duplicate`
event rather than merging silently.

That event needs somewhere to go, or the dashboard accumulates a list that only ever
grows. Each pair gets two actions:

- **Merge**: the newer job gets `merged_into_job_id` set to the survivor, and its
  source and URL are appended to the survivor's record so both application routes stay
  reachable. Every query excludes rows where `merged_into_job_id IS NOT NULL`.
- **Not a duplicate**: writes a `duplicate_dismissed` event keyed on the job-ID pair,
  so the same suggestion is never raised twice.

Merge is a soft delete, not a `DELETE`. The merged job may already carry an
assessment, an event trail, or a draft application, and deleting the row would orphan
them and destroy the record that the duplicate was ever found. Soft delete is also
reversible, which matters because a wrong merge loses a job.

Neither action is automatic.

## Submission lifecycle

Row existence is not proof of submission. `application.status` carries the state:

```
draft ──> in_flight ──> submitted
              │
              ├───────> failed ──> (retry, max 3) ──> failed_permanent
              │
              └───────> held_unknown     (crash sweep; blocks further attempts)
```

| Status | Meaning | Blocks a new attempt? |
|---|---|---|
| `draft` | Rendered, not sent. What a dry run produces | No |
| `in_flight` | Written immediately before the browser presses submit | Yes |
| `submitted` | Confirmation observed and stored | Yes |
| `failed` | The attempt errored and did not send | No |
| `failed_permanent` | Three failed attempts. Stop trying | Yes |
| `held_unknown` | Crashed mid-submission; true state unknown | Yes |

**Retry cap.** A form that errors deterministically, because a required field cannot
be filled or the posting closed, would otherwise be retried forever. After three
`failed` rows for the same job the next transition is `failed_permanent`, which blocks
further attempts and surfaces in the dashboard for a manual look. `failed_permanent`
is distinct from `held_unknown` on purpose: a deterministic failure is known not to
have submitted, whereas a crash mid-send is genuinely unknown, and conflating them
would put "definitely not sent" jobs into a queue meant for "might already be sent".

### The uniqueness constraint

Revision 2 specified `job_id UNIQUE` alongside this lifecycle. Those contradict:
a unique column permits one row per job, while the lifecycle requires drafts and
failed attempts to coexist with a later real submission. Under `UNIQUE(job_id)` a dry
run would block the real send, which is the exact bug the lifecycle was introduced to
fix.

The constraint is a partial unique index instead:

```sql
CREATE UNIQUE INDEX one_live_application_per_job
  ON application(job_id)
  WHERE status IN ('in_flight', 'submitted', 'held_unknown', 'failed_permanent');
```

Drafts and transient failures accumulate freely, preserving attempt history. Two live
attempts remain impossible.

`held_unknown` is in the blocking set deliberately. An unknown-state attempt may
already have submitted, so permitting a second send would make the crash sweep a
cause of double applications rather than a guard against them. Blocking is the safe
direction when the truth is unknown.

This index is the real guarantee. The status check in application code is for a
readable error message.

### Stale in-flight sweep

On startup, any `in_flight` row older than 15 minutes indicates a crash during
submission. It moves to `held_unknown` and appears in the dashboard for you to
confirm against the employer's confirmation email. Guessing either way is wrong.

## Idempotency and resume

Restored from revision 1, where it was dropped in revision 2.

A run can die at any point. Every stage writes to SQLite before the next begins, so a
re-run resumes rather than repeats.

**Discovery** is idempotent through the fingerprint. Re-fetching the same listing is a
no-op.

**Scoring** skips any job holding an assessment at the current `gate.PROMPT_VERSION`.
A run that dies after scoring 40 of 80 jobs resumes at 41 rather than re-spending 40
calls.

**Bumping `PROMPT_VERSION` invalidates every existing assessment and forces
re-scoring.** This is the mechanism, not a side effect: changing the prompt and
re-running the golden set is only meaningful if old verdicts are recomputed. Any
prompt edit requires a version bump in the same commit.

**Submission** is protected by the partial unique index and the stale sweep above.

## Guardrails

v1 submits only on a human click, so the guardrail runs on the dashboard path.

1. The job's stored verdict is `submit` or `hold`, **or** the verdict is `skip` and
   the origin is a dashboard override. A `skip` can never be submitted
   automatically, only by a deliberate human override, which is recorded.
2. No `application` row for this job in `in_flight`, `submitted`, `held_unknown`, or
   `failed_permanent`.
3. The global pause flag is clear.
4. Today's submission count is below the daily cap.

Rule 2 is enforced twice: once in application code for a readable message, once by
the partial unique index. The index is the guarantee.

Auto-eligibility and mode checks are specified but inert in v1. They activate in v3;
their tests are written now.

## Error handling

| Failure | Behavior |
|---|---|
| Captcha appears | Abandon, mark held for review, screenshot to the timeline |
| LinkedIn session expired | Stop that lane, notify, continue others |
| Apify Actor fails or returns nothing | Log, continue with other sources |
| Subscription token expired | Fail loudly and stop. Never fall back silently |
| Fewer than 10 facts at scoring time | Raise. Do not score |
| 10 to 19 facts | Score, but warn that credibility is probably depressed |
| Third consecutive failed submission | Move to `failed_permanent`, stop retrying, surface in the dashboard |
| Question absent from `qa_bank` and unanswerable from facts | Do not guess. Hold, and prompt to add the answer |
| Volatile answer older than its confirmation window | Present for confirmation; never auto-fill |
| Crash mid-submission | Stale sweep moves it to `held_unknown` |

A captcha means the site has already classified the session as suspicious. Solving it
teaches nothing except how to be flagged more thoroughly.

## Cost model

Absent from both prior revisions.

### Per-call prompt size

One call per surviving job:

| Component | Tokens | Varies per job? |
|---|---|---|
| Career brief | ~300 | No |
| Facts store, 20 to 40 entries with claim, evidence, project, metric | ~2,000 | No |
| Prompt template | ~400 | No |
| Job description, truncated to 6,000 characters | ~1,500 | Yes |
| **Input total** | **~4,200** | |
| Output, a JSON verdict | ~150 | Yes |

Revision 3 estimated the facts store at 500 tokens, which was low by roughly 4x. It
is the largest single component, not a rounding error, and it grows as the store
does.

**2,700 of those 4,200 tokens are identical across every call in a run.** A cache
breakpoint after brief, facts, and template makes the repeated portion bill at
roughly a tenth, cutting effective input to about 1,770 per job.

The first call of each run pays a cache *write* instead, at roughly 1.25x, so the
"cached" figures below are optimistic by one call's premium per run. Negligible over
80 jobs, noticeable over 8.

### Subscription mode, the expected case

On a Pro/Max subscription token the marginal cost of a call is zero. The binding
constraint is the rolling usage window, not money.

The relevant risk is shape, not spend: 80 scoring calls fired in one morning batch is
a burst, and a burst is what trips a window.

**The mitigation is a per-run cap, not a spread run.** Cap scored jobs per run and
carry the remainder to the next scheduled run. The idempotency rule already makes this
safe, since scored jobs are skipped on the next pass.

Revision 4 also offered "spread the run across the day". That is withdrawn, because it
cancels the caching the cost model depends on. Prompt cache entries live for minutes,
not hours, so a run split into segments hours apart pays a fresh cache *write* per
segment. Writes bill at roughly 1.25x base input rate, so spreading the run costs more
than not caching at all. Batch tightly within each run; separate runs by hours if you
must, but accept that each run pays one write.

Exact window limits are not recorded here because they vary by plan and change. If
scoring starts hitting limits, the per-run cap is the lever.

### Pay-per-token mode, the failure case

This applies only if Spike 1 finds the SDK in bare mode. Rates are $5 per MTok input
and $25 output.

| Volume | Effective input/day | Output/day | Cost/day | Cost/month |
|---|---|---|---|---|
| 8 relevant/day, uncached | 34K | 1.2K | ~$0.20 | ~$6 |
| 8 relevant/day, cached | 14K | 1.2K | ~$0.10 | ~$3 |
| 80 relevant/day, uncached | 336K | 12K | ~$1.98 | ~$59 |
| 80 relevant/day, cached | 142K | 12K | ~$1.01 | ~$30 |

The hard filter reduces this bill directly, because it removes jobs before they cost
anything. That is a second argument for stage 1 beyond correctness.

### Apify

Metered per Actor run, and the only discovery cost regardless of which auth mode
Spike 1 lands in. Roughly $3 to $6 a month at this volume: LinkedIn at about $1 per
1,000 jobs with descriptions opt-in, Naukri pay-per-result. The Free plan's $5
monthly credit covers early testing. See `docs/research-browser-automation.md`.

### Total

**Settled at $3 to $6 a month, all of it Apify.** Spike 1 passed, so subscription
mode applies and model calls cost nothing at the margin. The pay-per-token table
above is retained as the counterfactual, not the forecast.

## Staleness

`staleness_days`, default 30, in `career_brief.toml`. Listings older than that are
dropped at discovery and never scored.

## Auth

Per `docs/agent-setup-guide.md`: `claude setup-token`, stored as
`CLAUDE_CODE_OAUTH_TOKEN` in a gitignored `.env`, with `ANTHROPIC_API_KEY` unset.
Startup runs a trivial call to confirm auth rather than inspecting the token.

Anthropic's terms do not permit third party developers to offer claude.ai login or
rate limits to their own users. This project stays single-user on the developer's own
quota. If it ever ships to other people, the auth model changes first.

**Bare mode: resolved. Spike 1 passed 2026-08-18.** The installed SDK accepts
`CLAUDE_CODE_OAUTH_TOKEN` and does not invoke the CLI in bare mode. Verified by
running `scripts/spike_auth.py`, which makes a real tool-less call and checks the
reply, rather than by inspecting the credential. Subscription mode is therefore the
live cost model.

## Testing

Guardrails get one test per rule. A guardrail never observed failing closed is an
assumption.

The partial unique index gets its own tests: a `draft` row does not block a real
submission; a `failed` row does not block a retry; `in_flight`, `submitted`, and
`held_unknown` each do block. That specific set is why the index exists.

The hard filter gets a table-driven test per rule, asserting rejection with the
correct reason. Pure functions, free to run.

Idempotency gets a test: scoring a job twice at the same `PROMPT_VERSION` produces one
assessment and one LLM call; bumping the version produces a second.

Calibration gets three: a `skip` verdict is rejected on the normal path and accepted
with a recorded `human_override` when the origin is a dashboard override; a
`submitted` application with no outcome after 30 days is derived to `no_response` by
the nightly job, and a manual outcome arriving later supersedes it under the
latest-wins-derived-loses-ties rule; three `failed` rows move the next attempt to
`failed_permanent` and the fourth attempt is refused.

Fingerprinting gets the collision this document specifies by name. "Sr. Backend
Engineer" and "Senior Software Engineer, Backend" at the same company and location
must produce the same fingerprint. It is the one place with exact required behavior
written down, so it is the one place that gets an explicit assertion rather than a
general property test.

The scored gate runs against the golden set on prompt changes, reporting
disagreements. Disagreement is information about the threshold, not necessarily a
defect.

## Resolved decisions

**Deterministic pipeline, not an agent loop.**

v1's run is: fetch from three sources, normalize, fingerprint, drop stale, hard
filter, score survivors, write. Six steps, fixed order, one LLM call, and no branch
the model is better placed to decide than a config file. With auto-submission out of
v1, the agent loop's remaining justification was orchestrating submission, and there
is no unattended submission left to orchestrate.

The cost was concrete: an in-process MCP server, a tool allowlist, a `PreToolUse`
hook, and a test per hook rule, all of it built to contain discretion the design chose
to introduce. As a pipeline, the guardrails become ordinary function calls on the
dashboard's submit path.

The Agent SDK is still used, for `score_job`, which is the one genuine model call. The
subscription-token auth from `docs/agent-setup-guide.md` is unaffected.

This reverses the original choice, and the reversal is cheap and reversible in the
other direction: if v3's submission orchestration turns out to want real discretion,
the loop can come back around a pipeline that will by then be well tested. Recorded
here so the reasoning survives the decision.

## Watch this in week one

Seniority landed at 1 to 3 years, which interacts badly with the `credibility` floor
of 60.

An early-career candidate has fewer defensible facts. Fewer facts means credibility
scores lower across the board, and a hard floor of 60 will skip aggressively in the
first month, before the facts store has filled out. The override mechanism is what
catches this, but only if you look.

So watch the override rate specifically in week one. A high rate means the floor is
miscalibrated for a thin facts store, and the response is to lower the floor or add
facts. It does not mean the market is thin. Concluding the latter from the former
would be the most expensive misreading available, because it looks like evidence and
points the wrong way.

**Candidate seniority: resolved at 1 to 3 years.** Revisions 2 and 3 carried this
open after a review referred to a 15-year candidate. The reviewer has since retracted
that as a misreading of an earlier message, which leaves the vault's original
persona (early-career AI engineer, roughly 1 to 3 years) as the only sourced figure.

Consequences now locked in: title families target AI Engineer, ML Engineer, and
Applied AI Engineer at entry to mid level; cold volume converts well enough to
justify the queue-centred shape this document describes; and the board list stays a
supporting artifact rather than becoming the deliverable.

Had it been 15 years, cold applications would be the worst-converting channel
available and the system's best output would have been a ranked set of companies to
find a warm introduction into. That product is not this one. If the figure is wrong,
say so before `career_brief.toml` is written, because it sets the title families, the
salary floor, and the purpose.

## Deferred

- Per-role tailoring and resume rendering: v2.
- Recruiter message handling and calendar: v2.
- Auto-submission: v3, gated on tailoring plus outcome evidence.
- Naukri submission: no Actor exists, no connector planned. Discovery-only.
- Warm-path and referral detection: unscheduled. Lower priority now that seniority is
  settled at 1 to 3 years, where cold applications still convert.
