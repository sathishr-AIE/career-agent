"""Intent router for the Home chat (spec slice S7).

Classifies a free-text Home-chat message into a fixed intent via a
sandboxed one-shot `claude -p --model haiku --json-schema ...` classifier
-- same `--tools ""` / `--strict-mcp-config` containment as
apply/agent.py's build_cmd, minus the Playwright MCP server and
bypassPermissions: this call reads only the user's own chat text (not
untrusted job-posting text) and needs no tools at all, so there is nothing
for --permission-mode to gate.

`resolve_job` then turns the free-text `job_ref` the classifier extracted
into a job id by matching it against the apply queue (worker.py's
QUEUE_WHERE -- the one definition of "in the queue").
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess

from career_agent.web.worker import QUEUE_WHERE

log = logging.getLogger(__name__)

INTENTS = ("find_jobs", "show_queue", "apply_to", "pause_apply",
           "resume_apply", "stop_apply", "status", "help", "unknown")

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"enum": list(INTENTS)},
        "job_ref": {"type": ["string", "null"]},
        "reply": {"type": "string"},
    },
    "required": ["intent", "reply"],
    "additionalProperties": False,
}

_MAX_INPUT = 2000
_UNKNOWN = {"intent": "unknown", "job_ref": None,
            "reply": "Sorry, I didn't understand that. Try 'help'."}

_INTENT_MEANINGS = """\
find_jobs   -- run a new discovery+score pass
show_queue  -- list jobs currently queued to apply
apply_to    -- start or approve applying to one specific job (job_ref names it)
pause_apply -- pause the apply worker
resume_apply-- resume the paused apply worker
stop_apply  -- stop the apply worker
status      -- report current run/apply status
help        -- explain what this chat can do
unknown     -- none of the above fit"""


def _build_prompt(text: str) -> str:
    return (
        "Classify the user's chat message into exactly one intent from this "
        f"fixed list:\n{_INTENT_MEANINGS}\n\n"
        "job_ref is the user's own words identifying a job if the message "
        "names one -- an id like \"#1639\", a company, or a title fragment "
        "-- else null.\n"
        "reply is one short, friendly sentence responding to the user.\n\n"
        f"Message: {text}"
    )


def build_intent_cmd(schema: dict) -> list[str]:
    """argv for the sandboxed one-shot classifier. No tools, no MCP server,
    no bypassPermissions -- the classifier only reads chat text and returns
    structured JSON, so it needs none of build_cmd's browser machinery."""
    return ["claude", "-p", "--model", "haiku",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--tools", "",
            "--strict-mcp-config"]


def _default_runner(prompt: str, schema: dict) -> dict:
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    proc = subprocess.run(build_intent_cmd(schema), input=prompt,
                          capture_output=True, text=True, env=env,
                          timeout=60, check=True)
    data = json.loads(proc.stdout)
    structured = data.get("structured_output")
    if structured is None:
        structured = json.loads(data["result"])
    return structured


def _coerce(result) -> dict:
    if not isinstance(result, dict):
        return dict(_UNKNOWN)
    intent_val = result.get("intent")
    reply = result.get("reply")
    job_ref = result.get("job_ref")
    if intent_val not in INTENTS or not isinstance(reply, str) or not reply:
        return dict(_UNKNOWN)
    if job_ref is not None and not isinstance(job_ref, str):
        return dict(_UNKNOWN)
    return {"intent": intent_val, "job_ref": job_ref, "reply": reply}


def route(text: str, *, runner=None) -> dict:
    """Classify one Home-chat message. `runner(prompt, schema) -> dict` is
    the only seam -- tests inject a fake; the default spawns `claude`.
    Never raises: any runner failure (bad output, exception, timeout)
    degrades to the unknown intent so a chat message can't crash the chat."""
    runner = runner or _default_runner
    text = (text or "")[:_MAX_INPUT]
    try:
        result = runner(_build_prompt(text), SCHEMA)
    except Exception:
        log.warning("intent router: runner failed", exc_info=True)
        return dict(_UNKNOWN)
    return _coerce(result)


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so a job_ref containing '%' or '_' is matched
    literally, not as a pattern. '\\' is the ESCAPE char used below."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _looks_like_id(ref: str) -> int | None:
    """"#1639" or "1639" -> 1639; anything else -> None. Existence is a
    separate DB check -- this only decides which lookup path to take."""
    id_str = ref[1:] if ref.startswith("#") else ref
    return int(id_str) if id_str.isdigit() else None


def describe_matches(conn: sqlite3.Connection, job_ref: str | None) -> list[dict]:
    """Up to 5 {id, company, title} candidates for job_ref, so a caller can
    ask "did you mean ...?" on an ambiguous or unresolved reference."""
    ref = (job_ref or "").strip()
    if not ref:
        return []
    as_id = _looks_like_id(ref)
    if as_id is not None:
        row = conn.execute("SELECT id, company, title FROM job WHERE id = ?",
                           (as_id,)).fetchone()
        return [dict(row)] if row else []
    pattern = f"%{_escape_like(ref)}%"
    rows = conn.execute(
        f"""SELECT j.id, j.company, j.title FROM job j
            JOIN assessment a ON a.job_id = j.id
            WHERE {QUEUE_WHERE}
              AND (j.company LIKE ? ESCAPE '\\' OR j.title LIKE ? ESCAPE '\\')
            LIMIT 5""",
        (pattern, pattern)).fetchall()
    return [dict(r) for r in rows]


def resolve_job(conn: sqlite3.Connection, job_ref: str | None) -> int | None:
    """None/blank -> None. "#1639" or "1639" -> that job's id, only if it
    exists (any status). Otherwise a case-insensitive substring match on
    company OR title among jobs currently in the apply queue -- exactly one
    match resolves it, zero or several is ambiguous -> None."""
    ref = (job_ref or "").strip()
    if not ref:
        return None
    matches = describe_matches(conn, ref)
    if _looks_like_id(ref) is not None:
        return matches[0]["id"] if matches else None  # id path: 0 or 1 rows
    return matches[0]["id"] if len(matches) == 1 else None
