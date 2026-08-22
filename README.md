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

**v1 is implemented and runnable** — discovery, the quality gate, a live dashboard, and outcome tracking. See [`docs/product-vision.md`](docs/product-vision.md) for the full vision this is building toward.

> **It does not submit applications.** Applying is manual, by design: auto-submission is
> deferred to v3, gated on per-role tailoring landing first and on outcome data showing
> tailored applications actually convert. The dashboard finds and scores roles and tracks
> what you did about them — you apply on the site yourself. See
> [the v1 design's phase table](docs/superpowers/specs/2026-08-18-career-agent-v1-design.md)
> for the reasoning: *"An application is a consumable resource. There is roughly one useful
> attempt per company per role."* Naukri submission is not planned at any version — it is a
> discovery source only.

## Running it

**Prerequisites:** Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv
uv pip install -e ".[dev]"
playwright install chromium   # needed to actually submit applications
cp .env.example .env          # fill in CLAUDE_CODE_OAUTH_TOKEN and APIFY_TOKEN
```

`CLAUDE_CODE_OAUTH_TOKEN` comes from `claude setup-token`; it's required even for `serve` alone, since the dashboard's discovery run uses it. `APIFY_TOKEN` is only needed to actually discover jobs (Run Now / `career-agent run`).

Edit `career_brief.toml` (target roles, locations, salary floor, daily application cap, non-negotiables) and `ats_boards.toml` (company ATS boards to also search) before your first real run — both are read from the current directory.

Two commands, via the `career-agent` console script (`--help` for all flags):

- **`career-agent run`** — one-shot: discovers jobs, hard-filters, and scores them against `career_brief.toml`. Writes to `data/career.db`. This is what a scheduled task would call daily (see `scripts/install-scheduler.ps1`).
- **`career-agent serve`** — starts the dashboard at [http://localhost:8000](http://localhost:8000):
  - **`/`** — Overview: KPIs, the discovery→apply funnel, source performance, score distribution, recent discoveries/outcomes, and a **Run Now** button that triggers the same discover+score pipeline as `career-agent run`, in the background. Run Now opens a live monitor — current stage, live counts (found/passed/scored/shortlisted), and a streaming activity feed — that you can dismiss to keep working while the run continues.
  - **`/applications`** — the live application queue: Start/Pause/Resume/Stop a worker that walks scored jobs, applying automatically (**Auto** mode) or pausing for your review before each send (**Manual** mode, the default).
  - **`/resumes`** — shows the master template's status and every tailored resume generated so far, with the verified fact each bullet was built from.

Per-role tailoring (the first Apply click on a job) needs a master resume at `resume/master.docx`. It must contain two literal marker paragraphs: `<<SUMMARY>>` (a paragraph whose text gets replaced with the tailored summary) and `<<PROJECT_BULLET>>` (a bullet-styled paragraph cloned once per selected fact, then removed). Everything else in the template — header, contact info, education, layout — is left untouched.

### Roadmap

This is the roadmap as built, from
[the v1 design](docs/superpowers/specs/2026-08-18-career-agent-v1-design.md). Each phase
has an explicit gate that must be met before the next one starts — the gates are the
point, not decoration.

| Phase | Delivers | Gate to advance |
| --- | --- | --- |
| **v1** *(shipped)* | Discovery, hard filter, scored gate, dashboard, manual apply, outcome tracking | The gate agrees with your judgment, and callback data exists |
| **v2** | Per-role tailoring from the facts store, resume rendering | Nothing claimed that you cannot defend |
| **v3** | Auto-submission within limits | Tailoring proven, and outcomes show tailored applications convert |

Deferred and unscheduled: recruiter-message handling and calendar (v2); **Naukri
submission — no Actor exists and no connector is planned, so Naukri stays discovery-only
at every version**; warm-path and referral detection (unscheduled).

An earlier draft of this README carried a different, pre-implementation roadmap that
listed autonomous submission under v1. That numbering was superseded by the design spec
above and was wrong about what v1 does.

## Repository layout

```
career-agent/
├── README.md
└── docs/
    └── product-vision.md    # full product vision (mirrored from Obsidian)
```

Working notes live in the Obsidian vault under `Automations/job-applyier` — that vault is the source of truth for the vision; `docs/` mirrors it.
