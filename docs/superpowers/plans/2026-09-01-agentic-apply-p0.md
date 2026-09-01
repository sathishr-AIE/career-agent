# Agentic Apply Engine (P0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deterministic Greenhouse form filler with a per-job Claude Code agent driving real Chrome over CDP, so every job source can be applied to, with classified failures.

**Architecture:** New `apply/agent.py` (prompt builder + `claude -p` subprocess + sentinel parser) and `apply/chrome.py` (real-Chrome lifecycle) slot in under `apply/ats.py`'s `submit()`, whose public contract and application state machine stay as-is. The old Playwright filler functions are deleted. Auto mode becomes a single agent run; manual mode keeps draft-review-send with pinned answers.

**Tech Stack:** Python 3.11+, pytest + pytest-asyncio (`asyncio_mode=auto`), Claude Code CLI (`claude -p --output-format stream-json`), `@playwright/mcp` via npx, system Chrome with `--remote-debugging-port`, SQLite.

**Spec:** `docs/lld-apply-button-v2.md` (design), with rationale in `docs/applypilot-analysis.md`. Executors read both.

## Global Constraints

- All commands run from `backend/` (`cd backend && pytest ...`).
- Schema changes ONLY via `_add_column_if_missing` in `db.py` — never a bare `CREATE TABLE` edit for existing tables (every request re-runs `init_schema`).
- Datetimes stay naive UTC to match SQLite `datetime('now')` (see `ats._confirmed_within_days` comment; recurring bug class).
- No subprocess/browser code in unit tests — the agent/Chrome shells are exercised only through injected fakes (house convention; the old filler followed the same rule).
- `SUBMISSION_IMPLEMENTED` stays `False` in every commit of this plan.
- Do NOT copy prompt text or code from the ApplyPilot repository (AGPL). Structure may match the spec's section table; wording is written fresh.
- Live agent runs cost real API credits — no task in this plan runs one. Task 9's live verification is explicitly user-gated.
- Windows dev machine: process kill = `taskkill /F /T`, Chrome profile source = `%LOCALAPPDATA%\Google\Chrome\User Data`.

## File Structure

```
backend/src/career_agent/apply/
    ats.py       # submit() orchestration + state machine (rewritten internals, same signature shape)
    agent.py     # NEW: AgentResult, parse_result, build_prompt, run_agent, stream consumption
    chrome.py    # NEW: chrome_command, get_chrome_path, ensure_profile, launch_chrome, cleanup
backend/src/career_agent/db.py        # +2 columns via _add_column_if_missing
backend/src/career_agent/store.py     # +qa_all()
backend/src/career_agent/web/worker.py  # apply_tick: auto = single pass
backend/tests/
    test_apply_agent.py   # NEW: parse_result, build_prompt, consume_stream
    test_apply_chrome.py  # NEW: chrome_command only
    test_apply_ats.py     # rewritten around fake run_agent
    test_worker.py        # auto-mode single-pass cases updated
```

---

### Task 1: `AgentResult` and `parse_result`

**Files:**
- Create: `backend/src/career_agent/apply/agent.py`
- Test: `backend/tests/test_apply_agent.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces: `AgentResult` dataclass with fields `code: str`, `reason: str = ""`, `answers: dict | None = None`, `transcript_path: str = ""`, `cost_usd: float = 0.0`, `duration_ms: int = 0`; and `parse_result(output: str) -> AgentResult`. Codes emitted: `applied`, `draft_ready`, `expired`, `captcha`, `login_issue`, `needs_answer`, `failed`. Tasks 4 and 6 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_apply_agent.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError` (agent module doesn't exist).

- [ ] **Step 3: Implement**

```python
# backend/src/career_agent/apply/agent.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_apply_agent.py -v` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/career_agent/apply/agent.py backend/tests/test_apply_agent.py
git commit -m "feat: AgentResult and sentinel parser for the agentic apply engine"
```

---

### Task 2: `build_prompt`

**Files:**
- Modify: `backend/src/career_agent/apply/agent.py`
- Test: `backend/tests/test_apply_agent.py`

**Interfaces:**
- Consumes: `career_agent.models.Job`, `career_agent.config.CandidateProfile`, `career_agent.config.CareerBrief` (existing pydantic models — see those files for fields).
- Produces: `build_prompt(job, profile, brief, qa_rows, resume_text, resume_path, *, mode, pinned_answers=None, score=None) -> str` where `mode` ∈ `{"draft", "send", "auto"}`, `qa_rows` is a `list[dict]` with keys `question_normalized`, `answer`, `is_volatile`, `last_confirmed_at`, and `pinned_answers` is the question→answer dict (required when `mode="send"`). Task 6 calls this exact signature.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_apply_agent.py` (reuse the job/brief factory style from `tests/test_apply_ats.py` if one exists; otherwise build models inline as below):

```python
import pytest
from career_agent.apply.agent import build_prompt
from career_agent.config import CandidateProfile, CareerBrief
from career_agent.models import Job


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


def _brief():
    # Match career_brief.toml.example / CareerBrief's required fields.
    import tomllib
    from pathlib import Path
    text = Path("career_brief.toml").read_text(encoding="utf-8")
    return CareerBrief(**tomllib.loads(text))


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_apply_agent.py -k prompt -v`
Expected: FAIL with `ImportError: cannot import name 'build_prompt'`.

- [ ] **Step 3: Implement `build_prompt`**

Add to `agent.py`. Structure below is the contract; write the full prose of each
section fresh, following the spec's §3.2 section table (`docs/lld-apply-button-v2.md`)
and the section-by-section description in `docs/applypilot-analysis.md` §3.4 — do not
copy ApplyPilot's text. Reuse `_confirmed_within_days` by importing it from
`career_agent.apply.ats` for the volatile-stale check (it already handles the
naive-UTC rule); tolerate `last_confirmed_at=None` as stale.

```python
def build_prompt(job, profile, brief, qa_rows, resume_text, resume_path, *,
                 mode, pinned_answers=None, score=None) -> str:
    if mode not in ("draft", "send", "auto"):
        raise ValueError(f"unknown mode {mode!r}")
    if mode == "send" and pinned_answers is None:
        raise ValueError("send mode requires pinned_answers")

    from career_agent.apply.ats import _confirmed_within_days, QA_VOLATILE_WINDOW_DAYS

    def qa_line(row):
        stale = row["is_volatile"] and not (
            row["last_confirmed_at"]
            and _confirmed_within_days(row["last_confirmed_at"],
                                       QA_VOLATILE_WINDOW_DAYS))
        mark = "  [stale — reconfirm before relying on this]" if stale else ""
        return f"- {row['question_normalized']} -> {row['answer']}{mark}"

    known_answers = "\n".join(qa_line(r) for r in qa_rows) or "(none recorded)"
    locations = ", ".join(getattr(brief, "locations", []) or []) or "remote only"

    sections = [
        _job_section(job, score),              # JOB: url/title/company/score
        _files_section(resume_path),           # FILES: resume path to upload
        f"== RESUME TEXT ==\n{resume_text}",
        _profile_section(profile),             # name/email/phone/links + standard
                                               # defaults (18+, background check ok,
                                               # EEO: decline to self-identify)
        f"== KNOWN ANSWERS (prefer these verbatim) ==\n{known_answers}",
        _hard_rules_section(),                 # "Never lie about ..."; hard facts
                                               # only from PROFILE/KNOWN ANSWERS,
                                               # else RESULT:NEEDS_ANSWER
        _never_do_section(),                   # camera/mic/biometric/marketplace/
                                               # payment -> failure slugs
        _location_section(locations),          # decision table, run FIRST
        _platform_rules_section(),             # linkedin easy_apply, naukri_platform,
                                               # sso_required domains
        _screening_section(),                  # hard facts / skills / open-ended / EEO
        _steps_section(mode),                  # numbered steps incl. mode ending
        _efficiency_section(),                 # one snapshot per page, fill_form once
        _form_tricks_section(),                # tabs, prefill pages, dropdowns,
                                               # phone digits, honeypots
        _give_up_section(),                    # stuck/expired/page_error/CAPTCHA;
                                               # stop immediately
        _result_codes_section(),               # the exact grammar (below)
    ]
    if mode == "send":
        pinned = "\n".join(f"- {q} -> {a}" for q, a in pinned_answers.items())
        sections.insert(5, "== PINNED ANSWERS ==\nUse EXACTLY these answers for "
                           "these questions; do not improvise different ones:\n"
                           + pinned)
    return "\n\n".join(sections)
```

`_result_codes_section()` must emit exactly (this is the contract with `parse_result`):

```
== RESULT CODES (output EXACTLY one, on its own line, as your final output) ==
RESULT:APPLIED -- submitted and confirmation page seen
RESULT:DRAFT_READY -- form fully filled, NOT submitted (draft mode only; output ANSWERS_JSON first)
RESULT:EXPIRED -- posting closed / no longer accepting applications
RESULT:CAPTCHA -- a CAPTCHA blocks progress (do not try to solve it)
RESULT:LOGIN_ISSUE -- could not sign in or register
RESULT:NEEDS_ANSWER:<question> -- a hard-fact question not covered by PROFILE or KNOWN ANSWERS
RESULT:FAILED:<reason> -- anything else; use slugs sso_required, easy_apply,
    naukri_platform, not_eligible_location, already_applied, not_a_job_application,
    unsafe_permissions, unsafe_verification, stuck, page_error when they fit
```

`_steps_section(mode)` ends with, per mode — draft:
"Before finishing: take a full-page screenshot of the completed form. Then output one
line `ANSWERS_JSON: {...}` mapping every question you answered to the answer you gave
(use keys `_name`, `_email`, `_phone`, `_resume_uploaded` for the standard fields),
then `RESULT:DRAFT_READY`. Do NOT click Submit/Apply — this run is for human review.";
send: "Fill using the PINNED ANSWERS, verify every field against them, click Submit,
confirm the thank-you/received page, then output RESULT:APPLIED."; auto: "Verify every
field, output the `ANSWERS_JSON:` line, click Submit, confirm the thank-you page, then
output RESULT:APPLIED."

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_apply_agent.py -v` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/career_agent/apply/agent.py backend/tests/test_apply_agent.py
git commit -m "feat: agent playbook prompt builder with draft/send/auto modes"
```

---

### Task 3: `chrome.py` — real-Chrome lifecycle

**Files:**
- Create: `backend/src/career_agent/apply/chrome.py`
- Test: `backend/tests/test_apply_chrome.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `chrome_command(chrome_exe: str, profile_dir: Path, port: int = 9222, headless: bool = False) -> list[str]` (pure, tested); `get_chrome_path() -> str`; `ensure_profile() -> Path`; `launch_chrome(port: int = 9222, headless: bool = False) -> subprocess.Popen`; `cleanup(proc: subprocess.Popen | None) -> None`. Task 6 wraps `launch_chrome`/`cleanup` around the agent run.

- [ ] **Step 1: Write the failing test (command builder only — the shell is untested by convention)**

```python
# backend/tests/test_apply_chrome.py
from pathlib import Path
from career_agent.apply.chrome import chrome_command


def test_chrome_command_flags():
    cmd = chrome_command("C:/chrome.exe", Path("C:/prof"), port=9223)
    assert cmd[0] == "C:/chrome.exe"
    assert "--remote-debugging-port=9223" in cmd
    assert any(a.startswith("--user-data-dir=") and a.endswith("prof") for a in cmd)
    for flag in ("--no-first-run", "--deny-permission-prompts",
                 "--use-fake-ui-for-media-stream", "--disable-notifications",
                 "--disable-session-crashed-bubble", "--password-store=basic"):
        assert flag in cmd
    assert "--headless=new" not in cmd
    assert "--headless=new" in chrome_command("c", Path("p"), headless=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_apply_chrome.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# backend/src/career_agent/apply/chrome.py
"""Real-Chrome lifecycle for the apply agent: launch the system Chrome with
CDP remote debugging on an isolated profile cloned once from the user's own
profile. Single worker, port 9222. See docs/lld-apply-button-v2.md §4."""
import json
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

PROFILE_DIR = Path("data/chrome-profile")

_SKIP_ON_CLONE = {"Cache", "Code Cache", "GPUCache", "ShaderCache",
                  "GrShaderCache", "Service Worker", "CacheStorage",
                  "Crashpad", "Temp", "SingletonLock", "SingletonSocket",
                  "SingletonCookie", "BrowserMetrics", "SafeBrowsing"}

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
]


def chrome_command(chrome_exe: str, profile_dir: Path, port: int = 9222,
                   headless: bool = False) -> list[str]:
    cmd = [chrome_exe,
           f"--remote-debugging-port={port}",
           f"--user-data-dir={profile_dir}",
           "--profile-directory=Default",
           "--no-first-run", "--no-default-browser-check",
           "--window-size=1280,800",
           "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
           "--noerrdialogs", "--password-store=basic",
           "--disable-save-password-bubble",
           "--deny-permission-prompts", "--use-fake-ui-for-media-stream",
           "--use-fake-device-for-media-stream", "--disable-notifications"]
    if headless:
        cmd.append("--headless=new")
    return cmd


def get_chrome_path() -> str:
    override = os.environ.get("CHROME_PATH")
    if override:
        return override
    for c in _CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    raise RuntimeError("Chrome not found -- set CHROME_PATH in .env")


def _user_profile_source() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


def ensure_profile() -> Path:
    """One-time clone of the user's Chrome profile (cookies, sessions,
    fingerprint). Chrome must be closed during the first clone or files
    are locked -- the copy skips what it can't read."""
    if (PROFILE_DIR / "Default").exists():
        return PROFILE_DIR
    src = _user_profile_source()
    if not src.exists():
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)  # fresh profile fallback
        return PROFILE_DIR
    log.info("Cloning Chrome profile from %s (first run)...", src)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in _SKIP_ON_CLONE:
            continue
        try:
            if item.is_dir():
                shutil.copytree(item, PROFILE_DIR / item.name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*_SKIP_ON_CLONE))
            else:
                shutil.copy2(item, PROFILE_DIR / item.name)
        except (PermissionError, OSError):
            pass  # locked file; skip
    return PROFILE_DIR


def _patch_prefs(profile_dir: Path) -> None:
    prefs = profile_dir / "Default" / "Preferences"
    if not prefs.exists():
        return
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
        data.setdefault("profile", {})["exit_type"] = "Normal"
        data.setdefault("session", {})["restore_on_startup"] = 4
        data["credentials_enable_service"] = False
        data.setdefault("autofill", {})["profile_enabled"] = False
        prefs.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        log.debug("could not patch Chrome prefs", exc_info=True)


def _kill_port(port: int) -> None:
    """Zombie sweep: kill whatever still listens on the CDP port."""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                       capture_output=True, timeout=10)
        else:
            out = subprocess.run(["lsof", "-ti", f":{port}"],
                                 capture_output=True, text=True, timeout=10).stdout
            for pid in out.split():
                subprocess.run(["kill", "-9", pid], capture_output=True)
    except Exception:
        log.debug("port sweep failed", exc_info=True)


def launch_chrome(port: int = 9222, headless: bool = False) -> subprocess.Popen:
    profile = ensure_profile()
    _kill_port(port)
    _patch_prefs(profile)
    proc = subprocess.Popen(chrome_command(get_chrome_path(), profile, port,
                                           headless),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)  # let the debug port open
    return proc


def cleanup(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10)
    else:
        proc.kill()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_apply_chrome.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/career_agent/apply/chrome.py backend/tests/test_apply_chrome.py
git commit -m "feat: real-Chrome CDP lifecycle for the apply agent"
```

---

### Task 4: `run_agent` — the subprocess shell and stream consumption

**Files:**
- Modify: `backend/src/career_agent/apply/agent.py`
- Test: `backend/tests/test_apply_agent.py` (stream consumption only; the spawn itself is untested by convention)

**Interfaces:**
- Consumes: `parse_result` (Task 1), `APPLY_MODEL` (Task 1).
- Produces: `consume_stream(lines: Iterable[str]) -> tuple[str, float]` returning `(joined_text_output, cost_usd)` (pure, tested); `async run_agent(prompt: str, *, job_id: int, cdp_port: int = 9222, timeout_s: int = 300, model: str = APPLY_MODEL) -> AgentResult` (shell). Task 6 injects a fake with `run_agent`'s exact `(prompt, job_id)`-leading shape.

- [ ] **Step 1: Write the failing tests for `consume_stream`**

```python
import json as _json
from career_agent.apply.agent import consume_stream


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_apply_agent.py -k consume -v` — Expected: FAIL.

- [ ] **Step 3: Implement `consume_stream` and `run_agent`**

```python
# add to agent.py
import asyncio
import os
import shutil as _shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

WORK_DIR = Path("data/apply-work")
LOG_DIR = Path("data/logs")


def consume_stream(lines) -> tuple[str, float]:
    """Fold claude's stream-json stdout into (text transcript, cost)."""
    parts, cost = [], 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
            continue
        if msg.get("type") == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name", "").replace("mcp__playwright__", "")
                    parts.append(f"  >> {name}")
        elif msg.get("type") == "result":
            cost = msg.get("total_cost_usd", 0.0) or 0.0
            parts.append(msg.get("result", "") or "")
    return "\n".join(parts), cost


def _mcp_config(cdp_port: int) -> dict:
    return {"mcpServers": {"playwright": {
        "command": "npx",
        "args": ["@playwright/mcp@latest",
                 f"--cdp-endpoint=http://localhost:{cdp_port}",
                 "--viewport-size=1280x800"]}}}


def _run_agent_blocking(prompt, job_id, cdp_port, timeout_s, model) -> AgentResult:
    if not (_shutil.which("claude") and _shutil.which("npx")):
        raise RuntimeError("agentic apply needs the `claude` CLI and `npx` on"
                           " PATH -- see docs/lld-apply-button-v2.md")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    mcp_path = WORK_DIR / ".mcp-apply.json"
    mcp_path.write_text(json.dumps(_mcp_config(cdp_port)), encoding="utf-8")
    session_dir = WORK_DIR / "session"
    if session_dir.exists():
        _shutil.rmtree(session_dir, ignore_errors=True)
    session_dir.mkdir(parents=True)

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    cmd = ["claude", "--model", model, "-p",
           "--mcp-config", str(mcp_path),
           "--permission-mode", "bypassPermissions",
           "--no-session-persistence",
           "--output-format", "stream-json", "--verbose", "-"]

    start = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env,
                            cwd=str(session_dir), shell=False)
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        text, cost = consume_stream(proc.stdout)
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        return AgentResult("failed", "timeout",
                           duration_ms=int((time.time() - start) * 1000))
    finally:
        if proc.poll() is None:
            _kill_tree(proc.pid)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    transcript = LOG_DIR / f"apply_{ts}_job{job_id}.txt"
    transcript.write_text(text, encoding="utf-8")

    result = parse_result(text)
    result.transcript_path = str(transcript)
    result.cost_usd = cost
    result.duration_ms = int((time.time() - start) * 1000)
    return result


def _kill_tree(pid: int) -> None:
    import platform as _platform
    if _platform.system() == "Windows":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


async def run_agent(prompt: str, *, job_id: int, cdp_port: int = 9222,
                    timeout_s: int = 300, model: str = APPLY_MODEL) -> AgentResult:
    """asyncio.to_thread wrapper: the same event-loop rule as
    web/pipeline.py's run_once -- the dashboard must stay responsive."""
    return await asyncio.to_thread(_run_agent_blocking, prompt, job_id,
                                   cdp_port, timeout_s, model)
```

Note the wall-clock caveat: `consume_stream(proc.stdout)` blocks until stdout closes, so
`timeout_s` is enforced by `proc.wait` only after EOF; a truly hung agent holds the
thread until Chrome dies with it. `# ponytail: soft timeout; move to a reader thread
with a hard deadline if hangs show up in practice.` Put that comment in the code.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_apply_agent.py -v` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/career_agent/apply/agent.py backend/tests/test_apply_agent.py
git commit -m "feat: run_agent subprocess shell with stream-json consumption"
```

---

### Task 5: schema columns + `store.qa_all`

**Files:**
- Modify: `backend/src/career_agent/db.py` (the `init_schema` migration block around line 146)
- Modify: `backend/src/career_agent/store.py`
- Test: `backend/tests/test_store.py` (or the file where existing `qa_*` tests live — check `grep -rn qa_lookup backend/tests/`)

**Interfaces:**
- Consumes: existing `_add_column_if_missing(conn, table, column, decl)`.
- Produces: `application.failure_reason TEXT` and `application.transcript_path TEXT` columns; `store.qa_all(conn) -> list[sqlite3.Row]` returning all `qa_bank` rows (each with `question_normalized`, `answer`, `is_volatile`, `last_confirmed_at`). Task 6 depends on both.

- [ ] **Step 1: Write the failing tests**

```python
def test_application_has_taxonomy_columns(conn):  # reuse the suite's conn fixture
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(application)")}
    assert {"failure_reason", "transcript_path"} <= cols


def test_qa_all_returns_every_row(conn):
    from career_agent import store
    store.qa_upsert(conn, "Visa status?", "Citizen", is_volatile=True)
    store.qa_upsert(conn, "Years of Python?", "6", is_volatile=False)
    rows = store.qa_all(conn)
    assert {r["question_normalized"] for r in rows} == \
        {store.normalize_question("Visa status?"),
         store.normalize_question("Years of Python?")}
```

(Match `qa_upsert` / `normalize_question`'s real names and signatures — check
`store.py` first; adjust the test to the actual helper names if they differ.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest backend/tests -k "taxonomy_columns or qa_all" -v` — Expected: FAIL.

- [ ] **Step 3: Implement**

In `db.py`'s migration block (next to the existing `_add_column_if_missing` calls):

```python
    _add_column_if_missing(conn, "application", "failure_reason", "TEXT")
    _add_column_if_missing(conn, "application", "transcript_path", "TEXT")
```

In `store.py`:

```python
def qa_all(conn) -> list:
    return conn.execute(
        "SELECT question_normalized, answer, is_volatile, last_confirmed_at"
        " FROM qa_bank ORDER BY question_normalized").fetchall()
```

- [ ] **Step 4: Run tests to verify they pass**, then **Step 5: Commit**

```bash
git add backend/src/career_agent/db.py backend/src/career_agent/store.py backend/tests
git commit -m "feat: failure-taxonomy columns on application, store.qa_all"
```

---

### Task 6: rewrite `submit()` around the agent

**Files:**
- Modify: `backend/src/career_agent/apply/ats.py`
- Test: `backend/tests/test_apply_ats.py` (rewrite the filler-era tests; keep the state-machine ones)

**Interfaces:**
- Consumes: `agent.build_prompt`, `agent.run_agent`, `agent.AgentResult` (Tasks 1–4); `chrome.launch_chrome` / `chrome.cleanup` (Task 3); `store.qa_all` + new columns (Task 5).
- Produces: `async submit(conn, job_id, dry_run, brief=None, profile=None, resume_version=None, run_agent=None) -> dict` — same return dicts as today (`{"ok", "reason", "needs_answer", "held", "unsupported", "status"}` keys). `run_agent` is the one test seam: `async (prompt: str, job_id: int) -> AgentResult`. Also `PERMANENT_REASONS: set[str]` and `classify_failure(reason: str, prior_failures: int) -> str`. Keeps: `SUBMISSION_IMPLEMENTED`, `MAX_ATTEMPTS`, `BLOCKING`, `sweep_stale_in_flight`, `RESUME_VERSION`, `_confirmed_within_days`, `QA_VOLATILE_WINDOW_DAYS`, `NeedsAnswer` (only for `qa` flows that import it — check `grep -rn NeedsAnswer backend/src`), and drops `compute_answers`/`fill_and_submit` params. Task 8 (worker) relies on these exact semantics.

- [ ] **Step 1: Write the failing tests**

Rewrite `test_apply_ats.py` in the existing suite's fixture style (it already builds a
temp DB and inserts job/resume rows — reuse those fixtures). Core cases:

```python
from career_agent.apply.agent import AgentResult
from career_agent.apply import ats


def fake_agent(result: AgentResult):
    async def _fake(prompt, job_id):
        _fake.prompts.append(prompt)
        return result
    _fake.prompts = []
    return _fake


async def test_draft_inserts_draft_row_with_answers(conn, job_id, brief, profile):
    fake = fake_agent(AgentResult("draft_ready",
                                  answers={"Visa?": "Citizen"},
                                  transcript_path="t.txt"))
    r = await ats.submit(conn, job_id, dry_run=True, brief=brief,
                         profile=profile, run_agent=fake)
    assert r["ok"] and r["status"] == "draft"
    row = conn.execute("SELECT * FROM application WHERE job_id=?",
                       (job_id,)).fetchone()
    assert row["status"] == "draft"
    assert json.loads(row["answers"]) == {"Visa?": "Citizen"}
    assert row["transcript_path"] == "t.txt"


async def test_draft_needs_answer_passthrough(conn, job_id, brief, profile):
    fake = fake_agent(AgentResult("needs_answer", "Do you have a PMP?"))
    r = await ats.submit(conn, job_id, dry_run=True, brief=brief,
                         profile=profile, run_agent=fake)
    assert not r["ok"] and r["needs_answer"] == "Do you have a PMP?"
    assert conn.execute("SELECT COUNT(*) n FROM application").fetchone()["n"] == 0


async def test_draft_captcha_holds_without_application_row(conn, job_id, brief, profile):
    fake = fake_agent(AgentResult("captcha"))
    r = await ats.submit(conn, job_id, dry_run=True, brief=brief,
                         profile=profile, run_agent=fake)
    assert not r["ok"] and r["held"]
    ev = conn.execute("SELECT type FROM event WHERE job_id=?", (job_id,)).fetchall()
    assert any(e["type"] == "captcha_held" for e in ev)


async def test_send_pins_draft_answers_into_prompt(conn, job_id, brief, profile):
    await ats.submit(conn, job_id, dry_run=True, brief=brief, profile=profile,
                     run_agent=fake_agent(AgentResult("draft_ready",
                                                      answers={"Visa?": "Citizen"})))
    fake = fake_agent(AgentResult("applied", answers={"Visa?": "Citizen"}))
    r = await ats.submit(conn, job_id, dry_run=False, brief=brief,
                         profile=profile, run_agent=fake)
    assert r["ok"] and r["status"] == "submitted"
    assert "EXACTLY" in fake.prompts[0] and "Visa?" in fake.prompts[0]
    row = conn.execute("SELECT status FROM application WHERE job_id=?"
                       " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    assert row["status"] == "submitted"


async def test_send_without_draft_runs_auto_mode(conn, job_id, brief, profile):
    fake = fake_agent(AgentResult("applied", answers={"q": "a"}))
    r = await ats.submit(conn, job_id, dry_run=False, brief=brief,
                         profile=profile, run_agent=fake)
    assert r["ok"]
    assert "EXACTLY" not in fake.prompts[0]      # auto mode, nothing pinned
    assert "ANSWERS_JSON" in fake.prompts[0]


async def test_send_refused_by_kill_switch(conn, job_id, brief, profile):
    r = await ats.submit(conn, job_id, dry_run=False, brief=brief, profile=profile)
    assert not r["ok"] and r["unsupported"]      # run_agent=None + flag False


async def test_permanent_failure_writes_failed_permanent(conn, job_id, brief, profile):
    fake = fake_agent(AgentResult("failed", "sso_required"))
    r = await ats.submit(conn, job_id, dry_run=False, brief=brief,
                         profile=profile, run_agent=fake)
    assert not r["ok"]
    row = conn.execute("SELECT status, failure_reason FROM application"
                       " WHERE job_id=? ORDER BY id DESC", (job_id,)).fetchone()
    assert row["status"] == "failed_permanent"
    assert row["failure_reason"] == "sso_required"


async def test_retryable_failure_promotes_at_max_attempts(conn, job_id, brief, profile):
    for _ in range(ats.MAX_ATTEMPTS):
        r = await ats.submit(conn, job_id, dry_run=False, brief=brief,
                             profile=profile,
                             run_agent=fake_agent(AgentResult("failed", "stuck")))
    row = conn.execute("SELECT status FROM application WHERE job_id=?"
                       " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    assert row["status"] == "failed_permanent"


async def test_expired_at_draft_time_is_permanent(conn, job_id, brief, profile):
    fake = fake_agent(AgentResult("expired"))
    await ats.submit(conn, job_id, dry_run=True, brief=brief, profile=profile,
                     run_agent=fake)
    row = conn.execute("SELECT status, failure_reason FROM application"
                       " WHERE job_id=?", (job_id,)).fetchone()
    assert row["status"] == "failed_permanent" and row["failure_reason"] == "expired"


async def test_blocking_status_refuses_new_attempt(conn, job_id, brief, profile):
    conn.execute("INSERT INTO application (job_id, resume_version, status)"
                 " VALUES (?, 'base-v1', 'submitted')", (job_id,))
    r = await ats.submit(conn, job_id, dry_run=True, brief=brief,
                         profile=profile, run_agent=fake_agent(AgentResult("applied")))
    assert not r["ok"] and "already has" in r["reason"]


async def test_missing_profile_raises(conn, job_id, brief):
    with pytest.raises(RuntimeError, match="candidate_profile"):
        await ats.submit(conn, job_id, dry_run=True, brief=brief, profile=None,
                         run_agent=fake_agent(AgentResult("applied")))


def test_classify_failure():
    assert ats.classify_failure("sso_required", 0) == "failed_permanent"
    assert ats.classify_failure("easy_apply", 0) == "failed_permanent"
    assert ats.classify_failure("stuck", 0) == "failed"
    assert ats.classify_failure("stuck", ats.MAX_ATTEMPTS - 1) == "failed_permanent"
```

Also KEEP (unchanged) the existing tests for `sweep_stale_in_flight` and any pure
state-machine tests that don't touch the deleted filler functions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_apply_ats.py -v` — Expected: new tests FAIL (old
`submit` signature/behavior), kept tests PASS.

- [ ] **Step 3: Rewrite `submit()` in `ats.py`**

Shape (translate `AgentResult` codes into today's return dicts and DB writes; every
DB-write pattern below already exists in the current file — keep those INSERT/UPDATE
statements, adding the two new columns):

```python
PERMANENT_REASONS = {
    "expired", "sso_required", "easy_apply", "naukri_platform",
    "not_eligible_location", "already_applied", "not_a_job_application",
    "unsafe_permissions", "unsafe_verification", "site_blocked",
}


def classify_failure(reason: str, prior_failures: int) -> str:
    if reason in PERMANENT_REASONS:
        return "failed_permanent"
    return ("failed_permanent" if prior_failures + 1 >= MAX_ATTEMPTS
            else "failed")


async def _live_run_agent(prompt: str, job_id: int):
    """Default agent runner: real Chrome around a real claude session.
    Tests inject their own run_agent instead; nothing in the test suite
    ever reaches this."""
    from career_agent.apply import chrome as chrome_mod
    from career_agent.apply.agent import run_agent as real_run_agent
    proc = chrome_mod.launch_chrome()
    try:
        return await real_run_agent(prompt, job_id=job_id)
    finally:
        chrome_mod.cleanup(proc)


async def submit(conn, job_id, dry_run, brief=None, profile=None,
                 resume_version=None, run_agent=None) -> dict:
    row = <fetch job row; not-found guard unchanged>
    if not dry_run and run_agent is None and not SUBMISSION_IMPLEMENTED:
        return {"ok": False, "unsupported": True, "reason":
                "The agentic apply engine is built but real sends are not"
                " enabled yet (SUBMISSION_IMPLEMENTED). Apply on the site"
                " yourself, then record the outcome."}
    <BLOCKING-status guard unchanged>
    if profile is None:
        raise RuntimeError("candidate_profile.toml not found -- see"
                           " candidate_profile.toml.example. The apply agent"
                           " needs it for every source.")
    <resolve resume_version + resume row; look up resume text:
     conn.execute("SELECT path, content FROM resume WHERE version = ?") --
     content may be NULL for the base resume; fall back to "">
    qa_rows = store.qa_all(conn)
    score = <latest assessment weighted_score for job_id, may be None>
    runner = run_agent or _live_run_agent

    if dry_run:
        prompt = agent_mod.build_prompt(job, profile, brief, qa_rows,
                                        resume_text, resume_path,
                                        mode="draft", score=score)
        result = await runner(prompt, job_id)
        return _record_draft_outcome(conn, job_id, resume_version, result)

    draft = <latest draft row, unchanged query>
    if draft is not None and draft["answers"]:
        prompt = agent_mod.build_prompt(..., mode="send",
                                        pinned_answers=json.loads(draft["answers"]))
    else:
        prompt = agent_mod.build_prompt(..., mode="auto")
    <INSERT in_flight row, app_id -- unchanged>
    result = await runner(prompt, job_id)
    return _record_send_outcome(conn, job_id, app_id, result)
```

`_record_draft_outcome`: `draft_ready` → INSERT draft row with `answers`,
`transcript_path` → `{"ok": True, "status": "draft"}`; `needs_answer` →
`{"ok": False, "needs_answer": reason, "reason": f"needs an answer: {reason}"}` (no
row); `captcha` → event `captcha_held`, `{"ok": False, "held": True, ...}`;
`expired` → INSERT row `status='failed_permanent'`, `failure_reason='expired'` +
event; `failed`/`login_issue` → INSERT row with `classify_failure(reason,
prior_failed_count)` + `failure_reason` + event, `{"ok": False, "reason": ...}`.

`_record_send_outcome`: `applied` → UPDATE in_flight → `submitted`,
`answers=json.dumps(result.answers or pinned)`, `submitted_at=datetime('now')`,
`transcript_path` + event `submitted` → `{"ok": True, "status": "submitted"}`;
`captcha` → DELETE in_flight row + event `captcha_held` (today's exact behavior);
`needs_answer` → DELETE in_flight row, needs_answer dict; `expired`/`failed`/
`login_issue` → UPDATE in_flight row to `classify_failure(...)` (`expired` maps
straight to `failed_permanent`) + `failure_reason` + event.

Prior-failure count query is the existing one:
`SELECT COUNT(*) n FROM application WHERE job_id = ? AND status = 'failed'`.

Delete in the same edit: `_default_compute_answers`, `_default_fill_and_submit`,
`_generic_stub_compute_answers`, `_read_custom_questions`, `_fill_field`,
`_check_for_captcha`, `_split_name`, `GREENHOUSE_STANDARD_FIELD_SELECTORS`,
`resolve_answers`, `FormField`, `CaptchaEncountered`, and the `is_greenhouse`
routing. Before deleting `NeedsAnswer`, run
`grep -rn "NeedsAnswer\|resolve_answers\|FormField\|CaptchaEncountered" backend/src backend/tests`
and keep whichever names something else still imports (expected: `worker.py` no
longer needs any of them after Task 8; delete test files' references as part of this
task's test rewrite).

- [ ] **Step 4: Run the full suite**

Run: `cd backend && pytest` — Expected: `test_apply_ats.py` and `test_apply_agent.py`
PASS; `test_worker.py` may FAIL where it exercised the old two-call auto flow — note
which cases, they are Task 8's job. Everything else PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/career_agent/apply/ats.py backend/tests/test_apply_ats.py
git commit -m "feat: submit() orchestrates the apply agent; delete the deterministic filler"
```

---

### Task 7: purge remaining dead filler references

**Files:**
- Modify: whatever `grep -rn "resolve_answers\|FormField\|GREENHOUSE_STANDARD\|compute_answers\|fill_and_submit\|CaptchaEncountered" backend/src backend/tests docs/lld-apply-button.md` still finds (expected: stragglers in `tests/test_apply_ats.py` already handled, possibly `web/actions.py`/`web/app.py` passing `compute_answers=`, and `store.qa_lookup` if now unused).

**Interfaces:**
- Consumes: Task 6's final `submit()` signature.
- Produces: a codebase where the only apply-engine entry point is `submit(run_agent=...)`.

- [ ] **Step 1: Grep** with the command above; list every hit.
- [ ] **Step 2: Fix each** — call sites passing removed kwargs drop them; `store.qa_lookup` is deleted ONLY if nothing (including the dashboard answer flow in `web/actions.py`) still calls it; add a one-line note to the top of `docs/lld-apply-button.md`: "Superseded by lld-apply-button-v2.md for the apply engine."
- [ ] **Step 3: Run** `cd backend && pytest` — Expected: no import errors; only Task 8's known worker failures remain.
- [ ] **Step 4: Commit** — `git commit -am "chore: purge dead deterministic-filler references"`

---

### Task 8: `apply_tick` — auto mode becomes one agent run

**Files:**
- Modify: `backend/src/career_agent/web/worker.py:149-189` (the draft/send block)
- Test: `backend/tests/test_worker.py`

**Interfaces:**
- Consumes: Task 6's `submit()` (notably: raises `RuntimeError` on missing profile; returns `needs_answer` from either phase).
- Produces: apply_tick behavior — manual mode: one `submit(dry_run=True)` then stop for review (unchanged); auto mode: one `submit(dry_run=False)` only; needs-answer park unchanged (`current_job_id` stays set) from either call.

- [ ] **Step 1: Update/write the failing tests**

In `test_worker.py`, following its existing fixture style (fake submit injected by
monkeypatching `ats_apply.submit`):

```python
async def test_auto_mode_makes_exactly_one_submit_call(worker_env, monkeypatch):
    calls = []
    async def fake_submit(conn, job_id, dry_run, **kw):
        calls.append(dry_run)
        return {"ok": True, "status": "submitted"}
    monkeypatch.setattr(ats_apply, "submit", fake_submit)
    <set run_state apply running, mode='auto'; seed one queue candidate>
    await apply_tick(conn, brief_path, profile_path)
    assert calls == [False]              # no draft pass in auto mode


async def test_manual_mode_still_drafts_and_parks_for_review(worker_env, monkeypatch):
    calls = []
    async def fake_submit(conn, job_id, dry_run, **kw):
        calls.append(dry_run)
        return {"ok": True, "status": "draft"}
    monkeypatch.setattr(ats_apply, "submit", fake_submit)
    <mode='manual'>
    await apply_tick(conn, brief_path, profile_path)
    assert calls == [True]
    assert get_run_state(conn, "apply")["current_job_id"] is not None


async def test_needs_answer_from_auto_send_parks(worker_env, monkeypatch):
    async def fake_submit(conn, job_id, dry_run, **kw):
        return {"ok": False, "needs_answer": "PMP cert?",
                "reason": "needs an answer: PMP cert?"}
    monkeypatch.setattr(ats_apply, "submit", fake_submit)
    <mode='auto'>
    await apply_tick(conn, brief_path, profile_path)
    assert get_run_state(conn, "apply")["current_job_id"] is not None
    <assert a needs_answer event row was logged>
```

Keep/adjust the existing `test_apply_tick_parks_on_needs_answer` and mode tests to the
new call counts. The existing missing-profile fallback test changes meaning: a missing
profile now surfaces as run_state `status='error'` (the `RuntimeError` path) for every
source — update that test accordingly.

- [ ] **Step 2: Run to verify failures** — `cd backend && pytest tests/test_worker.py -v`

- [ ] **Step 3: Implement** in `apply_tick` (replacing lines 139–189's flow):

```python
    brief = load_brief(brief_path)
    profile = load_candidate_profile(profile_path)   # FileNotFoundError -> the
                                                     # except below turns it into
                                                     # run_state error: the agent
                                                     # needs a profile for every
                                                     # source now
    state_mode = state["mode"]
    try:
        if state_mode == "manual":
            result = await ats_apply.submit(conn, job_id, dry_run=True,
                                            brief=brief, profile=profile,
                                            resume_version=resume_version)
        else:
            result = await ats_apply.submit(conn, job_id, dry_run=False,
                                            brief=brief, profile=profile,
                                            resume_version=resume_version)
    except Exception as exc:
        set_run_state(conn, "apply", status="error", current_job_id=None,
                      last_error=str(exc))
        store.log(conn, job_id, "run_error", str(exc))
        return

    if result.get("needs_answer"):
        store.log(conn, job_id, "needs_answer", result["needs_answer"])
        return                            # park: current_job_id stays set

    if not result["ok"]:
        store.log(conn, job_id, "job_skipped", result.get("reason", ""))
        set_run_state(conn, "apply", current_job_id=None)
        return

    if state_mode == "manual":
        return                            # draft awaiting review, unchanged

    set_run_state(conn, "apply", current_job_id=None)   # auto: done
```

(The `FileNotFoundError → profile=None` fallback block is deleted; the send-phase
`resume_version=store.resume_version_for(...)` lookup moves into the single auto call
— use `resume_version` from `tailor_for_apply`, which is the same value.)

- [ ] **Step 4: Run the FULL suite** — `cd backend && pytest` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/career_agent/web/worker.py backend/tests/test_worker.py
git commit -m "feat: auto mode applies in one agent run; manual keeps draft-review-send"
```

---

### Task 9: docs, suite, and the user-gated live check

**Files:**
- Modify: `CLAUDE.md` (apply-engine paragraph), `README.md` (setup: Chrome + claude CLI + npx requirements replace `playwright install chromium` for the apply path — keep the playwright dep note if other code still imports it: `grep -rn "playwright" backend/src` first)
- Modify: `docs/lld-apply-button-v2.md` — flip the status line from "design, not yet implemented" to implemented-at-commit.

**Interfaces:** none new.

- [ ] **Step 1: Update the docs listed above.** In `CLAUDE.md`, rewrite the "two-phase Greenhouse filler" section to describe the agent engine: submit() → build_prompt/run_agent, `SUBMISSION_IMPLEMENTED` still `False` and still the manual gate, all sources routed, taxonomy columns. Keep it to roughly the same length as the section it replaces.
- [ ] **Step 2: Full suite + a no-network smoke** — `cd backend && pytest`, and `python -c "from career_agent.apply import agent, chrome, ats"` — Expected: PASS, clean import.
- [ ] **Step 3: Commit** — `git commit -am "docs: agentic apply engine (P0) documented"`
- [ ] **Step 4 (USER-GATED — do not run without explicit approval; costs API credits and drives a real browser):** live dry-run verification: with Chrome closed (first profile clone), run the dashboard, set apply mode to `manual`, press Apply on one real queued job, and confirm: Chrome launches with the cloned profile, the agent fills the form, a `draft` application row appears with question→answer JSON and a transcript path, the "Answer needed" card appears if the agent emits NEEDS_ANSWER, and nothing was submitted. Repeat on a second posting from a different source. Report findings; only after this does anyone even discuss flipping `SUBMISSION_IMPLEMENTED`.

---

## Self-Review

- **Spec coverage:** LLD §3 agent.py → Tasks 1, 2, 4; §4 chrome.py → Task 3; §5 submit/flow/classification/schema → Tasks 5, 6; auto-single-pass + worker → Task 8; deletion list → Tasks 6, 7; testing table → each task's tests; §6 safety → kill-switch test (Task 6), flags test (Task 3); live verification → Task 9 (user-gated). Deferred items (§8) intentionally have no tasks.
- **Placeholder scan:** prompt prose in Task 2 and DB-write bodies in Task 6 are specified by contract + pointer to the spec's section tables and to existing statements in the current `ats.py` — the executor has exact structure, exact sentinels, and exact DB statements to keep. No TBDs.
- **Type consistency:** `AgentResult` fields and codes match across Tasks 1/4/6; `run_agent(prompt, job_id)` fake shape matches `_live_run_agent` and `runner` calls; `build_prompt` signature identical in Tasks 2 and 6; column names `failure_reason`/`transcript_path` consistent across Tasks 5/6.
