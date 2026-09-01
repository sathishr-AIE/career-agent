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
    end on a different code. ANSWERS_JSON must appear before RESULT:DRAFT_READY."""
    lines = output.splitlines()

    # Find the last RESULT: line and its index
    result_line = None
    result_line_idx = -1
    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith("RESULT:"):
            result_line = line
            result_line_idx = i

    if result_line is None:
        return AgentResult("failed", "no_result_line")

    # Scan for ANSWERS_JSON that appears before the winning RESULT: line
    answers = None
    for i, line in enumerate(lines):
        if i >= result_line_idx:
            break
        line = line.strip()
        if line.startswith("ANSWERS_JSON:"):
            try:
                answers = json.loads(line[len("ANSWERS_JSON:"):].strip())
            except json.JSONDecodeError:
                pass  # Skip malformed, keep previous value

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
