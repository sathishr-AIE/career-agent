# backend/tests/test_apply_agent.py
import json as _json
from pathlib import Path

import pytest

from career_agent.apply import agent as agent_mod
from career_agent.apply.agent import AgentResult, build_prompt, consume_stream, parse_result
from career_agent.config import CandidateProfile, CareerBrief
from career_agent.models import Job


def test_parse_applied():
    r = parse_result("filled the form\nRESULT:APPLIED\n")
    assert r.code == "applied" and r.reason == ""


def test_parse_draft_ready_with_answers():
    out = ('ANSWERS_JSON: {"_name": "A B", "Years of experience?": "6"}\n'
           "RESULT:DRAFT_READY\n")
    r = parse_result(out)
    assert r.code == "draft_ready"
    assert r.answers == {"_name": "A B", "Years of experience?": "6"}


def test_parse_draft_ready_without_answers_is_failed():
    r = parse_result("RESULT:DRAFT_READY\n")
    assert r.code == "failed" and r.reason == "bad_answers_json"


def test_parse_draft_ready_with_malformed_answers_is_failed():
    r = parse_result("ANSWERS_JSON: {oops\nRESULT:DRAFT_READY\n")
    assert r.code == "failed" and r.reason == "bad_answers_json"


def test_parse_needs_answer_keeps_question():
    r = parse_result("RESULT:NEEDS_ANSWER:Do you hold a PMP certification?\n")
    assert r.code == "needs_answer"
    assert r.reason == "Do you hold a PMP certification?"


def test_parse_failed_with_reason():
    r = parse_result("blah\nRESULT:FAILED:sso_required\n")
    assert r.code == "failed" and r.reason == "sso_required"


def test_parse_failed_without_reason():
    r = parse_result("RESULT:FAILED\n")
    assert r.code == "failed" and r.reason == "unknown"


def test_parse_simple_codes():
    assert parse_result("RESULT:EXPIRED").code == "expired"
    assert parse_result("RESULT:CAPTCHA").code == "captcha"
    assert parse_result("RESULT:LOGIN_ISSUE").code == "login_issue"


def test_parse_no_result_line():
    r = parse_result("the agent rambled and died")
    assert r.code == "failed" and r.reason == "no_result_line"


def test_parse_last_result_line_wins():
    out = "RESULT:FAILED:stuck\nrecovered actually\nRESULT:APPLIED\n"
    assert parse_result(out).code == "applied"


def test_parse_trailing_markdown_junk_stripped():
    assert parse_result("RESULT:FAILED:stuck**`").reason == "stuck"


def test_parse_answers_json_after_result_is_ignored():
    out = "RESULT:DRAFT_READY\nANSWERS_JSON: {\"x\": 1}\n"
    r = parse_result(out)
    assert r.code == "failed" and r.reason == "bad_answers_json"


# -- build_prompt --------------------------------------------------------

def _job(**kw):
    d = dict(source="ats", external_id="x1", company="Acme", title="Backend Eng",
             location="Chennai", is_remote=True, comp_min=None, comp_max=None,
             posted_at=None, url="https://boards.example/acme/1", description="desc")
    d.update(kw)
    return Job(**d)


def _profile():
    return CandidateProfile(candidate_name="Asha Rao",
                            candidate_email="asha@example.com",
                            candidate_phone="+91 90000 00000",
                            linkedin_url="https://linkedin.com/in/asha")


def _brief(**kw):
    # Inline construction, matching the established pattern in test_gate.py /
    # test_hardfilter.py / test_discovery.py -- not a toml read off cwd.
    d = dict(target_titles=["Backend Engineer"], search_locations=["Chennai"],
             locations=["Chennai", "Bangalore", "Remote"], remote_ok=True,
             gate_threshold=72)
    d.update(kw)
    return CareerBrief(**d)


QA = [{"question_normalized": "years of python experience",
       "answer": "6", "is_volatile": 0, "last_confirmed_at": None},
      {"question_normalized": "current notice period",
       "answer": "30 days", "is_volatile": 1,
       "last_confirmed_at": "2020-01-01 00:00:00"}]  # long stale


def test_prompt_embeds_job_profile_and_resume():
    p = build_prompt(_job(), _profile(), _brief(), QA, "RESUME BODY",
                     "C:/x/resume.docx", mode="auto")
    for needle in ("https://boards.example/acme/1", "Backend Eng", "Asha Rao",
                   "asha@example.com", "RESUME BODY", "resume.docx"):
        assert needle in p


def test_prompt_seeds_qa_bank_and_marks_stale_volatile():
    p = build_prompt(_job(), _profile(), _brief(), QA, "r", "x.docx", mode="auto")
    assert "years of python experience" in p
    assert "30 days" in p
    assert "stale" in p.lower()          # volatile row past the 30-day window


def test_draft_mode_forbids_submit_and_demands_answers_json():
    p = build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx", mode="draft")
    assert "RESULT:DRAFT_READY" in p and "ANSWERS_JSON" in p
    assert "do NOT" in p and "RESULT:APPLIED" not in p.split("RESULT CODES")[0]


def test_send_mode_pins_answers():
    p = build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx",
                     mode="send", pinned_answers={"Visa status?": "Citizen"})
    assert "EXACTLY" in p and "Visa status?" in p and "Citizen" in p


def test_send_mode_requires_pinned_answers():
    with pytest.raises(ValueError):
        build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx", mode="send")


def test_prompt_contains_safety_and_platform_rules():
    p = build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx", mode="auto")
    for needle in ("Never lie", "sso_required", "easy_apply", "RESULT:CAPTCHA",
                   "RESULT:NEEDS_ANSWER"):
        assert needle in p


def test_unconfirmed_volatile_row_is_marked_stale():
    qa = [{"question_normalized": "sponsorship needed", "answer": "No",
           "is_volatile": 1, "last_confirmed_at": None}]
    p = build_prompt(_job(), _profile(), _brief(), qa, "r", "x.docx", mode="auto")
    assert "sponsorship needed -> No  [stale" in p


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        build_prompt(_job(), _profile(), _brief(), [], "r", "x.docx", mode="bogus")


def test_location_check_states_remote_ok_explicitly():
    # locations deliberately has no literal "Remote" entry, so the only signal
    # the agent has about remote eligibility is brief.remote_ok itself.
    p_ok = build_prompt(_job(), _profile(), _brief(locations=["Chennai"], remote_ok=True),
                        [], "r", "x.docx", mode="auto")
    p_not_ok = build_prompt(_job(), _profile(),
                            _brief(locations=["Chennai"], remote_ok=False),
                            [], "r", "x.docx", mode="auto")
    assert "Remote work IS acceptable" in p_ok
    assert "Remote work is NOT acceptable" in p_not_ok
    assert p_ok != p_not_ok


# -- consume_stream -------------------------------------------------------

def test_consume_stream_collects_text_and_cost():
    lines = [
        _json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "navigating"},
            {"type": "tool_use", "name": "mcp__playwright__browser_click",
             "input": {"ref": "e12"}}]}}),
        _json.dumps({"type": "result", "total_cost_usd": 0.042,
                     "result": "RESULT:APPLIED"}),
    ]
    text, cost = consume_stream(lines)
    assert "navigating" in text and "RESULT:APPLIED" in text
    assert cost == 0.042


def test_consume_stream_tolerates_non_json_lines():
    text, cost = consume_stream(["not json at all", ""])
    assert "not json" in text and cost == 0.0


def test_consume_stream_missing_total_cost_usd_is_zero():
    line = _json.dumps({"type": "result", "result": "RESULT:APPLIED"})
    _, cost = consume_stream([line])
    assert cost == 0.0


def test_consume_stream_null_total_cost_usd_is_zero():
    line = _json.dumps({"type": "result", "total_cost_usd": None,
                        "result": "RESULT:APPLIED"})
    _, cost = consume_stream([line])
    assert cost == 0.0


# -- sandbox (the spawned session reads untrusted posting text) ------------

def test_the_spawned_session_gets_no_builtin_tools_and_no_other_mcp_config():
    """The agent runs bypassPermissions over job-posting text, which is a
    prompt-injection channel. --tools "" drops Bash/Read/Write/WebFetch and
    (per `claude --help`) the user/project/local settings files that carry
    the operator's hooks and plugins; --strict-mcp-config keeps every MCP
    server but ours out."""
    mcp_path = Path("C:/nowhere/.mcp-apply.json")
    cmd = agent_mod.build_cmd("sonnet", mcp_path)
    assert "--strict-mcp-config" in cmd
    assert "--tools" in cmd
    # the empty string must survive as its own argv element
    assert cmd[cmd.index("--tools") + 1] == ""
    # the flags the run still depends on
    assert cmd[cmd.index("--mcp-config") + 1] == str(mcp_path)
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[-1] == "-" and "-p" in cmd


def test_the_agent_workdir_is_outside_the_repo():
    """One relative path from backend/.env (CLAUDE_CODE_OAUTH_TOKEN,
    APIFY_TOKEN), candidate_profile.toml and career.db is not a cwd for an
    injectable agent."""
    repo_root = Path(agent_mod.__file__).resolve().parents[4]
    assert (repo_root / "backend" / "src").is_dir()   # the root really is the root
    for p in (agent_mod.WORK_DIR, agent_mod.WORK_DIR / "session"):
        assert not p.resolve().is_relative_to(repo_root), p


def test_transcripts_stay_in_the_repos_data_logs():
    """The audit trail: written by this process, not the agent, and
    recorded by absolute path in application.transcript_path."""
    assert agent_mod.LOG_DIR == Path("data/logs")
    assert not agent_mod.LOG_DIR.is_absolute()
