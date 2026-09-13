import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_agent.config import (CandidateProfile, WorkEntry,
                                 load_candidate_profile,
                                 save_candidate_profile)
from career_agent.web import api_profile
from career_agent.web import app as web


@pytest.fixture
def profile_path(tmp_path):
    return tmp_path / "candidate_profile.toml"


@pytest.fixture
def client(profile_path, monkeypatch):
    monkeypatch.setattr(web, "CANDIDATE_PROFILE_PATH", profile_path)
    app = FastAPI()
    app.include_router(api_profile.router)
    return TestClient(app)


def test_get_missing_file_returns_defaults(client):
    r = client.get("/api/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is False
    p = body["profile"]
    assert p["candidate_name"] == "" and p["candidate_email"] == "" and p["candidate_phone"] == ""
    assert p["gender"] == "decline"
    assert p["work_history"] == [] and p["education"] == []
    assert p["address"]["line1"] == ""


def test_get_existing_file_returns_work_history(client, profile_path):
    profile = CandidateProfile(
        candidate_name="Jane Doe", candidate_email="jane@example.com",
        candidate_phone="+91-1",
        work_history=[WorkEntry(company="Acme", title="Engineer", current=True)])
    save_candidate_profile(profile_path, profile)

    r = client.get("/api/profile")
    assert r.status_code == 200
    body = r.json()
    assert body["exists"] is True
    assert body["profile"]["work_history"][0]["company"] == "Acme"
    assert body["profile"]["work_history"][0]["current"] is True


def test_put_valid_full_profile_round_trips(client, profile_path):
    payload = {
        "candidate_name": "Jane Doe", "candidate_email": "jane@example.com",
        "candidate_phone": "+91-1", "linkedin_url": None, "portfolio_url": None,
        "gender": "female",
        "address": {"line1": "1 Main St", "city": "Chennai", "state": "TN",
                    "postal_code": "600001", "country": "India"},
        "work_history": [
            {"company": "Acme", "title": "Engineer", "start": "2022-01", "end": "",
             "current": True, "description": "Built things"},
            {"company": "Old Co", "title": "Intern", "start": "2020-06", "end": "2021-12",
             "current": False, "description": ""},
        ],
        "education": [
            {"institution": "IIT", "degree": "B.Tech", "field": "CS", "start": "2016", "end": "2020"},
        ],
    }
    r = client.put("/api/profile", json=payload)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Profile saved"}

    reloaded = load_candidate_profile(profile_path)
    assert [w.company for w in reloaded.work_history] == ["Acme", "Old Co"]
    assert reloaded.work_history[0].current is True
    assert reloaded.work_history[1].current is False
    assert reloaded.education[0].institution == "IIT"


def test_put_invalid_returns_422_and_leaves_file_unchanged(client, profile_path):
    original = CandidateProfile(candidate_name="Jane", candidate_email="jane@example.com",
                                candidate_phone="+91-1")
    save_candidate_profile(profile_path, original)
    before = profile_path.read_text(encoding="utf-8")

    payload = {
        "candidate_name": "  ", "candidate_email": "jane@example.com",
        "candidate_phone": "+91-1",
        "work_history": [{"title": "Engineer"}],  # missing company
    }
    r = client.put("/api/profile", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert "candidate_name" in body["errors"]
    assert "work_history.0.company" in body["errors"]
    assert profile_path.read_text(encoding="utf-8") == before


def test_put_trims_whitespace(client, profile_path):
    payload = {
        "candidate_name": "  Jane Doe  ", "candidate_email": " jane@example.com ",
        "candidate_phone": " +91-1 ", "gender": "female",
        "address": {"line1": "  1 Main St  ", "city": "", "state": "",
                    "postal_code": "", "country": ""},
        "work_history": [{"company": "  Acme  ", "title": " Engineer ", "start": "",
                          "end": "", "current": False, "description": ""}],
        "education": [],
    }
    r = client.put("/api/profile", json=payload)
    assert r.status_code == 200

    reloaded = load_candidate_profile(profile_path)
    assert reloaded.candidate_name == "Jane Doe"
    assert reloaded.candidate_email == "jane@example.com"
    assert reloaded.address.line1 == "1 Main St"
    assert reloaded.work_history[0].company == "Acme"
    assert reloaded.work_history[0].title == "Engineer"
