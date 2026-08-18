import logging

import pytest

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
    def __init__(self, db_path, max_score):
        self.db = str(db_path)
        self.brief = "career_brief.toml"
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
