import logging

import pytest

from career_agent import db
from career_agent.models import Verdict
from career_agent.run import AuthError, run_once, verify_auth


def test_rejects_api_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        verify_auth()


def test_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(AuthError, match="claude setup-token"):
        verify_auth()


def test_passes_with_token_only(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    verify_auth()


class _Args:
    def __init__(self, db_path, max_score, brief="career_brief.toml"):
        self.db = str(db_path)
        self.brief = brief
        self.boards = "ats_boards.toml"
        self.max_score = max_score


async def test_run_once_derives_no_response_after_scoring(
        tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "dummy")
    monkeypatch.setattr("career_agent.discovery.run_discovery",
                         lambda *a, **k: [])

    calls = []
    monkeypatch.setattr("career_agent.outcomes.derive_no_response",
                         lambda conn: calls.append(conn) or 0)

    with caplog.at_level(logging.WARNING):
        await run_once(_Args(tmp_path / "t.db", max_score=0))

    assert len(calls) == 1
    assert "max-score 0" in caplog.text


TEST_BRIEF = """\
target_titles = ["AI Engineer"]
title_families = ["ai engineer"]
search_locations = ["Chennai"]
locations = ["Chennai"]
remote_ok = false
daily_cap = 5
gate_threshold = 72
staleness_days = 3650
"""

VERDICT = Verdict(role_fit=80, credibility=80, opportunity=80,
                  application_quality=80, eligibility_soft=80,
                  verdict="submit", rationale="ok")


def _seed_job(conn, fp, location):
    conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized, location)"
        " VALUES (?, 'ats', ?, 'Acme', 'acme', 'AI Engineer',"
        " 'aiengineer', ?)", (fp, fp, location))


def _prepare(tmp_path, monkeypatch):
    """Env + a stubbed discovery, so run_once exercises only the scoring loop."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("APIFY_TOKEN", "dummy")
    monkeypatch.setattr("career_agent.discovery.run_discovery",
                        lambda *a, **k: [])
    brief_path = tmp_path / "brief.toml"
    brief_path.write_text(TEST_BRIEF, encoding="utf-8")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    return db_path, str(brief_path), conn


async def test_max_score_caps_model_calls_not_rows_examined(
        tmp_path, monkeypatch):
    """The bug: --max-score was a LIMIT on rows pulled, so jobs the hard
    filter rejects consumed the budget and a run scored ~nothing."""
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(20):
        _seed_job(conn, f"far{i}", "San Francisco, CA")   # fail the filter
    for i in range(3):
        _seed_job(conn, f"near{i}", "Chennai")            # pass it
    conn.commit()

    calls = []

    async def fake_score(job, brief, facts, ask):
        calls.append(job.external_id)
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=2, brief=brief_path))

    assert len(calls) == 2, "the cap must bound MODEL CALLS"
    conn = db.connect(db_path)
    hard = conn.execute("SELECT COUNT(*) n FROM assessment"
                        " WHERE stage = 'hard'").fetchone()["n"]
    assert hard == 20, "the whole pool must still be hard-filtered in one sweep"


async def test_hard_skips_persist_so_the_next_run_finds_real_candidates(
        tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(5):
        _seed_job(conn, f"far{i}", "San Francisco, CA")
    _seed_job(conn, "near0", "Chennai")
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=0, brief=brief_path))

    conn = db.connect(db_path)
    from career_agent import gate, store
    # scoring was disabled, but the sweep still retired the 5 rejects, so the
    # remaining pool is exactly the one real candidate
    remaining = store.unscored_jobs(conn, gate.PROMPT_VERSION)
    assert [r["external_id"] for r in remaining] == ["near0"]
