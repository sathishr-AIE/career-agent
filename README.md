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

**Early / pre-implementation.** The product vision is defined; the stack and architecture are not yet chosen. See [`docs/product-vision.md`](docs/product-vision.md) for the full vision.

### Roadmap

| Stage | Scope |
| --- | --- |
| **v0** | Career brief + professional narrative capture; job discovery across selected sources; fit scoring and the quality gate — report only, no submissions. |
| **v1** | Per-role resume/application tailoring and autonomous submission within limits; application pipeline and decision records. |
| **v2** | Response loop: recruiter-message classification, interview scheduling, reminders, follow-ups. |
| **v3** | Insight layer: outcome analysis, narrative performance, gap detection, weekly review. |

## Repository layout

```
career-agent/
├── README.md
└── docs/
    └── product-vision.md    # full product vision (mirrored from Obsidian)
```

Working notes live in the Obsidian vault under `Automations/job-applyier` — that vault is the source of truth for the vision; `docs/` mirrors it.
