import hashlib
import re
from datetime import date, datetime

from career_agent.models import Job

COMPANY_SUFFIXES = {"inc", "ltd", "limited", "pvt", "private", "llc", "gmbh",
                    "plc", "corp", "corporation", "co", "technologies",
                    "technology", "labs", "systems", "solutions", "software",
                    "india"}

TITLE_EXPANSIONS = {"sr": "senior", "jr": "junior", "eng": "engineer",
                    "engg": "engineer", "dev": "developer", "mgr": "manager"}

TITLE_NOISE = {"software", "staff"}

LOCATION_ALIASES = {"bengaluru": "bangalore", "bombay": "mumbai",
                    "madras": "chennai", "gurugram": "gurgaon",
                    "new delhi": "delhi"}

COUNTRY_SUFFIXES = {"india", "in", "usa", "us", "uk"}


def _words(value: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", value.lower()) if w]


def company(value: str) -> str:
    return "".join(w for w in _words(value) if w not in COMPANY_SUFFIXES)


def title(value: str) -> str:
    """Sort tokens so word order cannot break a match, and drop qualifiers
    that sources add inconsistently."""
    value = re.sub(r"\(.*?\)", " ", value)
    out = []
    for w in _words(value):
        w = TITLE_EXPANSIONS.get(w, w)
        if w not in TITLE_NOISE:
            out.append(w)
    return "".join(sorted(set(out)))


def location(value: str | None) -> str:
    if not value:
        return ""
    parts = [p.strip().lower() for p in value.split(",")]
    parts = [p for p in parts if p not in COUNTRY_SUFFIXES]
    head = parts[0] if parts else ""
    head = LOCATION_ALIASES.get(head, head)
    return re.sub(r"[^a-z0-9]+", "", head)


def fingerprint(job: Job) -> str:
    """Identifies the same role across sources. Posted date is deliberately
    excluded: the same role carries different dates on different boards."""
    raw = "|".join((company(job.company), title(job.title), location(job.location)))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def is_stale(job: Job, max_age_days: int) -> bool:
    if not job.posted_at:
        return False
    try:
        posted = datetime.fromisoformat(
            job.posted_at.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    return (date.today() - posted).days > max_age_days
