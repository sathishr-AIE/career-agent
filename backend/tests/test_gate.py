import pytest

from career_agent.config import CareerBrief
from career_agent.gate import InsufficientFacts, parse_verdict, score
from career_agent.models import Job

BRIEF = CareerBrief(target_titles=["AI Engineer"], search_locations=["Chennai"],
                    gate_threshold=72)
JOB = Job(source="ats", external_id="1", company="Acme", title="AI Engineer")
FACTS = [f"fact {i}" for i in range(20)]

GOOD = """Assessment follows.
```json
{"role_fit": 85, "credibility": 80, "opportunity": 75,
 "application_quality": 88, "eligibility_soft": 70,
 "verdict": "submit", "rationale": "Strong LLM and Python match."}
```"""


def test_parses_json_out_of_prose():
    v = parse_verdict(GOOD)
    assert v.verdict == "submit"
    assert v.role_fit == 85


def test_rejects_unparseable():
    with pytest.raises(ValueError):
        parse_verdict("I could not decide.")


def test_rejects_out_of_range_score():
    with pytest.raises(ValueError):
        parse_verdict('{"role_fit": 900, "credibility": 1, "opportunity": 1,'
                      ' "application_quality": 1, "eligibility_soft": 1,'
                      ' "verdict": "skip", "rationale": "x"}')


def test_weighted_score_uses_the_specified_weights():
    v = parse_verdict(GOOD)
    expected = (85 * .30) + (80 * .30) + (75 * .20) + (88 * .15) + (70 * .05)
    assert v.weighted == round(expected, 2)


async def test_raises_below_ten_facts():
    async def ask(_):
        raise AssertionError("must not call the model")

    with pytest.raises(InsufficientFacts):
        await score(JOB, BRIEF, ["one", "two"], ask)


async def test_retries_once_on_bad_output():
    calls = []

    async def ask(prompt):
        calls.append(prompt)
        return "nonsense" if len(calls) == 1 else GOOD

    v = await score(JOB, BRIEF, FACTS, ask)
    assert v.verdict == "submit"
    assert len(calls) == 2


async def test_raises_after_second_failure():
    async def ask(_):
        return "still nonsense"

    with pytest.raises(ValueError):
        await score(JOB, BRIEF, FACTS, ask)
