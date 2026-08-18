import logging
from typing import Any

from career_agent.models import Job

log = logging.getLogger(__name__)

# Pinned. Changing an Actor is a code change, not a runtime decision.
LINKEDIN_ACTOR = "practicaltools/linkedin-jobs"
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
    run_input = {
        "keyword": keyword,
        "location": location,
        "maxJobs": max_jobs,
        "fetchDescriptions": True,
    }
    if remote:
        run_input["workplaceType"] = "remote"

    items = _run(client, LINKEDIN_ACTOR, run_input)
    return [
        Job(source="linkedin",
            external_id=str(i.get("id") or i.get("jobId")),
            company=i.get("companyName") or "unknown",
            title=i.get("title") or "unknown",
            location=i.get("location"),
            is_remote=remote,
            posted_at=i.get("postedAt"),
            url=i.get("link") or i.get("jobUrl"),
            description=i.get("descriptionText"))
        for i in items if i.get("id") or i.get("jobId")
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
