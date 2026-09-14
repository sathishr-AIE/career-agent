# Career Agent

**A quality-gated AI career agent.** Set your career direction once; your agent works every day to move you toward it.

Career Agent is an autonomous job-search partner for ambitious professionals who want a serious job search without turning it into a second full-time job. It continuously finds relevant roles, decides whether each is genuinely worth pursuing, creates a truthful role-specific application, and submits it. Then it manages everything that follows: tracking outcomes, organizing interviews, following up, and helping you improve over time.

The goal is not "apply everywhere." It is: **never miss a strong opportunity, never send a generic application, and never lose track of what happens next.**

## What it is — and what it isn't

| It is | It isn't |
| --- | --- |
| A personal, always-on career agent | Another job board |
| A selective, explainable decision-maker | A spammy auto-apply bot |
| A truthful narrative layer over your real experience | A resume generator that invents experience |

## Who it's for

The launch persona is an early-career AI engineer (roughly 1–3 years) targeting AI engineer, ML engineer, applied AI, LLM, or adjacent software roles — someone with a credible but still developing portfolio who wants strong opportunities rather than indiscriminate volume, and who is comfortable delegating applications provided the agent respects clear guardrails.

These roles need nuanced matching across skills, projects, tools, domain exposure, location, and seniority — exactly where generic auto-apply products feel weak.

## The Quality Gate

Before anything is submitted, the agent scores five dimensions:

1. **Eligibility** — location, work authorization, remote/hybrid preference, compensation floor, experience range, job type.
2. **Role fit** — match to target titles, AI/ML stack, domain interests, and career direction.
3. **Credibility** — whether your real resume, projects, and achievements can support a compelling application *without exaggeration*.
4. **Opportunity quality** — company reputation, role clarity, growth potential, listing freshness, warning signs.
5. **Application quality** — tailored resume, relevant project emphasis, required questions completed, nothing conflicting or incomplete.

Three outcomes, always:

- **Submit** — passes the agreed threshold.
- **Hold for review** — borderline; surfaced to you with the rationale.
- **Skip** — poor fit; quietly skipped *with a recorded reason*.

Every submission carries an explainable decision record: why it qualified, what was tailored, and what was sent.

## How it works

1. **Define your career target** — target roles, industries, locations, remote/hybrid preference, salary expectations, non-negotiables, skills and projects to emphasize, companies to avoid, and a daily/weekly application limit. This becomes a living *career brief*, not a static search filter.
2. **Build your professional narrative** — your resume is understood as a set of credible career stories: technical strengths, projects with measurable outcomes, domains you care about, achievements you can defend in an interview, and growth areas positioned honestly.
3. **Search continuously, decide selectively** — daily scans, deduplication, stale-listing removal, fit scoring, and prioritization of roles where you are both qualified and motivated.
4. **Tailor and apply** — a role-specific version of your resume and application narrative that highlights the most relevant projects, tools, and outcomes, without inventing experience. Submitted automatically and recorded in your timeline.
5. **Manage the response loop** — recruiter messages are classified and acted on: flag interview requests, create calendar events and reminders, surface prep materials, draft replies, chase follow-ups, update application status.
6. **Learn from outcomes** — which titles, companies, and skills generate responses; which narratives perform best; where your profile is filtered out; which gaps are worth closing with a project or skill plan.

## Core capabilities

- Personalized opportunity discovery and matching
- Autonomous, quality-gated application submission
- Truthful per-role resume and application tailoring
- A live application pipeline with status, notes, and history
- Recruiter-message detection and follow-up management
- Interview scheduling, calendar events, reminders, and prep prompts
- Daily digest and weekly job-search performance review
- Adjustable autonomy: criteria, quality threshold, application limits, exclusions, pause controls
- Career insight layer that turns outcomes into practical next actions

## Status

**v1, v2, and v3 are all implemented** — discovery, the quality gate, a live dashboard, outcome tracking, per-role resume tailoring, and a real, agentic apply engine (one Claude Code session per job, driving a real Chrome). See [`docs/product-vision.md`](docs/product-vision.md) for the full vision this is building toward.

**The portal is chat-first.** It opens on a Home chat, where you can say things like "find jobs", "what's my queue?" or "apply to #1639". Anything that spends credits or applies shows an Approve card first. Each job has its own chat. There the agent narrates its run, asks questions it can't answer as cards (answer once, and it remembers), shows a review card with every field before it would click Submit, and offers **Continue** after an interruption. The earlier pages (queue, résumés, settings) and the Memory, Logins and Profile panels open as drawers beside the chat. See [the chat-first spec](docs/superpowers/specs/2026-09-13-chat-first-agent-design.md).

> **It does not submit applications yet.** The apply agent is built, but
> `SUBMISSION_IMPLEMENTED` in `career_agent/apply/ats.py` ships `False` — nothing sends
> for real until that's flipped by hand, after manually verifying it against a
> real live posting (see "Real application submission (v3)" below). This is a deliberate
> departure from the phase gate in the table below, which called for outcome data proving
> tailored applications convert before auto-submission was even built; that evidence gate
> was superseded by building the agent now and keeping the manual flag as the actual
> trust boundary. The flag gates every source alike — there is no more per-source
> refusal. Naukri submission is not planned at any version — it is a discovery source
> only.

## Running it

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/), and Node.js 18+ (for the frontend).

The backend (Python package, config, data) lives in `backend/`; the dashboard's frontend
is a separate app in `frontend/`. Run each from its own directory, in two terminals.

### Starting the app (day-to-day)

Once the one-time setup below has been done, this is all firing it up takes — two terminals, both left running:

**Terminal 1 — backend:**

```powershell
cd D:\Work-space\Automations\career-agent\backend
uv run career-agent serve
```

`uv run` finds and uses `backend\.venv` automatically, so there's nothing to activate.
Serves the API and legacy dashboard at [http://localhost:8000](http://localhost:8000).

**Terminal 2 — frontend:**

```powershell
cd D:\Work-space\Automations\career-agent\frontend
npm run dev
```

Open [http://localhost:5173](http://localhost:5173) — this is the chat-first portal. It proxies
`/api/*` to the backend, so terminal 1 has to be running first.

> **If `career-agent` or `python -m career_agent...` says "not found" / "No module named
> career_agent":** you're in a different Python environment than `backend\.venv` — a
> global uv Python, a conda env, an old venv from before the repo was split, etc. Either
> `cd backend; .\.venv\Scripts\Activate.ps1` first, or just use `uv run career-agent ...`
> from `backend/` as above, which sidesteps activation entirely.

### First-time setup

**Backend:**

```bash
cd backend
uv venv
uv pip install -e ".[dev]"
cp .env.example .env          # fill in CLAUDE_CODE_OAUTH_TOKEN, APIFY_TOKEN and CREDENTIAL_KEY
```

`CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token`; it's required even for `serve` alone, since the dashboard's discovery run uses it. `APIFY_TOKEN` is only needed to actually discover jobs (Run Now / `career-agent run`).

`CREDENTIAL_KEY` is the Fernet key that encrypts the site logins the apply agent creates or uses (the Logins panel). Generate one and paste it into `.env`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep it safe. If you lose or change it, every stored login becomes unreadable and has to be deleted and created again. Nothing else needs the key, and a missing key fails only when a login is saved or used.

Drafting or sending an application (the dashboard's Apply/Send buttons, or the apply
worker) also needs the `claude` CLI and `npx` on PATH — the apply agent spawns `claude -p`
with a Playwright MCP server attached — and a real Chrome install; see "Real application
submission (v3)" below. Discovery and scoring (`career-agent run`) don't need any of that.

Edit `career_brief.toml` (target roles, locations, salary floor, daily application cap, non-negotiables) and `ats_boards.toml` (company ATS boards to also search) before your first real run — both are read from the current directory (`backend/`).

Two commands, via the `career-agent` console script (`--help` for all flags), both run from `backend/`:

- **`career-agent run`** — one-shot: discovers jobs, hard-filters, and scores them against `career_brief.toml`. Writes to `data/career.db`. This is what a scheduled task would call daily (see `scripts/install-scheduler.ps1`).
- **`career-agent serve`** — starts the backend at [http://localhost:8000](http://localhost:8000). This serves two things at once: the `/api/*` JSON API the React portal runs on, and the original Jinja dashboard. Every Jinja page now also exists in the React portal (as a route and as a drawer beside the chat), but the Jinja pages still work. Either way, this needs to be running before the frontend has anything to show:
  - **`/`** — Overview: KPIs, the discovery→apply funnel, source performance, score distribution, recent discoveries/outcomes, and a **Run Now** button that triggers the same discover+score pipeline as `career-agent run`, in the background. Run Now opens a live monitor — current stage, live counts (found/passed/scored/shortlisted), and a streaming activity feed — that you can dismiss to keep working while the run continues.
  - **`/applications`** — the live application queue: Start/Pause/Resume/Stop a worker that walks scored jobs, applying automatically (**Auto** mode) or pausing for your review before each send (**Manual** mode, the default).
  - **`/resumes`** — shows the master template's status and every tailored resume generated so far, with the verified fact each bullet was built from.

Per-role tailoring (the first Apply click on a job) needs a master resume at `backend/resume/master.docx`. It must contain two literal marker paragraphs: `<<SUMMARY>>` (a paragraph whose text gets replaced with the tailored summary) and `<<PROJECT_BULLET>>` (a bullet-styled paragraph cloned once per selected fact, then removed). Everything else in the template — header, contact info, education, layout — is left untouched. The Resumes page also lets you upload one directly and have the markers inserted automatically.

**Frontend:**

```bash
cd frontend
npm install
```

Open [http://localhost:5173](http://localhost:5173). It proxies `/api/*` and file downloads to the backend on `:8000`, so nothing needs configuring beyond having that backend up.

## Real application submission (v3)

`career_agent/apply/ats.py` drives a real agentic apply engine — one Claude Code session
per job, filling the form in a real Chrome over CDP — but `SUBMISSION_IMPLEMENTED` ships
`False` — nothing sends for real until you flip that constant by hand, after you've
verified it against real postings.

Setup:

1. Have the `claude` CLI and `npx` on PATH, and a real Chrome install (the path
   auto-detects; override with `CHROME_PATH` in `.env` if needed).
2. Copy `candidate_profile.toml.example` to `candidate_profile.toml` and
   fill in your real name, email, and phone (this file is gitignored). The optional
   `gender` (default `"decline"`), `[address]`, `[[work_history]]` and `[[education]]`
   sections let the agent fill those parts of a form instead of asking; you can also
   edit them in the Profile panel.
3. Set `CREDENTIAL_KEY` in `.env` (see First-time setup). The agent needs it whenever a
   site requires an account.
4. With Chrome fully closed, run a few manual-mode applications against real postings first — the first draft clones your existing Chrome profile
   (cookies, sessions) into an isolated worker profile, which needs Chrome not running.
   With the flag off this is safe: the real agent fills the form and shows the review
   card with exactly what it would send, then stops at a draft. No real application goes
   out.
5. Only once you trust the drafts, flip `SUBMISSION_IMPLEMENTED = True`.

Questions the agent can't answer from your profile or memory show up as cards in the
job's chat. The session waits while you answer, and the answer reaches that same
session. A question asking for a password, one-time code or other secret is never shown
as a card: the agent is told "none", and the chat says so. A finished draft doesn't hold
up the queue, since the review card in the chat was the review. Answers are remembered for future applications; review or edit them in the
Memory panel. A site that needs an account shows an Approve card. On approval the
backend (never the agent) generates a password, fills it into the real page, submits the
form, and saves the login encrypted. The agent is told only that the form was submitted.

**Existing Chrome profile clones.** New clones of your Chrome profile skip Chrome's saved
passwords (`Login Data`). A `data/chrome-profile` cloned before this change still has
them. Delete that folder, with Chrome closed, so the next run clones it again without
them.

### Safety

The agent never holds a site password. The Playwright MCP server version is pinned, and
its evaluate, network and screenshot tools are disabled. Some risks remain that can't be
closed from outside the page, and have been accepted:

- A site that re-renders a failed sign-in with the password still in the field after the
  backend clears it, or a show-password toggle the agent clicks against its rules, could
  put the password in front of the agent.
- A page on the approved domain that is itself malicious, or a site that logs
  credentials to its own console, is outside what the backend can check.
- A sign-up the site rejects after submit still leaves a saved login. Delete it in the
  Logins panel.
- A sign-in page that advances without changing its URL or removing the form is refused
  as not submitted (it fails closed).
- Refusing secret-shaped questions and review-card edits relies on a word list, which is
  only a backstop. The real defence is you reading each card before you answer it.
- Operator hooks still load in the agent's session, Playwright can navigate `file://`,
  and drafts can upload the résumé and click Save on the employer's site (see
  [LLD §6](docs/lld-apply-button-v2.md)).

### Pending live checks

These have only been tested against stubs. Each needs a real run, which spends credits and
drives a real Chrome, before it is trusted:

- a real `--resume` of an interrupted session, not just the fresh-session fallback
- a real backend fill, submit and clear on a sign-in or sign-up page
- the Home chat's "apply to #id" flow, from Approve card to a running job chat
- an end-to-end draft through the chat: questions, review card, `DRAFT_READY`

### Roadmap

This is the roadmap as built, from
[the v1 design](docs/superpowers/specs/2026-08-18-career-agent-v1-design.md). Each phase
has an explicit gate that must be met before the next one starts — the gates are the
point, not decoration.

| Phase | Delivers | Gate to advance |
| --- | --- | --- |
| **v1** *(shipped)* | Discovery, hard filter, scored gate, dashboard, manual apply, outcome tracking | The gate agrees with your judgment, and callback data exists |
| **v2** *(shipped)* | Per-role tailoring from the facts store, resume rendering | Nothing claimed that you cannot defend |
| **v3** *(shipped, gated)* | Real auto-submission via an agentic apply engine (all sources) | Tailoring proven, and outcomes show tailored applications convert |

v3's code shipped ahead of its own gate above: the apply engine was built and
tested before outcome data existed to prove tailored applications convert.
`SUBMISSION_IMPLEMENTED` is the actual gate now — a manual switch flipped only after
verifying it against a real posting, not the outcome-evidence condition
originally specified for this phase.

Deferred and unscheduled: recruiter-message handling and calendar; warm-path and
referral detection; **Naukri submission — no Actor exists and no connector is planned,
so Naukri stays discovery-only at every version**.

An earlier draft of this README carried a different, pre-implementation roadmap that
listed autonomous submission under v1. That numbering was superseded by the design spec
above and was wrong about what v1 does.

## Repository layout

```
career-agent/
├── CLAUDE.md                  # guidance for Claude Code working in this repo
├── README.md
├── backend/
│   ├── career_brief.toml      # your search/apply preferences (version-controlled)
│   ├── ats_boards.toml        # company ATS boards to scrape directly
│   ├── candidate_profile.toml.example  # copy to candidate_profile.toml (gitignored PII)
│   ├── src/career_agent/      # discovery, gate, tailor, apply/ats.py, web/ dashboard + API
│   │   └── web/
│   │       ├── app.py         # Jinja routes (legacy pages) + lifespan (startup sweep, worker)
│   │       ├── api.py         # JSON /api/* routes for the React frontend
│   │       ├── api_chat.py    # /api/chat/* — conversations, messages, card answers, Continue
│   │       ├── intent.py      # Home chat command router
│   │       ├── context.py     # shared page-data builders (both frontends read these)
│   │       ├── actions.py     # shared mutations — Apply, Send, Save Settings, ...
│   │       └── templates/     # Jinja2 templates for the legacy pages
│   ├── tests/                 # pytest, including tests/golden/ (gate.py regression set)
│   └── scripts/                # scheduler install, auth spike
├── frontend/                  # Vite + React chat-first portal (npm run dev → :5173)
└── docs/
    ├── product-vision.md      # full product vision (mirrored from Obsidian)
    └── superpowers/           # design specs and implementation plans per feature
```

Working notes live in the Obsidian vault under `Automations/job-applyier` — that vault is the source of truth for the vision; `docs/` mirrors it.
