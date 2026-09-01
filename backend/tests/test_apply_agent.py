# backend/tests/test_apply_agent.py
from career_agent.apply.agent import AgentResult, parse_result


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
