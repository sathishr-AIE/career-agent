import logging
from typing import Any

from career_agent.models import Job

log = logging.getLogger(__name__)

# Pinned. Changing an Actor is a code change, not a runtime decision.
LINKEDIN_ACTOR = "valig/linkedin-jobs-scraper"
NAUKRI_ACTOR = "automation-lab/naukri-scraper"


def _run(client: Any, actor_id: str, run_input: dict) -> list[dict]:
    try:
        run = client.actor(actor_id).call(run_input=run_input)
        # apify-client >= 3 returns a typed Run object, not a dict. Subscripting
        # it raises TypeError, which the except below would report as an Actor
        # failure even though the Actor succeeded.
        dataset_id = run.default_dataset_id
        return list(client.dataset(dataset_id).iterate_items())
    except Exception as exc:  # an Actor outage must not kill the run
        log.warning("apify actor %s failed: %s", actor_id, exc)
        return []


def fetch_linkedin(keyword: str, location: str, remote: bool,
                   max_jobs: int, client: Any) -> list[Job]:
    # Key names verified against the Actor's published input schema, not assumed.
    run_input = {
        "title": keyword,
        "location": location,
        "limit": max_jobs,
    }
    if remote:
        # LinkedIn's f_WT workplace code: 1 on-site, 2 remote, 3 hybrid.
        run_input["remote"] = ["2"]

    items = _run(client, LINKEDIN_ACTOR, run_input)
    return [
        Job(source="linkedin",
            external_id=str(i["id"]),
            company=i.get("companyName") or "unknown",
            title=i.get("title") or "unknown",
            location=i.get("location"),
            is_remote=remote,
            posted_at=i.get("postedDate"),
            url=i.get("url"),
            description=i.get("description"))
        for i in items if i.get("id")
    ]


def fetch_naukri(keyword: str, location: str, remote: bool,
                 max_jobs: int, client: Any) -> list[Job]:
    run_input = {
        "keyword": keyword,
        "location": location,
        "maxJobs": max_jobs,
        "sortBy": "date",
    }
    if remote:
        run_input["workMode"] = "remote"

    items = _run(client, NAUKRI_ACTOR, run_input)
    return [
        Job(source="naukri",
            external_id=str(i["jobId"]),
            company=i.get("companyName") or "unknown",
            title=i.get("title") or "unknown",
            location=i.get("location"),
            is_remote=remote,
            comp_min=i.get("salaryMin"),
            comp_max=i.get("salaryMax"),
            posted_at=i.get("postedDate"),
            url=i.get("jobUrl"),
            description=i.get("jobDescription"))
        for i in items if i.get("jobId")
    ]
