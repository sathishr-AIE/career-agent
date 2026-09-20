"""MS1: the apply-agent model setting, the chat picker's partial update, and
the live run's model (spec "Model selection in chat")."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_agent import db, store
from career_agent.apply import agent as agent_mod
from career_agent.config import APPLY_MODELS, DEFAULT_APPLY_MODEL
from career_agent.web import api, context
from career_agent.web import app as web

BRIEF_TOML = """\
target_titles = ["AI Engineer"]
search_locations = ["Chennai"]
locations = ["Chennai"]
daily_cap = 5
gate_threshold = 72
staleness_days = 30
"""


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.init_schema(c)
    brief = tmp_path / "career_brief.toml"
    brief.write_text(BRIEF_TOML, encoding="utf-8")
    monkeypatch.setattr(web, "DB_PATH", path)
    monkeypatch.setattr(web, "BRIEF_PATH", brief)
    monkeypatch.setattr(web, "CANDIDATE_PROFILE_PATH", tmp_path / "candidate_profile.toml")
    return c


@pytest.fixture
def client(conn):
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _models(conn):
    s = store.get_settings(conn)
    return s["scoring_model"], s["apply_model"]


def test_apply_model_defaults_to_sonnet_and_settings_expose_the_choices(client, conn):
    assert _models(conn) == ("claude-sonnet-5", DEFAULT_APPLY_MODEL) == ("claude-sonnet-5",) * 2
    body = client.get("/api/settings").json()
    assert body["settings"]["apply_model"] == "claude-sonnet-5"
    assert body["apply_models"] == list(APPLY_MODELS) == ["claude-sonnet-5", "claude-opus-5"]
    assert set(body["apply_model_labels"]) == set(APPLY_MODELS)


def test_models_route_writes_only_the_models_it_is_given(client, conn):
    r = client.put("/api/settings/models", json={"apply_model": "claude-opus-5"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "scoring_model": "claude-sonnet-5", "apply_model": "claude-opus-5"}
    assert store.get_settings(conn)["max_score_per_run"] == 25  # untouched
    r = client.put("/api/settings/models", json={"scoring_model": "claude-haiku-4-5"})
    assert r.status_code == 200 and _models(conn) == ("claude-haiku-4-5", "claude-opus-5")


@pytest.mark.parametrize("body, field", [
    ({"apply_model": "claude-haiku-4-5"}, "apply_model"),   # too weak to drive a browser
    ({"scoring_model": "claude-opus-5"}, "scoring_model"),
    ({"scoring_model": "claude-sonnet-5", "apply_model": "gpt-5"}, "apply_model"),
    ({}, "form"),
])
def test_models_route_refuses_unknown_models_and_writes_nothing(client, conn, body, field):
    r = client.put("/api/settings/models", json=body)
    assert r.status_code == 422 and field in r.json()["errors"]
    assert _models(conn) == ("claude-sonnet-5", "claude-sonnet-5")


def _settings_form(**over):
    return {"brief_present": True, "target_titles": "AI Engineer", "search_locations": "Chennai",
            "locations": "Chennai", "daily_cap": 5, "gate_threshold": 72, "staleness_days": 30,
            "scoring_model": "claude-sonnet-5", "max_score_per_run": 25, **over}


def test_full_settings_save_sets_the_apply_model_and_blank_keeps_it(client, conn):
    assert client.put("/api/settings", json=_settings_form(apply_model="claude-opus-5")).status_code == 200
    assert _models(conn)[1] == "claude-opus-5"
    # The Jinja form never sends it: blank means unchanged.
    assert client.put("/api/settings", json=_settings_form()).status_code == 200
    assert _models(conn)[1] == "claude-opus-5"


def test_full_settings_save_refuses_an_unknown_apply_model_before_writing(client, conn):
    r = client.put("/api/settings", json=_settings_form(apply_model="nope", max_score_per_run=9))
    assert r.status_code == 422 and "apply_model" in r.json()["errors"]
    assert store.get_settings(conn)["max_score_per_run"] == 25


def test_run_status_reports_each_live_sessions_model(conn, monkeypatch):
    """The Job chat's picker locks while its session runs: a running claude -p
    can't switch models, so it shows the model the session was spawned with."""
    assert context.run_status_context(conn)["live_runs"] == []
    run = SimpleNamespace(cmd=["claude", "-p", "--model", "claude-opus-5", "--verbose"])
    monkeypatch.setitem(agent_mod.RUNS, 7, run)
    assert context.run_status_context(conn)["live_runs"] == [{"job_id": 7, "model": "claude-opus-5"}]


def test_full_settings_save_explains_empty_and_bad_fields_plainly(client, conn):
    """The Settings page shows these under each field and in its error summary."""
    r = client.put("/api/settings", json=_settings_form(target_titles=" , ", daily_cap="",
                                                        staleness_days="soon"))
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert errors["daily_cap"] == "Required"
    assert errors["staleness_days"] == "Must be a whole number"
    r = client.put("/api/settings", json=_settings_form(max_score_per_run=-1))
    assert r.json()["errors"] == {"max_score_per_run": "Can't be negative"}
    # the brief isn't validated until its numbers parse; fix them and the list error shows
    r = client.put("/api/settings", json=_settings_form(target_titles=" , "))
    assert r.json()["errors"] == {"target_titles": "Add at least one"}
