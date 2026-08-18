from career_agent import normalize
from career_agent.config import CareerBrief
from career_agent.models import Job


def check(job: Job, brief: CareerBrief) -> str | None:
    """Deterministic rejection. Returns None if the job survives, otherwise the
    reason. A failure here is final and never reaches the model, so a job under
    the salary floor cannot be rescued by an attractive tech stack."""

    if normalize.company(job.company) in {
            normalize.company(c) for c in brief.excluded_companies}:
        return f"excluded company: {job.company}"

    if not (job.is_remote and brief.remote_ok):
        accepted = {normalize.location(l) for l in brief.locations} - {""}
        if accepted and normalize.location(job.location) not in accepted:
            return f"location outside accepted set: {job.location}"

    if brief.salary_floor_inr and job.comp_max is not None:
        if job.comp_max < brief.salary_floor_inr:
            return f"below salary floor: {job.comp_max} < {brief.salary_floor_inr}"

    if brief.title_families:
        t = normalize.title(job.title)
        if not any(normalize.title(f) in t or t in normalize.title(f)
                   for f in brief.title_families):
            return f"title family mismatch: {job.title}"

    if normalize.is_stale(job, brief.staleness_days):
        return f"stale: posted {job.posted_at}"

    return None
