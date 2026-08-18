from career_agent.sources.apify import (LINKEDIN_ACTOR, NAUKRI_ACTOR,
                                        fetch_linkedin, fetch_naukri)


class FakeDataset:
    def __init__(self, items):
        self._items = items

    def iterate_items(self):
        return iter(self._items)


class FakeActor:
    def __init__(self, parent, actor_id):
        self.parent, self.actor_id = parent, actor_id

    def call(self, run_input):
        self.parent.calls.append((self.actor_id, run_input))
        return {"defaultDatasetId": "ds1"}


class FakeClient:
    def __init__(self, items=()):
        self.items, self.calls = list(items), []

    def actor(self, actor_id):
        return FakeActor(self, actor_id)

    def dataset(self, _):
        return FakeDataset(self.items)


def test_linkedin_pins_actor_and_passes_location():
    client = FakeClient([{"id": "99", "title": "AI Engineer",
                          "companyName": "Acme", "location": "Chennai",
                          "postedAt": "2026-08-01",
                          "descriptionText": "LLM work", "link": "https://x/1"}])
    jobs = fetch_linkedin("AI Engineer", "Chennai", False, 50, client)

    actor_id, run_input = client.calls[0]
    assert actor_id == LINKEDIN_ACTOR
    assert run_input["location"] == "Chennai"
    assert run_input["keyword"] == "AI Engineer"
    assert jobs[0].source == "linkedin"
    assert jobs[0].external_id == "99"


def test_naukri_pins_actor_and_parses_inr_band():
    client = FakeClient([{"jobId": "77", "title": "ML Engineer",
                          "companyName": "Globex", "location": "Chennai",
                          "salaryMin": 600000, "salaryMax": 1500000,
                          "postedDate": "2026-08-02",
                          "jobDescription": "ML pipelines",
                          "jobUrl": "https://n/77"}])
    jobs = fetch_naukri("ML Engineer", "Chennai", False, 50, client)

    actor_id, run_input = client.calls[0]
    assert actor_id == NAUKRI_ACTOR
    assert run_input["location"] == "Chennai"
    assert jobs[0].comp_min == 600000
    assert jobs[0].comp_max == 1500000


def test_remote_uses_workmode_not_a_location_string():
    client = FakeClient()
    fetch_naukri("AI Engineer", "Chennai", True, 50, client)
    _, run_input = client.calls[0]
    assert run_input["workMode"] == "remote"
    assert run_input["location"] == "Chennai"


def test_remote_flag_marks_returned_jobs_remote():
    client = FakeClient([{"jobId": "5", "title": "AI Engineer",
                          "companyName": "Acme", "location": "Chennai"}])
    assert fetch_naukri("AI Engineer", "Chennai", True, 50, client)[0].is_remote


def test_actor_failure_returns_empty_not_crash():
    class Boom(FakeClient):
        def actor(self, actor_id):
            raise RuntimeError("actor unavailable")

    assert fetch_linkedin("x", "Chennai", False, 5, Boom()) == []
