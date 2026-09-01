# backend/tests/test_apply_agent.py
import pytest

from career_agent.apply.agent import AgentResult, build_prompt, parse_result
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
