# Frontend redesign: design first, build later

Date: 2026-09-14
Status: Phase 1 (design). Screen 1 is approved. Screens 2 to 11 have their UX defined and
are waiting for Stitch designs (screen generation was unavailable on 2026-09-14).

## Goal

Redesign the whole React frontend, one screen at a time, in Stitch MCP. There are two
phases, and they are strictly separate:

1. **Design.** Each screen is designed in Stitch, reviewed and explicitly approved. No
   production React code changes during this phase. The only code work allowed is
   investigation.
2. **Implementation.** This starts only after every screen in the tracker is approved.
   Screens are built one at a time against their approved Stitch design, followed by a
   final integration pass.

The designs also cover backend capabilities the React app doesn't reach today. Each one
is recorded as a backend dependency, never built silently.

## Decisions

| Topic | Decision |
|---|---|
| Visual direction | Clean modern SaaS: light neutral surfaces, one accent colour, status colours for status only |
| Variants drawn in Stitch | Desktop light only. Mobile and dark mode are described in each screen's notes, not drawn |
| API gaps | Designed as first-class UI, and each screen lists its backend dependencies |
| Information architecture | A workspace sidebar plus a job hub (`/jobs/:id`) with Details and Chat tabs. Chat drawers are removed |
| Resumes | Its own screen, after Facts |
| Model selection | A context-aware picker in the chat composer (see below) |
| `POST /api/answer/{job_id}` | Not designed in, because chat cards replaced it. The route stays, per the Wiring Fixes plan U1 |

## Information architecture

**Sidebar (240px)**

- **Workspace:** Home (Home chat), Dashboard, Applications
- **Candidate:** Profile, Facts, Resumes
- **Agent:** Memory, Logins, Settings
- **Footer:** the agent status block, which replaces today's bottom `AgentStatusBar`. It
  shows:
  - the apply run state and mode
  - the pipeline state
  - "N cards waiting → Answer", linking to the newest open card's job chat

**Top bar (56px):** page title and breadcrumb (e.g. Applications › Stripe · SRE), with
page-level actions on the right. There is no global search, since no API supports one.

**Job hub (`/jobs/:id`):**

- A shared header shows company, title, verdict badge, score, application status and the
  primary action (Apply / Continue / Open draft).
- Two tabs, **Details** and **Chat**. The Chat tab shows a dot while a card is waiting.
- `/chat/:id` redirects to the job's Chat tab. The Home conversation lives at `/`.
- Every screen has its own URL, and the `?panel=` drawers go away.

## Design system

This is defined in Stitch as one project design system, applied to every screen.

- **Type:** Inter for UI. JetBrains Mono for scores, IDs, dates and counts, with tabular
  numbers.
- **Accent:** indigo `#4F46E5`. It is used only for the primary action, the active nav
  item and focus rings.
- **Status colours**, used only for status:

  | Colour | Means |
  |---|---|
  | Emerald | submit verdict, submitted |
  | Amber | hold verdict, waiting on you |
  | Rose | skip verdict, failed |
  | Sky | in progress, agent running |
  | Slate | draft, idle |

- **Shape and space:** 8px radius and a 4px base grid. Cards have a slate-200 hairline and
  a small shadow. Tables use dense 40px rows.
- **Shared components:**
  - Established on the Shell and reused everywhere:
    - badges (verdict, status, confidence)
    - buttons (primary, secondary, destructive, ghost)
    - inputs with an inline field error
    - cards, and a table with a filter tab bar
    - confirm dialog and toast
    - empty state, loading skeleton, and inline error for a backend refusal
  - Established on screen 5: the chat card family.
- **Dark mode:** token pairs in the design system, not drawn per screen.

If a later screen needs a shared pattern changed, the change and its effect on screens
already approved go back to the user for approval first.

## Model selection in chat

The picker sits in the composer toolbar, and what it controls depends on the chat.

- **Home chat:** sets the **scoring model**, meaning `setting.scoring_model` (today
  `claude-sonnet-5` or `claude-haiku-4-5`). It is the same value Settings edits, so there
  are two views of one setting and no second source of truth. Approved `find_jobs` runs
  use it.
- **Job chat:** sets the **apply-agent model** for the next Apply or Continue on any job.
  - While the job's session is live, the picker is locked and reads "Running on
    Sonnet 5". A running `claude -p` session can't change its model.
  - Choices are `claude-sonnet-5` (the default, matching today's `sonnet`) and
    `claude-opus-5`. Haiku is left out, because the browser-driving agent needs a
    stronger model.
- **The intent router stays hard-coded to haiku.** It's a cheap classifier and gets no
  picker.
- Settings shows both model settings too, in its Models section, so they can be edited
  without opening a chat.

**Backend dependency, new slice MS1:**

- An `apply_model` column on `setting`, added with `_add_column_if_missing` and defaulting
  to `claude-sonnet-5`.
- An `APPLY_MODELS` tuple in `config.py`, next to `SCORING_MODELS`.
- `GET` and `PUT /api/settings` expose the column.
- **A partial update route, `PUT /api/settings/models {scoring_model?, apply_model?}`.**
  The chat picker can't use `PUT /api/settings`, which always requires
  `max_score_per_run` and, when the brief is present, every brief field. The new route
  validates each given model against its tuple, writes only the `setting` row, and
  returns the saved values. 422 on an unknown model.
- `ats.submit` and `agent.run_session` read it instead of the `APPLY_MODEL` constant.
- `/api/run/status` returns the live run's model, so the picker can show it locked.

## Screen tracker

| # | Screen | Design | Impl | Backend deps |
|---|---|---|---|---|
| 1 | Application Shell | **Approved** | Not started | OC1, `/api/pipeline/status` |
| 2 | Dashboard | UX defined; Stitch pending (outage 2026-09-14) | Not started | EV1, OC1 |
| 3 | Applications | UX defined; Stitch pending (outage 2026-09-14) | Not started | none |
| 4 | Job Details | UX defined; Stitch pending (outage 2026-09-14) | Not started | JD1 |
| 5 | Chat (Home + Job) | UX defined; Stitch pending (outage 2026-09-14) | Not started | MS1 |
| 6 | Facts | UX defined; Stitch pending (outage 2026-09-14) | Not started | FC1 |
| 7 | Resumes | UX defined; Stitch pending (outage 2026-09-14) | Not started | RS1, FC1 (links only) |
| 8 | Profile | UX defined; Stitch pending (outage 2026-09-14) | Not started | none |
| 9 | Settings | UX defined; Stitch pending (outage 2026-09-14) | Not started | MS1 |
| 10 | Memory | UX defined; Stitch pending (outage 2026-09-14) | Not started | MM1 |
| 11 | Logins | UX defined; Stitch pending (outage 2026-09-14) | Not started | LG1 |

Design statuses: Pending → UX defined → In review → **Approved**. Implementation statuses:
Not started → In progress → **Implemented**.

**Stitch outage (2026-09-14).** Screen generation timed out on every attempt, including a
one-line prompt in an empty project. Screen edits still worked. While it lasts, screens
advance only to "UX defined". No screen is approved without its Stitch design, and
reviews resume in tracker order.

## Backend dependencies

Slices H1 to H4 and M1 to M6 are defined in the Wiring Fixes plan
(`~/.claude/plans/give-me-the-plan-lovely-magpie.md`). As of 2026-09-14 **all of them are
in the working tree**:

- H4: `actions.job_message`, `AgentRun.add_note`, `checkpoint.add_note`, and the prompt's
  `NOTE` lines.
- M1 to M4: Applications polling, `failure_reason` and `/api/transcript/{id}`, draft
  actions, and the score dimensions in `LIST_SQL`.
- M5 and M6: the sidebar nav, and Settings sending `candidate_present: false`.

These are no longer design dependencies. The remaining new slices are:

- **MS1, model settings.** See "Model selection in chat" above.
- **EV1, global event feed.** `GET /api/events?limit=` returns recent `event` rows, joined
  to job company and title, newest first, for the Dashboard. Read-only. Per-job events
  come from JD1.
- **JD1, job detail.** `GET /api/jobs/{job_id}` is read-only and returns, in one payload:
  - the `job` row: company, title, location, `is_remote`, `comp_min`/`comp_max`,
    `posted_at`, `url`, `source`, `description`, `priority`, `discovered_at`
  - every `assessment` row, newest first: stage, verdict, rationale, the five dimensions,
    weighted score, model, `prompt_version`, `created_at`
  - every `application` attempt, newest first: status, `failure_reason`, `resume_version`,
    `started_at`/`submitted_at`, whether a transcript exists, and its `outcome` rows with
    the effective outcome marked
  - the `apply_checkpoint`, if any: status, step, mode, `resume_count` with `MAX_RESUMES`,
    `form_url`, `updated_at`, and the count of pinned notes (not the note text)
  - the tailored resume for the job, if any: version, summary, and bullets with their fact
    ids resolved to claims, as `resumes_context` already does
  - `conversation_id`, whether the job has an open card, and the job's `event` rows, newest
    first
  - `gate_threshold` from the career brief, so the score card can show the threshold

  A missing job returns 404. It adds no new actions: everything on the page calls routes
  that already exist.
- **OC1, open card count.** `/api/run/status` returns only the newest open job card. Add
  `open_prompt_count`, the number of open `agent_prompt` rows on job conversations, so the
  Shell and Dashboard can say "N cards waiting". Until then the UI says "Card waiting"
  with no number.
- **LG1, add a login.** `POST /api/logins {domain, login_url, email, password,
  replace?}` calls `credentials.put(..., created_by="user")`.
  - **Call `credentials.account_domain` explicitly first.** For `created_by="user"`,
    `put` itself only runs `normalize_domain`, so without this a hand-entered login could
    be saved for a bare label, an IP, or a shared ATS host the agent path refuses. 422
    with its message.
  - **No silent replace.** `put` upserts by domain. When a login already exists and
    `replace` isn't true, return 409 `{exists: true, created_by}`, and the UI confirms
    before resending with `replace: true`.
  - `email` and `password` are required (422). `login_url` is optional, must be `https`
    when given, and its host must match the domain (`credentials.host_matches`).
  - A missing or invalid `CREDENTIAL_KEY` (`CredentialKeyError`) returns 409, "Saving
    logins needs CREDENTIAL_KEY in .env", and nothing is written.
  - The password is write-only: never returned, never logged, and never shown again.
    The response is the metadata row, the same shape as `list_`.

**HS1 was dropped, because it isn't a gap.** `store.save_hard_skip` writes an `assessment`
row with `stage = 'hard'`, `verdict = 'skip'` and the reason as `rationale`. `LIST_SQL`
already returns `stage`, so the Skipped tab can separate hard-filter skips from gate
skips without any backend change.

- **FC1, fact citations in the list.** `GET /api/facts` items gain `cited_by: [resume
  versions]`, computed with the existing `store.fact_cited_by`. Today a user finds out a
  fact is cited only when a delete is refused with 409, and editing a cited fact silently
  changes the citations those resumes display (`resumes_context` resolves claims live).
  Read-only; the 409 delete guard stays the authority.

- **RS1, resume version links.** `resumes_context` versions gain `job_id` (the resume
  row's own column) and `prompt_version` (already stored in the content JSON as
  `TAILOR_PROMPT_VERSION`). Read-only. It lets a version open its job hub and show which
  tailoring prompt produced it.

- **MM1, memory source job.** `store.memory_list` items gain `source_job_id`, which the
  query already selects, so a memory can show "Learned from Stripe · Senior SRE" and link
  to that job hub. Read-only.

An API gap the designs expose is written down here, never fixed silently. The backend is
not redesigned just to make a visual design possible.

## Screen UX definitions

These are written before each Stitch design. They are the brief the design must meet.

### 2. Dashboard

- **Purpose:** how the search is performing, and whether anything needs you.
- **Primary task:** notice what needs attention. **Secondary tasks:** run the pipeline,
  read funnel and outcome trends.
- **Primary action:** Run pipeline, in the top bar. It is disabled while the pipeline is
  `running`, and enabled when `idle` or `error`, as today.
- **Data:** one `/api/overview` poll every 3 s, which already includes the pipeline
  status. `/api/run/status` covers "Needs you". The event feed needs EV1.
- **Layout, top to bottom:**
  1. **Six KPI tiles:**
     - Discovered, After filter, Shortlisted, Applied, Responses, each with a sparkline.
     - **Today:** `today_submitted / daily_cap` with a thin bar. The backend already
       returns both, but today's React page never shows them.
  2. **Pipeline card (2/3 width):**
     - status badge and start time
     - a five-step stepper: Discover → Clean → Filter → Score → Ready
     - a progress bar, with the Score band filling by `scored / max_score`
     - counters: found, duplicates, passed, scored / max, shortlisted
     - `last_error` as an inline refusal alert
     - the run-scoped live activity list, or "No activity in this run"
  3. **Needs you (1/3 width):**
     - card waiting → Answer (links to the job hub Chat tab)
     - resumable sessions (`stats.resumable`) → Review
     - recent failures (EV1) → Open
     - an empty state when none of these apply
  4. **Activity (1/3 width, under Needs you):** a global EV1 feed with type icons and
     relative times, plus "View all activity".
  5. **Recent discoveries table:** verdict badge, title, company, source, score, location.
     Rows open the job hub.
  6. **Three cards:**
     - Source performance
     - Score distribution: four buckets, coloured by verdict band
     - Outcomes this month: applied, responses, interviews, offers, callback and interview
       rate, plus a short recent-outcomes list
- **First-run state**, shown while nothing has been scored:
  - A "Get your first shortlist" checklist replaces the empty tables.
  - The steps are: brief loaded, facts at N of `min_hard` (scoring blocked below it),
    master resume uploaded, run the pipeline.
  - Each step has one action (Settings, Facts, Resumes, Run pipeline).
- **Removed:** the "Ready to apply?" banner. The Shortlisted tile links to the Applications
  Queue instead.
- **States:**
  - loading: skeleton tiles and cards
  - pipeline `error`: rose badge, the error marker on the current stage, the `last_error`
    alert
  - Run Now refused (409): inline refusal on the Pipeline card
- **Narrow width:** tiles go to 3×2, then 2×3. The two-column rows stack, with Needs you
  first.

### 3. Applications

- **Purpose:** decide what the agent applies to, and follow every application to an
  outcome.
- **Primary task:** work the queue (Apply, reorder, skip). **Secondary tasks:** control
  the apply run, resolve held and failed rows, record outcomes, override gate skips.
- **Data:** one `/api/applications?show=skipped` poll every 3 s. It returns every verdict,
  and all tabs filter it on the client.
  - Polling must not reset open row expansions, forms or a busy state.
  - There is no pagination, per the Shell decision.
- **Top bar actions (run controls):**
  - a Manual / Auto segmented control, locked while the run is `running`
  - the buttons follow the run state:

    | Run state | Buttons |
    |---|---|
    | `idle`, `stopped`, `error` | Start |
    | `running` | Pause, Stop |
    | `paused` | Resume, Stop |

  - a refusal shows as a toast, and the error also stays inline under the run strip
- **Run strip card** (above the tabs):
  - status badge and mode
  - the current job, with "Card waiting · Answer →" when there is an open card
  - six compact stats: Total applied, Queued, In progress, Successful, Failed/Skipped,
    Interrupted (`resumable`)
  - `last_error`
  - a collapsible "Recent events" log
- **Notices:**
  - an info alert when `scheduled` is false: "No scheduled run is installed", with the
    `career-agent run` and `install-scheduler.ps1` hints
  - the tailored-sent and outcomes-recorded line, only when `submission_implemented` is
    true
- **Tabs, with mono counts:**
  - **Queue:** `submit` and `hold` verdicts with no terminal status. Drafts stay here with a
    Drafted badge.
  - **All:** every `submit` and `hold` job. Status filter chips (All, Queued, In progress,
    Drafted, Submitted, Held, Failed) filter on the client.
  - **Skipped:** two segments.
    - **Gate skipped** (`stage = 'scored'`): scored, with a rationale.
    - **Hard filter** (`stage = 'hard'`): no score, shown as "—"; the reason is the
      rationale; read-only, with no actions.
    - The intro line explains that applying anyway records an override.
- **Columns:** Score (mono), Role and company (opens the job hub), Source, Verdict badge,
  Status badge, Actions.
- **Row expansion** (chevron) keeps the table dense. It holds:
  - the rationale
  - "Why this score", with the five dimensions as mini bars
  - the tailored resume download
  - any row form: Record outcome, Mark applied, "It was submitted"
- **Status badge and actions by row state.** Each row has one visible primary action; the
  rest go in a ⋯ menu.

  | Row state | Badge | Primary | ⋯ menu / expansion |
  |---|---|---|---|
  | Queued | slate "Queued" | Apply (opens the job hub Chat tab) | Move up, Move down, Skip |
  | Drafted | slate "Drafted" | Open draft | Redo draft |
  | In progress (`in_flight`) | sky "In progress" (amber "Waiting on you" if it has an open card) | Open chat | none |
  | Submitted | emerald outcome label, or "Awaiting response" (`no_response` is derived, never entered) | Record outcome: rejected / Response / interview / offer, date, notes | none |
  | Held (`held_unknown`) | amber "Held" with `failure_reason` | It was submitted (date) | "Not submitted — clear hold" (confirm dialog), Transcript |
  | Failed permanently | rose "Failed" with `failure_reason` | Transcript | Dismiss |
  | Untracked in All | none | Mark applied (date) | Dismiss |
  | Gate skipped | rose "Skip" verdict | Track anyway (override) | none |

- **States:**
  - loading: skeleton rows
  - empty Queue: "Nothing to apply to — run the pipeline, or check Skipped"
  - empty Skipped: "No skipped jobs yet"
  - each row action refusal: inline under the row, plus a toast
- **Narrow width:** the table becomes stacked row cards showing company and role, verdict,
  score and status badges, and the primary action. The run strip stats scroll sideways.

### 4. Job Details (job hub, Details tab)

- **Purpose:** everything known about one job in one place: why it was scored the way it
  was, what the agent has done, and what you can do next.
- **Primary task:** take the next action for this job. **Secondary tasks:** understand the
  score, read the posting, audit past attempts, and open the transcript or resume.
- **Route:** `/jobs/:id`, Details tab. Rows in Applications and Dashboard, the Needs you
  links and "Answer" all land in the hub. "Answer" opens the Chat tab.
- **Data:** one `GET /api/jobs/{id}` (JD1), polled every 3 s while an attempt is
  `in_flight` or the checkpoint is `running`/`waiting`, and otherwise only on focus. Every
  action calls a route that already exists.
- **Hub header, shared with the Chat tab:**
  - company, title, and a link to the posting (opens `url`)
  - verdict badge, score (mono), and the status badge. The status uses the same row-state
    rules as Applications, so both screens always show the same label.
  - location, a Remote badge when `is_remote`, compensation when present, and source and
    posted date in mono
  - one primary action, chosen by state:

    | State | Primary action | Other actions |
    |---|---|---|
    | Queued | Apply | none |
    | Drafted | Open draft | Redo draft |
    | In progress | Open chat | none |
    | Resumable | Continue where it left off | none |
    | Held | It was submitted | Not submitted — clear hold |
    | Submitted | Record outcome | none |
    | Gate skipped | Track anyway | none |
    | Hard skipped | none | none |

    Apply, Redo draft, Continue and Track anyway switch to the Chat tab after firing, as
    Apply does today.
  - a ⋯ menu with Move up / Move down / Skip (queued only), Mark applied (untracked only)
    and Dismiss
  - tabs: **Details** and **Chat**. Chat shows a dot while the job has an open card.
- **Body, two columns (main 2/3 + side 1/3):**
  - **Main column:**
    1. **Attention banner**, only when relevant:
       - amber "Waiting on you: <card title>" → Answer
       - sky "Session interrupted at step <step> — resume <n> of 3" → Continue
       - amber "Held — the agent may already have submitted this. Check the employer's
         site." with both resolve actions
       - rose "Failed permanently: <failure_reason>" → Transcript
    2. **Score card:**
       - the weighted score, large in mono, with the verdict badge and threshold context
         ("gate threshold 70")
       - the rationale
       - the five dimensions as labelled bars: Role fit, Credibility, Opportunity,
         Application quality, Eligibility
       - a footer in mono with the model, prompt version and scored date
       - "Previous assessments (n)", a disclosure listing older rows, e.g. after a
         `PROMPT_VERSION` re-score or a hard-filter row
       - a hard skip shows only "Hard filter: <reason>", with no dimensions
    3. **Tailored resume card:**
       - the version (mono), a Download link (`/resume/{version}`), and the summary
       - the bullets, each with fact chips linking to Facts
       - "Not tailored yet — tailoring runs on first Apply" when absent
    4. **Job description:** the posting text, collapsed to about 12 lines with "Show
       all", and an empty note when `description` is null
  - **Side column:**
    1. **Applications card:**
       - one row per attempt, newest first: status badge, started or submitted date,
         `failure_reason`, resume version, and a Transcript link when one exists
       - outcomes under each submitted attempt: the effective one as a badge and the history
         as small lines, with "No response" marked "derived after 30 days"
       - the Record outcome form (type, date, notes) inline under the submitted attempt
    2. **Apply session card**, only when a checkpoint exists:
       - status (running / waiting / resumable / done), step, and mode badge (manual / auto)
       - resumes used, "1 of 3" in mono, and last update
       - "2 notes pinned for the agent", a count only
       - the form URL as a link
    3. **Timeline:** the job's events, newest first, with type icons and mono timestamps.
       Human actions (`human_applied`, `human_override`) are marked "You".
- **States:**
  - loading: skeleton header and cards
  - 404: "This job no longer exists" with a link back to Applications
  - merged duplicate (`merged_into_job_id`): a notice linking to the surviving job
  - each action refusal: inline under the header action, plus a toast
- **Narrow width:** one column in this order: attention banner, header action, Score,
  Applications, Apply session, Resume, Description, Timeline.

### 5. Chat (Home at `/`, Job at the hub's Chat tab)

- **Purpose:** the human ↔ agent channel.
  - **Home:** ask for things. Find jobs and apply-to need approval; status, queue, help,
    pause, resume and stop answer at once.
  - **Job chat:** follow one application, answer its questions, review before sending,
    and guide it with notes.
- **Primary task:** answer the open card. **Secondary tasks:** send messages or notes,
  continue an interrupted session, pick the model.
- **Data** (unchanged routes):
  - `GET /api/chat/{cid}/messages?after=` every 3 s, returning messages, `open_prompt` and
    `resumable`
  - `POST /api/chat/{cid}/messages`
  - `POST /api/chat/prompts/{id}/answer`
  - `POST /api/chat/jobs/{id}/resume`
  - `/api/chat/conversations` for the Home list
  - `/api/run/status` for `submission_implemented`
  - the model picker needs MS1
- **Layouts:**
  - **Home (`/`):**
    - a chat column, max 760px, centered
    - a 320px right rail, **Conversations**: job chats with title, last message, relative
      time, and an amber dot while a card waits. Each opens that job's hub Chat tab.
    - This replaces today's conversation sidebar and drawer rail.
  - **Job chat:** under the hub header, the same 760px column, no rail. The header already
    carries job context and the primary action.
- **Message taxonomy.** Each type must be told apart at a glance, without reading the
  text:

  | Type | Source | Treatment |
  |---|---|---|
  | **Your message** (Home) | `role = user` in Home | right-aligned indigo-50 bubble, "You" |
  | **Your note to the agent** (Job) | `role = user` in a job chat | right-aligned **slate bubble with a note icon and a "Note to agent" label**. The system line that follows becomes a status under the bubble, not a separate message. |
  | **Agent message** | `role = agent` | left-aligned, no bubble, small agent avatar, full reading width |
  | **Agent question** (open ASK) | `role = prompt` + open `choice`/`text`/`approve` | left-aligned card with **an amber left border and an "Agent asks" chip**, plus a secondary "Why the agent asks" line from `why` |
  | **Approval request** (Home `find_jobs`/`apply_to`) | open `approve` card whose payload has `origin: home` | card with an indigo left border and a "Needs your go-ahead" chip. The text says what it spends (e.g. "uses Apify + scoring credits"). Approve / Not now. |
  | **Review before sending** (CONFIRM) | open `confirm` card | full-width card, see below |
  | **Account creation** (`approve_account`) | open card | amber card with a key icon, see below |
  | **Signing in** (`need_password`) | card answered by the backend | sky info row: "Signing in to <domain> with your saved login — the agent never sees the password". No actions. |
  | **System notice** | `role = system` | centered, small slate text between hairlines, with an icon by content (resumed, interrupted, stopped, refused) |
  | **Closed card** | `prompt_status` `answered`/`expired` | collapsed one-line row: question and a badge, emerald "Answered" or slate "Expired". It expands to show the original card read-only. |
  | **Handoff to a job chat** | message whose payload has `conversation_id` | agent message plus a secondary button, "Open job chat", to the hub Chat tab |

  Note status lines:
  - live run: "Queued — reaches the agent with your next answer"
  - no live run: "Saved, not sent — no live session"

  A note is guidance only. The label must never suggest it approves or sends anything.
- **ASK card controls:**
  - `choice`: option buttons, with the default option outlined indigo
  - `text`: an input pre-filled with `default`, and Send
  - both: a "Remember this for future applications" checkbox, on by default
  - `approve`: Approve (primary) and Reject
  - a refusal from the answer route shows inline in the card, and the card stays open
- **CONFIRM card ("Review before applying"):**
  - Header: the mode badge (manual), plus a slate "Submission off — approving saves a
    draft" note while `submission_implemented` is false.
  - A fields table (label / value). Secret-shaped rows (the same regex as today) show a
    lock icon, and in edit mode stay read-only with the "Secrets are never typed here"
    note.
  - Files and account actions as lists. **Memory used as chips** (`memory_used` is in the
    payload today but not shown). `notes` as a secondary paragraph.
  - Actions:
    - view mode: Approve (primary), Change an answer (secondary), Cancel application
      (destructive ghost)
    - edit mode: Save changes, disabled until a value changes and blocked while any
      changed value is empty; and Discard
    - cancel mode: inline "Cancel this application? The job will be marked cancelled." with
      Yes, cancel (destructive) and Keep it
- **approve_account card:**
  - domain, email, sign-up page and "Browser is on" URLs in mono, and the terms summary
  - a warning line that approving creates the account, accepts the terms, and has the
    backend generate, fill and save the password, which the agent never sees
  - Approve and Reject
- **Composer:**
  - a textarea: Enter sends, Shift+Enter adds a new line, the text is kept if sending
    fails
  - placeholder: Home "Message the agent"; job chat "Add a note for the agent"
  - job chat helper text under it: "Notes guide this application. They never approve a
    submission."
  - a toolbar, left: the **model picker chip**
    - Home: "Scoring model · Sonnet 5 ▾", choosing between `SCORING_MODELS`
    - Job chat: "Apply agent · Sonnet 5 ▾", choosing between `APPLY_MODELS`
    - while the job's session is live: locked with a lock icon, "Running on Sonnet 5",
      with a tooltip that a running session can't switch models and the choice applies
      to the next Apply or Continue
    - a change saves at once through `PUT /api/settings/models` (MS1) and confirms with a
      toast
  - toolbar, right: Send
  - **Secret refusal (422):** an inline rose alert above the composer, "Secrets are never
    typed into chat — saved logins are filled by the backend; manage them in Logins",
    with a link to Logins. The text stays in the composer so it can be edited.
- **Continue banner** (job chat, while `resumable`):
  - a sky card above the composer: "This session was interrupted — continue where it left
    off", with a primary Continue button
  - busy until the first new line after the resume arrives
  - a refusal (409) shows inline
- **Home empty state:** a short welcome line and suggestion chips that send text: "Find
  new jobs", "What's in my queue?", "Status", "Pause applying".
- **Job chat empty state:** "Nothing here yet — Apply starts the agent and its questions
  appear here."
- **Other states:**
  - loading: message skeletons
  - offline: after 3 failed polls, a slate banner "Can't reach the server — retrying"
  - new messages while you've scrolled up: a "New messages ↓" pill, with no auto-scroll
  - on append while at the bottom: auto-scroll
- **Narrow width:**
  - Home's Conversations rail becomes a "Conversations" button that opens a sheet
  - cards go full width
  - the CONFIRM table becomes stacked label/value rows

### 6. Facts

- **Purpose:** the verified claims the gate scores credibility against and tailored
  resumes cite. Scoring and tailoring refuse to run below `MIN_FACTS_HARD` (10).
- **Primary task:** get to, and stay above, enough defensible facts. **Secondary tasks:**
  edit a fact, remove a weak one, see what resumes rely on it.
- **Data:**
  - `GET /api/facts` returns `items`, `min_hard` (10) and `min_warn` (20), plus
    `cited_by` per item (FC1)
  - `POST /api/facts`, `PUT /api/facts/{id}` and `DELETE /api/facts/{id}` (unchanged)
  - 422 errors come back as `errors: {field: message}` and show beside each field
  - loaded on open and after each change; no polling
- **Top bar action:** "Add fact" (primary). It opens the add panel at the top of the list.
- **Coverage card** (top of page): the count against the thresholds, as a bar with tick
  marks at 10 and 20.

  | Count | Colour | Message |
  |---|---|---|
  | below 10 | rose | "Scoring and tailoring are blocked — add N more facts" |
  | 10 to 19 | amber | "Scoring works — N more gives more reliable verdicts" |
  | 20 or more | emerald | "Enough facts for reliable scoring" |

  The same threshold drives the amber dot on the Facts sidebar item and the Dashboard
  first-run checklist.
- **Intro line:** "Verified claims about your work. Only add what you can defend — resumes
  cite these."
- **Filters, client-side** (the list is small, and no API is needed): confidence chips
  (All, High, Medium, Low), a "Cited" toggle, and a text filter over claim and project.
- **Fact list:** one card per fact, in API order (oldest first):
  - claim as the card title, and evidence under it in secondary text
  - project and metric as small labelled chips, shown only when set
  - a confidence badge: High emerald, Medium amber, Low slate
  - "Cited by v7, v9" chips linking to Resumes, when `cited_by` is non-empty
  - actions: Edit (secondary) and Delete (destructive ghost). **Delete is disabled on a
    cited fact**, with a tooltip "Cited by resume v7 — edit it instead". The 409 refusal
    still shows inline if it happens.
- **Add and edit form** (the add panel, and the same fields in place inside a card):
  - Claim (required, one line)
  - Evidence (required, textarea: "what you shipped and the measurable result")
  - Project and Metric (optional)
  - Confidence (segmented High / Medium / Low, default High)
  - Save stays disabled until claim and evidence are filled, and, when editing, until
    something changes
  - Cancel discards
  - editing a cited fact shows an amber note: "Resumes v7, v9 cite this fact; your edit
    changes what they show"
- **Delete:** a confirm dialog, "Delete this fact?". If this delete would drop the count
  below 10, the body warns: "You'll have 9 facts — scoring and tailoring will be blocked."
- **States:**
  - loading: skeleton cards
  - load error: an inline refusal alert with the server message, and Retry
  - empty: a list-checks icon, "No facts yet — scoring needs at least 10", and Add fact
  - filters matching nothing: "No facts match these filters", and Clear filters
  - save or delete success: a toast
- **Narrow width:** the coverage bar stays full width, filters scroll sideways, and the
  card actions move into a ⋯ menu.

### 7. Resumes

- **Purpose:** the master template tailoring renders into, and every tailored version the
  agent has uploaded or will upload.
- **Primary task:** have a working master in place. **Secondary tasks:** inspect what a
  tailored resume says and which facts back each bullet, and download a version.
- **Data** (unchanged routes):
  - `GET /api/resumes` returns `master {exists, path, modified}` and `versions[]` with
    company, title, `created_at`, summary, and bullets with `fact_ids` and resolved
    `fact_claims`. RS1 adds `job_id` and `prompt_version`.
  - `POST /api/resumes` (multipart `.docx`) returns `{ok, message, changes[]}` and 422 on
    refusal.
  - `/resume/{version}` downloads a version.
  - loaded on open and after an upload; no polling
- **Rules the UI must say plainly:**
  - **Upload:**
    - only a `.docx` is accepted
    - a file without markers is prepared automatically, and `changes[]` lists exactly what
      was replaced
    - an unreadable structure is refused, never guessed at
    - a failed upload leaves the current master untouched
  - **Replacing the master affects future tailoring only.** Existing versions were
    rendered from the previous master.
  - **One tailored version per job, reused.** It's created on the job's first Apply, and
    Redo draft and Continue reuse it. There is no "re-tailor" action, so the page offers
    none.
- **Master card** (top):
  - Present: a file icon, the path (mono), last modified (mono), a "Ready for tailoring"
    emerald badge, and Replace master (secondary).
  - Missing: an amber state card, "No master resume — tailoring and Apply can't run until
    you upload one", with Upload master (primary).
  - **Upload area:** a drop zone or file picker, `.docx` only, busy while uploading.
  - **Upload result:**
    - success: an emerald alert with the message, and when `changes[]` is non-empty, a
      "What we changed in your file" list, e.g. `summary paragraph ("Senior SRE with…")
      replaced with <<SUMMARY>>`
    - refusal: an inline rose alert with the server message
  - A collapsible "How the template works" note: the two marker paragraphs
    `<<SUMMARY>>` and `<<PROJECT_BULLET>>` (cloned once per bullet), with everything else
    left untouched.
- **Tailored versions:** a list of expandable rows, newest first.
  - **Row:** role at company (links to the job hub, RS1), version (mono), generated date
    (mono), bullet count, prompt version (mono, RS1), and Download (ghost).
  - **Expanded:**
    - the summary paragraph
    - the bullets, each with fact chips ("#12 Cut p95 latency 40%") linking to that fact in
      Facts
    - a citation that no longer resolves shows a rose "Unknown fact #12" chip
  - A client-side text filter over company and title. There's no pagination, per the
    Shell decision.
- **States:**
  - loading: skeleton master card and rows
  - no versions: "No tailored resumes yet — one is created the first time you Apply to a
    job"
  - load error: an inline refusal alert with Retry
- **Narrow width:** the master card stacks, the upload area goes full width, and rows show
  role, version and Download, with the details moved into the expansion.

### 8. Profile

- **Purpose:** the single source of truth for who the candidate is. The apply agent fills
  real application forms from it, and its APPLICANT PROFILE outranks Memory. Settings no
  longer holds contact fields (M6).
- **Primary task:** keep the details the agent types into forms complete and correct.
  **Secondary tasks:** maintain work history and education.
- **Data** (unchanged routes):
  - `GET /api/profile` returns `{exists, profile}`, with an empty shape when no file
    exists.
  - `PUT /api/profile` sends the whole profile. It validates everything before writing
    anything and returns 422 `errors` keyed by dotted path (`candidate_email`,
    `address.city`, `work_history.0.title`).
  - Blank work and education rows are dropped before sending, as today. The client remaps
    row-index errors to stable row ids.
  - The server trims whitespace and clears `end` when `current` is set.
- **Privacy note** under the title: "Stored only on this machine in
  `candidate_profile.toml`, which is never committed. The apply agent uses it to fill
  application forms."
- **Layout:** a two-column form page, with section navigation on the left and the form on
  the right.
  - Left: section anchors, each with a completion mark — Contact, Personal, Address, Work
    history, Education.
  - Right sections:
    1. **Contact:** Full name*, Email*, Phone* (required marks), LinkedIn URL, Portfolio
       URL.
    2. **Personal:** Gender, a select with Decline to state (default), Female, Male,
       Non-binary and Other. Other reveals "Please specify". Helper text: "Used only
       when a form asks; Decline to state is always allowed."
    3. **Address:** Line 1, City, State, Postal code, Country, in a 2-column grid.
    4. **Work history:** one card per entry.
       - Fields: Company*, Title*, Start and End as **month pickers** (native
         `<input type="month">`, which already produces `YYYY-MM`), and "I currently work
         here", which disables End and shows "Present".
       - Description as a textarea.
       - Actions: Move up, Move down, Remove. "Add work entry" at the end.
       - A collapsed card summarises as "Title · Company · 2021-04 – Present".
    5. **Education:** the same card pattern. Institution*, Degree, Field, Start and End as
       month pickers, with Move up, Move down and Remove. Education gains reordering,
       client-side, to match work history.
- **Save:**
  - a **sticky save bar** appears at the bottom once the form differs from the last load
    or save: "Unsaved changes" with Discard and Save profile (primary, busy while
    saving)
  - leaving the page with unsaved changes asks for confirmation
  - on success: a toast "Profile saved", and the bar hides
  - on 422: an error summary at the top ("3 fields need attention", each linking to its
    field), the inline field errors, and the relevant section scrolled into view
- **States:**
  - loading: skeleton sections
  - no profile yet: an amber banner, "No profile saved yet — the apply agent can't fill
    contact details until you save one", and the form starts empty with Decline to state
    selected
  - load error: an inline refusal alert with Retry
- **Narrow width:** the section navigation becomes a horizontal scrolling chip row, all
  grids become one column, and the save bar stays pinned.

### 9. Settings

- **Purpose:** how the agent searches, filters, scores and paces itself. **No contact or
  candidate fields** — those live on Profile (M6).
- **Primary task:** tune the career brief. **Secondary tasks:** choose models and set the
  scoring budget.
- **Data:**
  - `GET /api/settings` returns `brief` (null plus `brief_error` when the TOML can't be
    read), `candidate_error`, `settings {scoring_model, max_score_per_run}` (plus
    `apply_model` with MS1), `scoring_models`, `model_labels`, and `apply_models` (MS1).
  - `PUT /api/settings` sends the whole form. It validates everything before writing
    anything, returns 422 `errors` by field name, and always sends
    `candidate_present: false`.
  - The brief is written back through `save_brief`, which keeps TOML comments.
  - Changes take effect on the next run, with no restart.
- **Layout:** the same pattern as Profile — section navigation on the left, a form on the
  right, and a sticky "Unsaved changes" bar with Discard and Save settings.
  1. **Search:**
     - Target titles* and Title families
     - Search locations*, with the helper "What we ask sources for — each city multiplies
       daily Actor runs"
     - Accepted locations, with the helper "What the hard filter accepts on the way back —
       usually a superset"
     - Remote acceptable (toggle)
  2. **Filters & eligibility:**
     - Work authorization
     - Salary floor (INR/year, number, blank means no floor, with that exact helper)
     - Excluded companies
     - Non-negotiables
     - Staleness (days, ≥ 1)
  3. **Apply pacing & gate:**
     - Daily application cap (≥ 1, a number stepper)
     - Gate threshold: a 0–100 slider paired with a mono number input, a tick at the
       current value, and the helper 'Weighted score at or above this scores "submit"'
  4. **Models & budget:**
     - Scoring model (a radio card per `SCORING_MODELS` entry showing `model_labels`),
       with the helper "Recorded on every verdict, so past scores stay attributable"
     - Apply agent model (a radio card per `APPLY_MODELS` entry, MS1), with the helper
       "Used for the next Apply or Continue; a running session keeps its model"
     - Jobs scored per run (≥ 0), with the helper "Caps model calls, not jobs examined. 0
       runs discovery and the hard filter only"
     - A note: "The chat composer's model picker changes these same two settings."
  5. **Candidate details:** a link card, "Name, contact details, address, work history
     and education live on Profile", with Open Profile. `candidate_error` shows here when
     present.
- **List fields are tag inputs** (Target titles, Title families, Search locations, Accepted
  locations, Work authorization, Excluded companies, Non-negotiables):
  - Enter or a comma adds a chip, × removes it, and a pasted comma list splits into chips
  - the page still sends comma-joined strings, so there's no API change
  - a required list with no chips shows its field error
- **Validation:**
  - numeric fields use native number inputs with their min/max
  - on 422: a "Nothing was saved" summary at the top with human field labels (not raw
    keys), each linking to an inline error
- **Broken brief** (`brief_error`):
  - sections 1–3 are replaced by a rose card: "Career brief unavailable: <error>"
  - with the explanation: "These fields are hidden rather than filled with defaults, which
    would overwrite your file with a brief you never chose"
  - Models & budget still save
- **States:**
  - loading: skeleton sections
  - saved: the toast "Settings saved — they take effect on the next run"
  - load error: an inline refusal alert with Retry
- **Out of scope:** editing `ats_boards.toml` (the job boards list). The user deferred it
  in the Wiring Fixes plan.
- **Narrow width:** the section navigation becomes chips, the slider stays full width, the
  radio cards stack, and the save bar stays pinned.

### 10. Memory

- **Purpose:** the answers the agent reuses on application forms instead of asking you
  again.
- **Primary task:** check and correct what the agent will answer on your behalf.
  **Secondary tasks:** remove a wrong answer, and mark an answer to be re-confirmed.
- **Precedence, stated on the page:**
  - APPLICANT PROFILE (Profile) comes first, then **preferences** (keyed rows), then
    **known answers** (literal questions), and only then does the agent ask.
  - So a profile field always wins over a memory.
- **Data** (unchanged routes):
  - `GET /api/memory` returns items with `label` (the `memory_key` for a preference, else
    the normalized question), `answer`, `kind`, `options`, `is_volatile`,
    `last_confirmed_at`, `use_count`, `last_used_at` and `is_preference`, plus
    `source_job_id` with MM1.
  - A keyed row's literal twins are already hidden by `memory_list`.
  - `PUT /api/memory/{id} {answer, is_volatile}`: an empty answer returns 422. A
    preference's edit also updates its hidden twins.
  - `DELETE /api/memory/{id}`: a preference's delete also removes its twins.
  - loaded on open and after each change; no polling
- **Intro line:** "Answers the agent reuses on forms. Your Profile always takes priority.
  Passwords and codes are never stored here."
- **Tabs, with mono counts:**
  - **Preferences:** keyed rows. The label is shown humanized, e.g. `notice_period_days`
    becomes "Notice period days", with the raw key in mono underneath.
  - **Known answers:** literal questions, shown as the question text.
  - Client-side text filter over label and answer.
- **Row / card:**
  - the label, the answer (prominent), and a kind badge (Text / Choice)
  - usage in mono: "Used 4× · last 2026-09-12"
  - "Learned from Stripe · Senior SRE" linking to the job hub (MM1)
  - **Re-confirm badge:** when `is_volatile`, a slate "Re-confirm every 30 days" chip.
    When `last_confirmed_at` is more than 30 days ago, it turns amber, "Due to re-confirm —
    the agent will ask again, suggesting this answer".
    - The comparison is naive UTC against SQLite's `datetime('now')` format, never local
      time.
  - actions: Edit (secondary) and Delete (destructive ghost)
- **Edit (in place):**
  - a `choice` row with `options` edits with a select of those options; a `text` row with
    an input
  - the **"Re-confirm every 30 days" toggle** (`is_volatile`) replaces today's misleading
    "Ask me again next time"
  - editing a preference shows: "Also updates this answer everywhere the agent learned
    it"
  - Save is disabled while the answer is empty or unchanged; Cancel discards
  - a 422 shows under the field
- **Delete:** a confirm dialog, "Forget this answer? The agent will ask you next time a
  form needs it." For a preference, add: "This also forgets it for every job it was
  learned from."
- **States:**
  - loading: skeleton rows
  - empty: a brain icon, "Nothing remembered yet — tick 'Remember this' when answering the
    agent's questions in chat"
  - filter matching nothing: "No memories match", with Clear
  - load error: an inline refusal alert with Retry
  - success: toasts
- **Narrow width:** rows become cards, with actions in a ⋯ menu.

### 11. Logins

- **Purpose:** saved site accounts the backend uses to sign in or create accounts during
  applications. **The agent never sees a password**, and this page never shows one.
- **Primary task:** see which sites have saved logins, and remove one that's wrong.
  **Secondary task:** add a login you already have (LG1).
- **Data:**
  - `GET /api/logins` returns metadata only: `domain`, `login_url`, `email`,
    `created_by` (`agent` or `user`), `created_at`, `last_used_at`
  - `DELETE /api/logins/{id}`
  - `POST /api/logins` (LG1)
  - loaded on open and after each change; no polling
- **Intro line and security note:**
  - "Passwords are encrypted on this machine with your CREDENTIAL_KEY. They are typed
    into the site by the backend only when the browser is really on that domain — never
    shown to the agent, never shown here."
  - A lock icon, and a link to where `CREDENTIAL_KEY` is documented (README).
- **Top bar action:** "Add login" (secondary; the agent normally creates accounts through
  approval cards).
- **Table:**
  - Site: the domain in mono, linking to `login_url` when present (opens a new tab)
  - Email
  - Created by: badge "You", or "Agent · you approved"
  - Created, and Last used (mono dates; "Never" when null)
  - Actions: Delete (destructive ghost)
  - client-side text filter over domain and email
- **Add login dialog (LG1):**
  - fields: Domain*, Sign-in page URL (optional, https), Email*, Password*
  - the password is write-only: `autocomplete="new-password"`, a show-while-typing toggle,
    the value cleared from memory once the dialog closes, never echoed back, and a
    "Write-only" tag
  - errors:
    - a domain refusal (422 from `account_domain`) inline under Domain, e.g. "Not a site an
      account can be saved for: boards.greenhouse.io — shared job-board hosts can't hold
      one login"
    - a sign-in URL that isn't https or doesn't match the domain, inline
  - **An existing login** (409 `exists`): the dialog swaps to a confirm, "A login for
    stripe.com already exists (created by the agent). Replace its email and password?",
    with Replace (destructive) and Cancel
  - **Missing key** (409): a rose alert, "Saving logins needs CREDENTIAL_KEY in .env —
    nothing was saved"
  - success: the toast "Login saved for stripe.com — the password can't be viewed again"
- **Delete:** a confirm dialog, "Delete the login for stripe.com? The agent will need your
  approval to create or sign in to an account there again."
- **States:**
  - loading: skeleton rows
  - empty: a key icon, "No saved logins. When a site needs an account the agent asks you
    in chat first; approved logins appear here", with Add login
  - load error: an inline refusal alert with Retry
- **Narrow width:** rows become cards showing domain, email, badge and a ⋯ menu; the Add
  login dialog goes full screen.

## Per-screen design workflow (Phase 1)

For each screen, in tracker order:

1. **Understand.**
   - Read the route and components, the API calls and data, the user actions and business
     rules, and the loading, empty and error states.
   - Look at how the screen relates to other screens.
   - Don't drop functionality just because it isn't prominent in today's UI.
2. **Define the UX.** Purpose, primary and secondary tasks, hierarchy, primary action,
   navigation, key states, and responsive notes.
3. **Design in Stitch.** One desktop light screen per state that matters, using realistic
   data and the project design system.
4. **Review.** Present the design, the key UX decisions and the backend deps, then stop and
   wait.
5. **Revise** until explicitly approved.
6. **Lock.** Mark it Approved here, record the decisions under "Approved screens", and move
   to the next screen. Don't implement it.

## Screen notes (scope each design must cover)

1. **Shell.**
   - Sidebar groups, the active and hover states, and the agent status block (idle,
     running, paused, cards waiting).
   - Top bar with breadcrumb.
   - The shared component sheet.
   - A narrow-width note: the sidebar collapses to icons, then to a sheet.
2. **Dashboard.**
   - Pipeline panel: status, stage, counters, Run Now.
   - Apply run stats.
   - The global event feed (EV1).
   - The resumable-sessions count.
3. **Applications.**
   - Tabs: All, Queue, Skipped (gate and hard skips), plus status filters.
   - Run controls: Start, Pause, Resume, Stop, auto/manual mode.
   - Row actions:
     - Apply, Override, Dismiss
     - priority ▲/▼, Skip, Retry
     - Mark applied, Record outcome
   - Drafts shown with a Drafted badge, plus Open draft chat and Redo draft.
   - Score plus "Why this score" (M4). Failure reason (M2).
   - Loading, empty and refusal states.
4. **Job Details.**
   - Job facts: source, location, URL.
   - Verdict, rationale, the five dimensions.
   - Tailored resume version and link.
   - Application status and outcome.
   - Failure reason and transcript viewer.
   - Event timeline.
   - Resumable checkpoint with Continue.
5. **Chat.** The design must clearly tell apart:
   - user messages
   - agent messages
   - agent questions (ASK cards: `choice`, `text`, `approve`, `approve_account`)
   - human notes to the agent (H4: "delivered with your next answer" / "not sent — no
     live session")
   - system notices
   - CONFIRM review cards (fields, files, account actions, memory used), with secret rows
     read-only
   - answered, expired and superseded card states

   It also covers:
   - Home approval cards (`find_jobs`, `apply_to`)
   - the model picker (MS1), editable and locked
   - Continue on a resumable session
   - the rule that a secret-shaped message is refused, never sent
6. **Facts.**
   - List, add, edit, delete.
   - Confidence badge. `claim` and `evidence` required.
   - Warnings below `min_warn`, and the blocked state below `min_hard` (10).
   - Delete refused (409) when a tailored resume cites the fact.
7. **Resumes.**
   - Master `.docx` upload.
   - Tailored versions per job.
   - Bullet → fact citations, linking to Facts.
8. **Profile.** The single source of contact details: name, email, phone, links, gender,
   address, work history, education.
9. **Settings.**
   - Career brief: titles, locations, cap, threshold.
   - Models (MS1): scoring and apply.
   - `max_score_per_run`.
   - No contact fields, only a link to Profile (M6).
10. **Memory.**
    - Q&A rows, including keyed preferences versus literal answers and twin links.
    - Edit, delete, and the stale (volatile, 30-day) indicator.
11. **Logins.**
    - Domain and email list. Passwords are never shown.
    - Delete.
    - Add login (LG1), with a write-only password and domain validation errors.

## Phase 2: implementation

This starts only when all 11 screens are Approved.

- **For each screen:**
  1. Build its backend-dep slices first.
  2. Retrieve the approved Stitch design.
  3. Implement it in the existing React app, wired to the real APIs, keeping every
     business rule.
  4. Build all its states.
  5. Run `npx tsc -b`, plus `pytest` for backend slices.
  6. Compare against the Stitch design, fix discrepancies, and mark it Implemented.
- **No redesign during implementation.** If a technical limit forces a design change, stop
  and bring it back for approval.
- **Live-credit checks** (H4 notes and the apply model on a real session) still need
  explicit approval, as with the README's pending live checks.
- **Final integration pass:**
  - navigation and transitions
  - the application, chat and Facts workflows
  - the Profile/Settings split
  - all loading, empty and error states
  - narrow-width layouts
  - shared component consistency
  - the type check

## Approved screens

Each approved screen records its Stitch screen ID(s) and key decisions here.

Stitch project: `14960912770210209518`. Design system: `assets/4887706637794229790`.

### 1. Application Shell (approved 2026-09-14)

- **Screens:** Shell `1faa69250fb54fd483095f1877385017`, shared component sheet
  `8c3f224648274563afec62e50123bd4c`. Ignore the superseded draft `3854ea8c…`.
- **Sidebar:**
  - Workspace, Candidate and Agent groups.
  - The active item is indigo with a 2px left bar and a mono count.
  - Facts shows an amber dot while the fact count is below `min_warn`.
- **Agent status card in the sidebar footer.** It replaces `AgentStatusBar` and shows:
  - apply run state and mode, and the current job
  - pipeline state and last run time
  - an amber "N card(s) waiting · Answer →" row, linking to the newest open card
  - a "Submission off · Draft only" pill while `submission_implemented` is false
- **Top bar:** a breadcrumb that names only where you are (e.g. "Workspace ›
  Applications"), with page actions on the right.
- **No pagination.** `/api/applications` returns every row. Lists use filter tabs and
  scrolling, with no client-side pager.
- **Component sheet sample content is neutral.** It must never show controls for features
  that don't exist. Real submission is a code constant, not a UI toggle.
