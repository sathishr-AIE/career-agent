import httpx

from career_agent.config import Board
from career_agent.sources.ats import fetch_greenhouse

BOARD = Board(provider="greenhouse", token="acme", company="Acme")

SAMPLE = {"jobs": [{
    "id": 4001,
    "title": "AI Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4001",
    "updated_at": "2026-08-01T10:00:00Z",
    "location": {"name": "Chennai, India"},
    "content": "Build LLM pipelines.",
}]}


def test_maps_fields_and_uses_company_name_not_token():
    def handler(request):
        assert "acme" in str(request.url)
        return httpx.Response(200, json=SAMPLE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = fetch_greenhouse(BOARD, client)

    assert len(jobs) == 1
    assert jobs[0].source == "ats"
    assert jobs[0].external_id == "4001"
    assert jobs[0].company == "Acme"
    assert jobs[0].location == "Chennai, India"
    assert "LLM" in jobs[0].description


def test_detects_remote_from_location_text():
    payload = {"jobs": [{**SAMPLE["jobs"][0],
                         "location": {"name": "Remote - India"}}]}
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    assert fetch_greenhouse(BOARD, client)[0].is_remote is True


def test_http_error_returns_empty_not_crash():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert fetch_greenhouse(BOARD, client) == []
