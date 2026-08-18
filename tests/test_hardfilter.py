import pytest

from career_agent.config import CareerBrief
from career_agent.hardfilter import check
from career_agent.models import Job

BRIEF = CareerBrief(
    target_titles=["AI Engineer"],
    title_families=["ai engineer", "machine learning engineer"],
    search_locations=["Chennai"],
    locations=["Chennai", "Remote"],
    remote_ok=True,
    salary_floor_inr=1_200_000,
    excluded_companies=["BadCo"],
    staleness_days=30,
)


def _job(**kw):
    base = dict(source="ats", external_id="1", company="Acme",
                title="AI Engineer", location="Chennai")
    return Job(**{**base, **kw})


def test_survives_a_clean_job():
    assert check(_job(), BRIEF) is None


@pytest.mark.parametrize("kw,fragment", [
    (dict(location="Mumbai"), "location"),
    (dict(comp_max=600_000), "salary floor"),
    (dict(title="Chef"), "title family"),
    (dict(company="BadCo"), "excluded"),
    (dict(posted_at="2020-01-01"), "stale"),
])
def test_each_rule_rejects_with_its_reason(kw, fragment):
    reason = check(_job(**kw), BRIEF)
    assert reason is not None
    assert fragment in reason.lower()


def test_remote_job_passes_regardless_of_city():
    assert check(_job(location="Berlin", is_remote=True), BRIEF) is None


def test_unstated_compensation_does_not_reject():
    assert check(_job(comp_max=None), BRIEF) is None
