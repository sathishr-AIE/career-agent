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

Open [http://localhost:5173](http://localhost:5173) — this is the dashboard. It proxies
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
cp .env.example .env          # fill in CLAUDE_CODE_OAUTH_TOKEN and APIFY_TOKEN
```

`CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token`; it's required even for `serve` alone, since the dashboard's discovery run uses it. `APIFY_TOKEN` is only needed to actually discover jobs (Run Now / `career-agent run`).

Drafting or sending an application (the dashboard's Apply/Send buttons, or the apply
worker) also needs the `claude` CLI and `npx` on PATH — the apply agent spawns `claude -p`
with a Playwright MCP server attached — and a real Chrome install; see "Real application
submission (v3)" below. Discovery and scoring (`career-agent run`) don't need any of that.

Edit `career_brief.toml` (target roles, locations, salary floor, daily application cap, non-negotiables) and `ats_boards.toml` (company ATS boards to also search) before your first real run — both are read from the current directory (`backend/`).

Two commands, via the `career-agent` console script (`--help` for all flags), both run from `backend/`:

- **`career-agent run`** — one-shot: discovers jobs, hard-filters, and scores them against `career_brief.toml`. Writes to `data/career.db`. This is what a scheduled task would call daily (see `scripts/install-scheduler.ps1`).
- **`career-agent serve`** — starts the backend at [http://localhost:8000](http://localhost:8000). This serves two things at once: the original Jinja dashboard (being migrated page by page to the React frontend below — pages not yet migrated still live here), and the `/api/*` JSON API the React frontend runs on. Either way, this needs to be running before the frontend has anything to show:
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
   fill in your real name, email, and phone (this file is gitignored).
3. With Chrome fully closed, use manual mode's draft step (Apply, not Send) against a
   few real postings first — the first draft clones your existing Chrome profile
   (cookies, sessions) into an isolated worker profile, which needs Chrome not running.
   Drafting is safe with the flag off — it runs the real agent and shows you exactly
   what it would send, with zero real applications going out.
4. Only once you trust the drafts, flip `SUBMISSION_IMPLEMENTED = True`.

Questions the agent can't answer (no `qa_bank` entry, or a stale volatile
one) show up as "Answer needed" on the Applications page's status card —
answer once, and it's remembered for every future application via `qa_bank`.

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
│   │       ├── app.py         # Jinja routes (legacy pages, migrating out)
│   │       ├── api.py         # JSON /api/* routes for the React frontend
│   │       ├── context.py     # shared page-data builders (both frontends read these)
│   │       ├── actions.py     # shared mutations — Apply, Send, Save Settings, ...
│   │       └── templates/     # Jinja2 templates for pages not yet migrated
│   ├── tests/                 # pytest, including tests/golden/ (gate.py regression set)
│   └── scripts/                # scheduler install, auth spike
├── frontend/                  # Vite + React SPA dashboard (npm run dev → :5173)
└── docs/
    ├── product-vision.md      # full product vision (mirrored from Obsidian)
    └── superpowers/           # design specs and implementation plans per feature
```

Working notes live in the Obsidian vault under `Automations/job-applyier` — that vault is the source of truth for the vision; `docs/` mirrors it.
