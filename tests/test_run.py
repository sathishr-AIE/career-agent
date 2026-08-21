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
    assert "scoring cap is 0" in caplog.text


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
    # Interleaved on purpose: near2 is the row that trips the "budget spent"
    # branch (it's the 3rd survivor against a budget of 2), and far10..19
    # are dead-end rows seeded AFTER it. Under `continue` those far rows
    # still get visited and hard-filtered this sweep; under `break` the loop
    # would stop at near2 and they'd never be swept -- that's what makes
    # `hard == 20` below distinguish the two, unlike an all-far-then-all-near
    # ordering where nothing follows the last survivor either way.
    for i in range(10):
        _seed_job(conn, f"far{i}", "San Francisco, CA")   # fail the filter
    for i in range(2):
        _seed_job(conn, f"near{i}", "Chennai")            # pass it, get scored
    _seed_job(conn, "near2", "Chennai")                   # pass it, budget spent
    for i in range(10, 20):
        _seed_job(conn, f"far{i}", "San Francisco, CA")   # fail it, seeded after
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
    # Interleaved on purpose, same reasoning as the test above: near0 trips
    # the "budget spent" branch immediately (max_score=0), and far2..4 are
    # seeded AFTER it so only `continue` (not `break`) would still sweep them.
    for i in range(2):
        _seed_job(conn, f"far{i}", "San Francisco, CA")
    _seed_job(conn, "near0", "Chennai")
    for i in range(2, 5):
        _seed_job(conn, f"far{i}", "San Francisco, CA")
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


async def test_scoring_model_comes_from_settings(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")
    conn.commit()
    from career_agent import store
    store.save_settings(conn, "claude-haiku-4-5", 25)

    seen = {}

    async def fake_score(job, brief, facts, ask):
        seen["model"] = ask.keywords["model"]
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=None, brief=brief_path))

    assert seen["model"] == "claude-haiku-4-5"
    conn = db.connect(db_path)
    row = conn.execute("SELECT model FROM assessment"
                       " WHERE stage = 'scored'").fetchone()
    assert row["model"] == "claude-haiku-4-5", \
        "the stored verdict must be attributable to the model that produced it"


async def test_a_retired_stored_model_falls_back_instead_of_being_sent(
        tmp_path, monkeypatch, caplog):
    """save_settings validates against SCORING_MODELS, but rows written
    before a model was retired -- and db.py's schema default, which repeats
    the id as a bare literal -- can still hold an id that is no longer
    allowed. Sending it would fail every scoring call; refusing to run would
    strand the user. Fall back to the default and warn."""
    from career_agent.config import DEFAULT_SCORING_MODEL

    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")
    # bypasses save_settings on purpose: this is a row from an older release
    conn.execute("UPDATE setting SET scoring_model = 'claude-retired-3'")
    conn.commit()

    seen = {}

    async def fake_score(job, brief, facts, ask):
        seen["model"] = ask.keywords["model"]
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    with caplog.at_level(logging.WARNING):
        await run_once(_Args(db_path, max_score=None, brief=brief_path))

    assert seen["model"] == DEFAULT_SCORING_MODEL
    assert "claude-retired-3" in caplog.text
    conn = db.connect(db_path)
    row = conn.execute("SELECT model FROM assessment"
                       " WHERE stage = 'scored'").fetchone()
    assert row["model"] == DEFAULT_SCORING_MODEL, \
        "the verdict must record the model that actually produced it"


async def test_omitted_max_score_uses_the_stored_setting(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(4):
        _seed_job(conn, f"near{i}", "Chennai")
    conn.commit()
    from career_agent import store
    store.save_settings(conn, "claude-sonnet-5", 2)

    calls = []

    async def fake_score(job, brief, facts, ask):
        calls.append(job.external_id)
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=None, brief=brief_path))
    assert len(calls) == 2


async def test_explicit_max_score_overrides_the_setting(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    for i in range(4):
        _seed_job(conn, f"near{i}", "Chennai")
    conn.commit()
    from career_agent import store
    store.save_settings(conn, "claude-sonnet-5", 2)

    calls = []

    async def fake_score(job, brief, facts, ask):
        calls.append(job.external_id)
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)
    await run_once(_Args(db_path, max_score=3, brief=brief_path))
    assert len(calls) == 3
    # the override is for this run only and must not be persisted
    conn = db.connect(db_path)
    assert store.get_settings(conn)["max_score_per_run"] == 2


async def test_run_once_reports_stages_in_order(tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)

    seen = []

    def progress(**kw):
        if "stage" in kw:
            seen.append(kw["stage"])

    await run_once(_Args(db_path, max_score=5, brief=brief_path),
                   progress=progress)

    assert seen == ["discover", "clean", "filter", "score", "ready"]


async def test_run_once_reports_counters_matching_what_it_did(
        tmp_path, monkeypatch):
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")               # passes the filter
    _seed_job(conn, "near1", "Chennai")               # passes the filter
    _seed_job(conn, "far0", "San Francisco, CA")      # fails it
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)

    latest = {}

    def progress(**kw):
        latest.update(kw)

    await run_once(_Args(db_path, max_score=5, brief=brief_path),
                   progress=progress)

    assert latest["passed"] == 2
    assert latest["scored"] == 2
    assert latest["shortlisted"] == 2, "VERDICT's verdict is 'submit'"
    assert latest["stage"] == "ready"


async def test_run_once_works_without_a_progress_callback(
        tmp_path, monkeypatch):
    """The CLI path. Omitting progress must change nothing."""
    db_path, brief_path, conn = _prepare(tmp_path, monkeypatch)
    _seed_job(conn, "near0", "Chennai")
    conn.commit()

    async def fake_score(job, brief, facts, ask):
        return VERDICT

    monkeypatch.setattr("career_agent.gate.score", fake_score)

    await run_once(_Args(db_path, max_score=5, brief=brief_path))

    conn = db.connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) n FROM assessment WHERE stage='scored'"
    ).fetchone()["n"] == 1
