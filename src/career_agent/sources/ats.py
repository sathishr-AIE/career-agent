import logging

import httpx

from career_agent.config import Board
from career_agent.models import Job

log = logging.getLogger(__name__)

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def fetch_greenhouse(board: Board, client: httpx.Client) -> list[Job]:
    """Public Greenhouse board feed. No auth, no login, no account risk."""
    try:
        resp = client.get(GREENHOUSE.format(token=board.token), timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("greenhouse fetch failed for %s: %s", board.token, exc)
        return []

    jobs = []
    for item in resp.json().get("jobs", []):
        loc = (item.get("location") or {}).get("name")
        jobs.append(Job(
            source="ats",
            external_id=str(item["id"]),
            company=board.company,
            title=item["title"],
            location=loc,
            is_remote=bool(loc and "remote" in loc.lower()),
            posted_at=item.get("updated_at"),
            url=item.get("absolute_url"),
            description=item.get("content"),
        ))
    return jobs
