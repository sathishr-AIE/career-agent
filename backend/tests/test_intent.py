import json

import pytest

from career_agent import db
from career_agent.web import intent


# ---------------------------------------------------------------------------
# route()
# ---------------------------------------------------------------------------

def test_route_returns_valid_runner_dict_as_is():
    def runner(prompt, schema):
        return {"intent": "find_jobs", "job_ref": None, "reply": "On it."}

    result = intent.route("find me new jobs", runner=runner)
    assert result == {"intent": "find_jobs", "job_ref": None, "reply": "On it."}


def test_route_passes_prompt_and_schema_to_runner():
    captured = {}

    def runner(prompt, schema):
        captured["prompt"] = prompt
        captured["schema"] = schema
        return {"intent": "help", "job_ref": None, "reply": "Sure."}

    intent.route("help", runner=runner)
    assert "help" in captured["prompt"].lower()
    assert captured["schema"] == intent.SCHEMA


def test_route_accepts_job_ref_as_string():
    def runner(prompt, schema):
        return {"intent": "apply_to", "job_ref": "SES", "reply": "Applying."}

    result = intent.route("apply to the SES role", runner=runner)
    assert result == {"intent": "apply_to", "job_ref": "SES", "reply": "Applying."}


@pytest.mark.parametrize("bad", [
    {"intent": "not_a_real_intent", "job_ref": None, "reply": "hi"},
    {"intent": "help", "job_ref": None},                       # missing reply
    {"intent": "help", "reply": 123},                          # reply not a string
    {"intent": "help", "job_ref": 42, "reply": "hi"},           # job_ref not string/None
    {"intent": "help", "job_ref": None, "reply": ""},           # empty reply
    "not a dict",
    None,
])
def test_route_coerces_invalid_runner_output_to_unknown(bad):
    def runner(prompt, schema):
        return bad

    result = intent.route("whatever", runner=runner)
    assert result == {"intent": "unknown", "job_ref": None,
                      "reply": "Sorry, I didn't understand that. Try 'help'."}


def test_route_runner_exception_falls_back_to_unknown_without_raising():
    def runner(prompt, schema):
        raise RuntimeError("boom")

    result = intent.route("find jobs", runner=runner)
    assert result == {"intent": "unknown", "job_ref": None,
                      "reply": "Sorry, I didn't understand that. Try 'help'."}


def test_route_runner_timeout_falls_back_to_unknown():
    import subprocess

    def runner(prompt, schema):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=60)

    result = intent.route("find jobs", runner=runner)
    assert result["intent"] == "unknown"


def test_default_runner_refuses_when_anthropic_api_key_is_set(monkeypatch):
    """An inherited ANTHROPIC_API_KEY outranks the subscription token and
    would silently bill per token (run.verify_auth's guard, also run
    before every apply_tick). route()'s default runner must check this
    BEFORE spawning claude -- verify_auth's raise is caught by route()'s
    own except and degrades to the unknown fallback."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-leaked")
    calls = []
    monkeypatch.setattr(intent.subprocess, "run",
                        lambda *a, **kw: calls.append((a, kw)))

    result = intent.route("find me new jobs")

    assert result == {"intent": "unknown", "job_ref": None,
                      "reply": "Sorry, I didn't understand that. Try 'help'."}
    assert calls == []  # subprocess.run was never reached


def test_route_truncates_overlong_input_before_reaching_runner():
    captured = {}

    def runner(prompt, schema):
        captured["prompt"] = prompt
        return {"intent": "unknown", "job_ref": None, "reply": "?"}

    intent.route("x" * 5000, runner=runner)
    # the raw text embedded in the prompt must be capped, not the whole
    # prompt (which also carries the fixed intent-list preamble)
    assert "x" * 2001 not in captured["prompt"]
    assert "x" * 2000 in captured["prompt"]


# ---------------------------------------------------------------------------
# build_intent_cmd()
# ---------------------------------------------------------------------------

def test_build_intent_cmd_argv():
    argv = intent.build_intent_cmd(intent.SCHEMA)
    assert "-p" in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--json-schema" in argv
    schema_arg = argv[argv.index("--json-schema") + 1]
    assert json.loads(schema_arg) == intent.SCHEMA
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--permission-mode" not in argv
    assert "bypassPermissions" not in argv


# ---------------------------------------------------------------------------
# resolve_job() / describe_matches()
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _job(conn, fp, company, title, verdict="submit", score=80):
    job_id = conn.execute(
        "INSERT INTO job (fingerprint, source, external_id, company,"
        " company_normalized, title, title_normalized)"
        " VALUES (?, 'ats', ?, ?, ?, ?, ?)",
        (fp, fp, company, company.lower(), title, title.lower())).lastrowid
    conn.execute(
        "INSERT INTO assessment (job_id, stage, weighted_score, verdict,"
        " rationale, model, prompt_version)"
        " VALUES (?, 'scored', ?, ?, 'r', 'm', 'v1')",
        (job_id, score, verdict))
    conn.commit()
    return job_id


def test_resolve_job_none_or_blank_returns_none(conn):
    assert intent.resolve_job(conn, None) is None
    assert intent.resolve_job(conn, "") is None
    assert intent.resolve_job(conn, "   ") is None


def test_resolve_job_hash_id_matches_existing_job(conn):
    jid = _job(conn, "fp1", "Acme", "AI Engineer")
    assert intent.resolve_job(conn, f"#{jid}") == jid


def test_resolve_job_bare_id_matches_existing_job(conn):
    jid = _job(conn, "fp1", "Acme", "AI Engineer")
    assert intent.resolve_job(conn, str(jid)) == jid


def test_resolve_job_nonexistent_id_returns_none(conn):
    assert intent.resolve_job(conn, "#999999") is None
    assert intent.resolve_job(conn, "999999") is None


def test_resolve_job_company_substring_unique_match(conn):
    jid = _job(conn, "fp1", "SES Government Solutions", "AI Engineer")
    assert intent.resolve_job(conn, "SES") == jid


def test_resolve_job_title_substring_match(conn):
    jid = _job(conn, "fp1", "Acme", "Senior AI Engineer")
    assert intent.resolve_job(conn, "AI Engineer") == jid


def test_resolve_job_ambiguous_returns_none(conn):
    _job(conn, "fp1", "Acme", "AI Engineer")
    _job(conn, "fp2", "Acme Robotics", "Data Scientist")
    assert intent.resolve_job(conn, "Acme") is None


def test_resolve_job_not_in_queue_is_not_matched(conn):
    # verdict 'skip' -> QUEUE_WHERE excludes it
    _job(conn, "fp1", "SkippedCo", "AI Engineer", verdict="skip")
    assert intent.resolve_job(conn, "SkippedCo") is None


def test_resolve_job_like_metacharacters_are_literal(conn):
    jid = _job(conn, "fp1", "100%_Fresh Co", "AI Engineer")
    # if % or _ were treated as wildcards this would over-match; it must
    # only match the literal substring
    assert intent.resolve_job(conn, "100%_Fresh") == jid

    _job(conn, "fp2", "Acme", "AI Engineer")
    # an unescaped '_' or '%' pattern must not accidentally match fp2's row
    assert intent.resolve_job(conn, "100%_Fresh") == jid


def test_resolve_job_underscore_is_not_a_wildcard(conn):
    """'_' in LIKE matches any single char. Unescaped, "A_Fresh" would also
    match "AZFresh" (Z standing in for the wildcard), making the query
    ambiguous and resolve_job wrongly return None. Escaped, only the
    literal "A_Fresh Co" matches."""
    jid = _job(conn, "fp1", "A_Fresh Co", "AI Engineer")
    _job(conn, "fp2", "AZFresh Co", "AI Engineer")
    assert intent.resolve_job(conn, "A_Fresh") == jid


def test_resolve_job_percent_is_not_a_wildcard(conn):
    """'%' in LIKE matches any run of chars. Unescaped, "50% Off" would
    also match "50X Off" (X standing in for the wildcard), making the
    query ambiguous and resolve_job wrongly return None. Escaped, only
    the literal "50% Off" substring matches."""
    jid = _job(conn, "fp1", "50% Off Co", "AI Engineer")
    _job(conn, "fp2", "50X Off Co", "AI Engineer")
    assert intent.resolve_job(conn, "50% Off") == jid


def test_resolve_job_hash_id_resolves_even_when_not_in_queue(conn):
    """The #id/bare-id path deliberately bypasses QUEUE_WHERE (any status
    resolves by id) -- Task 20's apply_to must route through do_apply,
    which re-validates before actually applying."""
    jid = _job(conn, "fp1", "SkippedCo", "AI Engineer", verdict="skip")
    assert intent.resolve_job(conn, f"#{jid}") == jid


def test_resolve_job_case_insensitive(conn):
    jid = _job(conn, "fp1", "Acme", "AI Engineer")
    assert intent.resolve_job(conn, "acme") == jid


def test_describe_matches_caps_at_five(conn):
    for i in range(7):
        _job(conn, f"fp{i}", f"Acme {i}", "AI Engineer")
    matches = intent.describe_matches(conn, "Acme")
    assert len(matches) == 5
    for m in matches:
        assert set(m.keys()) >= {"id", "company", "title"}


def test_describe_matches_empty_for_blank_ref(conn):
    assert intent.describe_matches(conn, None) == []
    assert intent.describe_matches(conn, "") == []


def test_describe_matches_returns_id_result_for_hash_id(conn):
    jid = _job(conn, "fp1", "Acme", "AI Engineer")
    matches = intent.describe_matches(conn, f"#{jid}")
    assert matches == [{"id": jid, "company": "Acme", "title": "AI Engineer"}]
