import json

import pytest

from career_agent.config import CareerBrief
from career_agent.gate import InsufficientFacts
from career_agent.models import Job
from career_agent.tailor import build_prompt, parse_tailor_result, tailor

BRIEF = CareerBrief(target_titles=["AI Engineer"], search_locations=["Chennai"])
JOB = Job(source="ats", external_id="1", company="Acme", title="AI Engineer",
          description="Build LLM-backed features in Python.")
FACTS = [(i, f"claim {i} (evidence: evidence {i})") for i in range(1, 11)]

GOOD = json.dumps({"summary": "Tailored summary.", "bullets": [
    {"text": "Built an LLM feature", "fact_ids": [1, 2]}]})


def test_build_prompt_includes_fact_ids_and_job_description():
    prompt = build_prompt(JOB, BRIEF, FACTS)
    assert "[1] claim 1" in prompt
    assert "Build LLM-backed features" in prompt


def test_parses_json_out_of_prose():
    r = parse_tailor_result(f"Here it is.\n```json\n{GOOD}\n```")
    assert r.summary == "Tailored summary."
    assert r.bullets[0].fact_ids == [1, 2]


def test_rejects_unparseable():
    with pytest.raises(ValueError):
        parse_tailor_result("I could not decide.")


def test_rejects_zero_bullets():
    with pytest.raises(ValueError):
        parse_tailor_result(json.dumps({"summary": "S", "bullets": []}))


def test_rejects_a_bullet_with_no_fact_ids():
    with pytest.raises(ValueError):
        parse_tailor_result(json.dumps(
            {"summary": "S", "bullets": [{"text": "x", "fact_ids": []}]}))


async def test_raises_below_ten_facts():
    async def ask(_):
        raise AssertionError("must not call the model")

    with pytest.raises(InsufficientFacts):
        await tailor(JOB, BRIEF, FACTS[:2], ask)


async def test_retries_once_on_bad_output():
    calls = []

    async def ask(prompt):
        calls.append(prompt)
        return "nonsense" if len(calls) == 1 else GOOD

    r = await tailor(JOB, BRIEF, FACTS, ask)
    assert r.summary == "Tailored summary."
    assert len(calls) == 2


async def test_retries_once_on_an_unknown_fact_id():
    calls = []
    bad = json.dumps({"summary": "S", "bullets": [
        {"text": "x", "fact_ids": [999]}]})

    async def ask(prompt):
        calls.append(prompt)
        return bad if len(calls) == 1 else GOOD

    r = await tailor(JOB, BRIEF, FACTS, ask)
    assert r.summary == "Tailored summary."
    assert len(calls) == 2


async def test_raises_after_second_failure():
    async def ask(_):
        return "still nonsense"

    with pytest.raises(ValueError):
        await tailor(JOB, BRIEF, FACTS, ask)
