from datetime import date

from career_agent import normalize
from career_agent.models import Job


def _job(**kw):
    base = dict(source="ats", external_id="1", company="Acme", title="AI Engineer")
    return Job(**{**base, **kw})


def test_the_collision_the_spec_names():
    a = _job(company="Acme Inc", title="Sr. Backend Engineer", location="Chennai")
    b = _job(company="Acme", title="Senior Software Engineer, Backend",
             location="chennai, India")
    assert normalize.fingerprint(a) == normalize.fingerprint(b)


def test_company_suffixes_stripped():
    assert normalize.company("Acme Technologies Pvt Ltd") == normalize.company("Acme")


def test_bengaluru_aliases_to_bangalore():
    assert normalize.location("Bengaluru") == normalize.location("Bangalore, India")


def test_different_company_does_not_collide():
    assert normalize.fingerprint(_job(company="Acme")) != \
           normalize.fingerprint(_job(company="Globex"))


def test_stale_when_older_than_window():
    assert normalize.is_stale(_job(posted_at="2020-01-01"), 30) is True


def test_fresh_when_recent():
    assert normalize.is_stale(_job(posted_at=date.today().isoformat()), 30) is False


def test_missing_posted_at_is_not_stale():
    assert normalize.is_stale(_job(posted_at=None), 30) is False
