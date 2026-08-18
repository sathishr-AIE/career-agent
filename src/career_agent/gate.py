import json
import logging
import re
from typing import Awaitable, Callable

from career_agent.config import CareerBrief
from career_agent.models import Job, Verdict

log = logging.getLogger(__name__)

# Bump this in the same commit as any prompt edit. Bumping invalidates every
# stored assessment and forces a re-score, which is what makes a prompt change
# measurable against the golden set.
PROMPT_VERSION = "gate-v1"

MIN_FACTS_HARD = 10
MIN_FACTS_WARN = 20


class InsufficientFacts(RuntimeError):
    """The facts store is too thin to score credibility honestly."""


TEMPLATE = """You are scoring one job against a candidate's career brief.

CAREER BRIEF
Target titles: {titles}
Search locations: {locations}
Remote acceptable: {remote}
Salary floor (INR/year): {floor}
Non-negotiables: {non_negotiables}

VERIFIED FACTS (the only evidence you may credit for credibility)
{facts}

JOB
Company: {company}
Title: {title}
Location: {location}
Posted: {posted}
Description:
{description}

Score each dimension 0-100:
- role_fit: match to target titles, stack, domain, career direction
- credibility: whether the VERIFIED FACTS above support a strong application
  WITHOUT exaggeration. Credit nothing that is not listed there. If the facts
  do not support it, score low.
- opportunity: company reputation, role clarity, growth, freshness, warning signs
- application_quality: whether a complete, non-conflicting application can be built
- eligibility_soft: residual eligibility the mechanical filter could not decide,
  such as an unstated experience range

Then choose a verdict, applying these rules in order:
1. Any dimension below 40, or credibility below 60 -> "skip"
2. Weighted score at or above {threshold} -> "submit"
   (weights: role_fit .30, credibility .30, opportunity .20,
    application_quality .15, eligibility_soft .05)
3. Otherwise -> "hold"

Reply with a single JSON object and nothing else:
{{"role_fit": int, "credibility": int, "opportunity": int,
  "application_quality": int, "eligibility_soft": int,
  "verdict": "submit"|"hold"|"skip", "rationale": "one or two sentences"}}
"""


def build_prompt(job: Job, brief: CareerBrief, facts: list[str]) -> str:
    return TEMPLATE.format(
        titles=", ".join(brief.target_titles),
        locations=", ".join(brief.search_locations),
        remote=brief.remote_ok,
        floor=brief.salary_floor_inr or "not specified",
        non_negotiables="; ".join(brief.non_negotiables) or "none",
        facts="\n".join(f"- {f}" for f in facts),
        company=job.company,
        title=job.title,
        location=job.location or "not specified",
        posted=job.posted_at or "unknown",
        description=(job.description or "")[:6000],
        threshold=brief.gate_threshold,
    )


def parse_verdict(text: str) -> Verdict:
    """Tolerate prose around the JSON; reject anything that is not a Verdict."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model output")
    try:
        return Verdict(**json.loads(match.group(0)))
    except Exception as exc:
        raise ValueError(f"could not parse verdict: {exc}") from exc


async def score(job: Job, brief: CareerBrief, facts: list[str],
                ask: Callable[[str], Awaitable[str]]) -> Verdict:
    if len(facts) < MIN_FACTS_HARD:
        raise InsufficientFacts(
            f"{len(facts)} facts recorded, need at least {MIN_FACTS_HARD}. "
            "Credibility carries 30% weight and a floor of 60, so scoring now "
            "would skip everything for a reason that looks like the market.")
    if len(facts) < MIN_FACTS_WARN:
        log.warning("only %d facts; credibility scores are probably depressed",
                    len(facts))

    prompt = build_prompt(job, brief, facts)
    try:
        return parse_verdict(await ask(prompt))
    except ValueError:
        retry = prompt + ("\n\nYour previous reply was not valid JSON. "
                          "Reply with ONLY the JSON object.")
        return parse_verdict(await ask(retry))
