"""Agentic apply engine: builds the playbook prompt, runs one Claude Code
session per job against a real Chrome over CDP, and parses the sentinel
outcome. See docs/lld-apply-button-v2.md."""
import json
import re
from dataclasses import dataclass

APPLY_MODEL = "sonnet"  # ponytail: constant; promote to the setting table
                        # when someone actually wants to change it


@dataclass
class AgentResult:
    code: str            # applied | draft_ready | expired | captcha
                         # | login_issue | needs_answer | failed
    reason: str = ""
    answers: dict | None = None
    transcript_path: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0


_SIMPLE = {"APPLIED": "applied", "EXPIRED": "expired",
           "CAPTCHA": "captcha", "LOGIN_ISSUE": "login_issue"}


def _clean(s: str) -> str:
    return re.sub(r'[*`"\s]+$', "", s).strip()


def parse_result(output: str) -> AgentResult:
    """Last RESULT: line wins -- the agent may hit a failure, recover, and
    end on a different code."""
    answers = None
    result_line = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("ANSWERS_JSON:"):
            try:
                answers = json.loads(line[len("ANSWERS_JSON:"):].strip())
            except json.JSONDecodeError:
                answers = None
        elif line.startswith("RESULT:"):
            result_line = line
    if result_line is None:
        return AgentResult("failed", "no_result_line")
    body = _clean(result_line[len("RESULT:"):])
    if body in _SIMPLE:
        return AgentResult(_SIMPLE[body])
    if body == "DRAFT_READY":
        if not isinstance(answers, dict):
            return AgentResult("failed", "bad_answers_json")
        return AgentResult("draft_ready", answers=answers)
    if body.startswith("NEEDS_ANSWER:"):
        return AgentResult("needs_answer", body[len("NEEDS_ANSWER:"):].strip())
    if body.startswith("FAILED"):
        rest = body[len("FAILED"):].lstrip(":").strip()
        return AgentResult("failed", rest or "unknown")
    return AgentResult("failed", f"unrecognized_result:{body[:50]}")
