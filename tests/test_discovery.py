from career_agent import discovery
from career_agent.config import Board, CareerBrief

BRIEF = CareerBrief(
    target_titles=["AI Engineer", "ML Engineer"],
    search_locations=["Chennai"],
    locations=["Chennai", "Remote"],
    remote_ok=True,
)


def test_queries_use_search_locations_from_the_brief():
    queries = discovery.plan_queries(BRIEF)
    assert {q.location for q in queries} == {"Chennai"}
    assert "Bangalore" not in {q.location for q in queries}


def test_one_query_per_title_per_location_per_source():
    brief = BRIEF.model_copy(update={"remote_ok": False})
    queries = discovery.plan_queries(brief)
    # 2 titles x 1 location x 2 apify sources
    assert len(queries) == 4
    assert {q.source for q in queries} == {"linkedin", "naukri"}


def test_remote_ok_adds_a_second_pass_per_title():
    queries = discovery.plan_queries(BRIEF)
    # 4 city queries + 4 remote queries
    assert len(queries) == 8
    assert sum(1 for q in queries if q.remote) == 4


def test_remote_pass_keeps_the_city_and_does_not_send_remote_as_location():
    remote_queries = [q for q in discovery.plan_queries(BRIEF) if q.remote]
    assert all(q.location == "Chennai" for q in remote_queries)


def test_multiple_cities_multiply_the_query_count():
    brief = BRIEF.model_copy(
        update={"search_locations": ["Chennai", "Bangalore"], "remote_ok": False})
    assert len(discovery.plan_queries(brief)) == 8


def test_run_discovery_calls_every_planned_query(monkeypatch):
    calls = []

    def fake_linkedin(keyword, location, remote, max_jobs, client):
        calls.append(("linkedin", keyword, location, remote))
        return []

    def fake_naukri(keyword, location, remote, max_jobs, client):
        calls.append(("naukri", keyword, location, remote))
        return []

    monkeypatch.setattr(discovery.apify, "fetch_linkedin", fake_linkedin)
    monkeypatch.setattr(discovery.apify, "fetch_naukri", fake_naukri)
    monkeypatch.setattr(discovery.ats, "fetch_greenhouse",
                        lambda board, client: [])

    boards = [Board(provider="greenhouse", token="acme", company="Acme")]
    discovery.run_discovery(BRIEF, boards, apify_client=None, http_client=None)

    assert len(calls) == 8
    assert all(location == "Chennai" for _, _, location, _ in calls)
