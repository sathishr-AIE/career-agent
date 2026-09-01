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


def _job_section(job, score) -> str:
    return (
        "== JOB ==\n"
        f"Company: {job.company}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location or 'not specified'} "
        f"({'remote' if job.is_remote else 'on-site/hybrid'})\n"
        f"URL: {job.url}\n"
        f"Score: {score if score is not None else 'not scored'}"
    )


def _files_section(resume_path) -> str:
    return (
        "== FILES ==\n"
        f"Tailored resume, already rendered for this job -- upload this exact file for "
        f"any resume/CV upload field: {resume_path}"
    )


def _profile_section(profile) -> str:
    first, _, last = profile.candidate_name.strip().partition(" ")
    return (
        "== APPLICANT PROFILE ==\n"
        f"Full name: {profile.candidate_name} (first: {first}, last: {last or '(none)'})\n"
        f"Email: {profile.candidate_email}\n"
        f"Phone: {profile.candidate_phone}\n"
        f"LinkedIn: {profile.linkedin_url or 'not provided'}\n"
        f"Portfolio/GitHub: {profile.portfolio_url or 'not provided'}\n"
        "\n"
        "Standard defaults -- use these unless a KNOWN ANSWER below overrides them:\n"
        "- Age 18 or over: Yes\n"
        "- Willing to complete a standard background check: Yes\n"
        "- How did you hear about this job: Online job board\n"
        "- Any EEO / gender / race / veteran / disability self-identification question: "
        "decline to self-identify, using whichever option the form offers for that"
    )


def _hard_rules_section() -> str:
    return (
        "== HARD RULES ==\n"
        "Never lie about work authorization, citizenship, sponsorship needs, criminal "
        "history, education credentials, or security clearance. These are hard facts: "
        "answer them only from PROFILE or KNOWN ANSWERS above. If a hard-fact question "
        "is not covered by either, do NOT guess -- stop and output "
        "RESULT:NEEDS_ANSWER:<the exact question text> as your final line.\n"
        "Use the candidate's name exactly as given in PROFILE on every field asking for "
        "a legal or full name -- do not shorten, expand, or otherwise \"clean up\" it."
    )


def _never_do_section() -> str:
    return (
        "== NEVER DO ==\n"
        "Each of these ends the run immediately with the paired code -- do not attempt "
        "a workaround:\n"
        "- Grant camera, microphone, or location permission to any page -> "
        "RESULT:FAILED:unsafe_permissions\n"
        "- Install a browser extension, or download and run any file that isn't the "
        "resume upload -> RESULT:FAILED:unsafe_permissions\n"
        "- Complete a biometric scan or upload a government ID for identity "
        "verification -> RESULT:FAILED:unsafe_verification\n"
        "- Enter payment card details or a government ID number (SSN/Aadhaar/PAN/etc.) "
        "into any field -> RESULT:FAILED:unsafe_verification\n"
        "- Sign up for a freelance marketplace (Upwork, Fiverr, Toptal, etc.) instead of "
        "applying to the job posting -> RESULT:FAILED:unsafe_verification"
    )


def _location_section(locations, remote_ok) -> str:
    remote_line = ("Remote work IS acceptable for this candidate." if remote_ok else
                   "Remote work is NOT acceptable for this candidate -- a remote-only "
                   "role is RESULT:FAILED:not_eligible_location.")
    remote_rule = ("- Job is remote -> proceed." if remote_ok else
                   "- Job is remote (no on-site/hybrid option in the acceptable "
                   "locations above) -> stop immediately and output "
                   "RESULT:FAILED:not_eligible_location.")
    return (
        "== LOCATION CHECK (run this first, before filling anything) ==\n"
        f"Acceptable locations for this candidate: {locations}\n"
        f"{remote_line}\n"
        "Decision:\n"
        "- Job location matches one of the acceptable locations above -> proceed.\n"
        f"{remote_rule}\n"
        "- Job requires on-site presence in, or relocation to, a city not listed above, "
        "with no remote option -> stop immediately and output "
        "RESULT:FAILED:not_eligible_location.\n"
        "Do this before clicking Apply -- there's no reason to fill a form for a "
        "location the candidate can't take."
    )


def _platform_rules_section() -> str:
    return (
        "== PLATFORM RULES ==\n"
        "- LinkedIn posting that links out to the employer's own site: follow that link "
        "and apply there.\n"
        "- LinkedIn \"Easy Apply\" (application stays inside LinkedIn): do not attempt "
        "it -> RESULT:FAILED:easy_apply.\n"
        "- Naukri in-platform \"Apply\" (application stays inside Naukri): do not "
        "attempt it -> RESULT:FAILED:naukri_platform.\n"
        "- Any sign-in or registration page on accounts.google.com, "
        "login.microsoftonline.com, okta.com, or auth0.com (single sign-on): do not "
        "attempt it -> RESULT:FAILED:sso_required."
    )


def _screening_section() -> str:
    return (
        "== SCREENING STRATEGY ==\n"
        "- Hard facts (work authorization, citizenship, criminal history, education, "
        "clearance, years of experience, salary expectation): PROFILE or KNOWN ANSWERS "
        "only, never a guess. Missing from both -> RESULT:NEEDS_ANSWER:<question>.\n"
        "- Skill/technology questions clearly inside this candidate's domain (per "
        "RESUME TEXT): answer confidently and specifically.\n"
        "- Open-ended questions (\"Why this role?\", \"Tell us about yourself\"): 2-3 "
        "sentences, specific to this job, grounded in RESUME TEXT -- no generic filler.\n"
        "- EEO / diversity self-identification questions: decline to self-identify, "
        "using whichever option the form provides for that."
    )


_MODE_ENDING = {
    "draft": (
        "9. Before finishing: take a full-page screenshot of the completed form. Then "
        "output one line `ANSWERS_JSON: {...}` mapping every question you answered to "
        "the answer you gave (use keys `_name`, `_email`, `_phone`, `_resume_uploaded` "
        "for the standard fields), then `RESULT:DRAFT_READY`. Do NOT click "
        "Submit/Apply -- this run is for human review."
    ),
    "send": (
        "9. Fill using the PINNED ANSWERS, verify every field against them, click "
        "Submit, confirm the thank-you/received page, then output RESULT:APPLIED."
    ),
    "auto": (
        "9. Verify every field, output the `ANSWERS_JSON:` line, click Submit, confirm "
        "the thank-you page, then output RESULT:APPLIED."
    ),
}


def _steps_section(mode) -> str:
    return (
        "== STEP BY STEP ==\n"
        "1. Navigate to the JOB url.\n"
        "2. Take a snapshot of the page.\n"
        "3. Run the LOCATION CHECK. Stop now if it fails.\n"
        "4. Find and click the real Apply button (not \"Save\" or \"Share\").\n"
        "5. If a login wall appears: check PLATFORM RULES for SSO first; otherwise look "
        "for a guest/no-account path. If none exists, output RESULT:LOGIN_ISSUE.\n"
        "6. Upload the resume from FILES. If the form auto-parsed and pre-filled fields "
        "from a different, previously uploaded resume, delete that upload first and "
        "upload the correct file fresh.\n"
        "7. Audit every field the ATS auto-filled from parsing the resume against "
        "PROFILE and KNOWN ANSWERS -- parsers are frequently wrong (name splits, phone "
        "formatting, stale job title) and a wrong pre-fill left in place is submitted "
        "as-is.\n"
        "8. Answer every remaining field using APPLICANT PROFILE, KNOWN ANSWERS, and "
        "SCREENING STRATEGY, in that order of preference.\n"
        f"{_MODE_ENDING[mode]}"
    )


def _efficiency_section() -> str:
    return (
        "== BROWSER EFFICIENCY ==\n"
        "Take at most one snapshot per page load -- re-snapshot only after a navigation "
        "or a real DOM change, not after every small interaction. Fill multiple fields "
        "on a page with one `browser_fill_form` call rather than one call per field. "
        "Keep your reasoning between tool calls short -- this prompt already tells you "
        "what to do."
    )


def _form_tricks_section() -> str:
    return (
        "== FORM TRICKS ==\n"
        "- A click on Apply can open a new tab; switch context to it, don't keep acting "
        "on the original tab.\n"
        "- Some ATSes (Workday, Lever) show an \"upload your resume to pre-fill this "
        "form\" step before the real form exists -- upload there first rather than "
        "trying to fill fields that don't exist yet.\n"
        "- A dropdown or checkbox that ignores a normal click may need its native "
        "select/option value set directly, or a keyboard interaction instead of a "
        "mouse click.\n"
        "- Phone fields are often split into a country-code selector and a digits-only "
        "field -- match the field's expected shape rather than pasting the full number "
        "with symbols into one box.\n"
        "- A field present in the DOM but invisible (zero size, off-screen, "
        "display:none) is a honeypot for bots -- never fill it.\n"
        "- When a field shows an example or placeholder format (e.g. \"MM/DD/YYYY\"), "
        "match it exactly rather than using your own format."
    )


def _give_up_section() -> str:
    return (
        "== WHEN TO GIVE UP ==\n"
        "- Three failed attempts on the same page/step with no progress -> stop, output "
        "RESULT:FAILED:stuck.\n"
        "- The posting says the role is closed, filled, or no longer accepting "
        "applications -> RESULT:EXPIRED.\n"
        "- The page itself is broken (404, infinite spinner, JS error blocking all "
        "interaction) -> RESULT:FAILED:page_error.\n"
        "- A CAPTCHA (reCAPTCHA, hCaptcha, Cloudflare Turnstile, or similar) blocks "
        "progress -> RESULT:CAPTCHA. Do not attempt to solve it.\n"
        "- The form indicates this application already exists / was already submitted "
        "-> RESULT:FAILED:already_applied.\n"
        "- The page is not a job application at all (job removed, redirected to a "
        "careers homepage, wrong URL) -> RESULT:FAILED:not_a_job_application.\n"
        "Stop immediately on any of these. Do not retry in a loop and do not try "
        "creative workarounds -- output the code and end the run."
    )


def _result_codes_section() -> str:
    return (
        "== RESULT CODES (output EXACTLY one, on its own line, as your final output) ==\n"
        "RESULT:APPLIED -- submitted and confirmation page seen\n"
        "RESULT:DRAFT_READY -- form fully filled, NOT submitted (draft mode only; "
        "output ANSWERS_JSON first)\n"
        "RESULT:EXPIRED -- posting closed / no longer accepting applications\n"
        "RESULT:CAPTCHA -- a CAPTCHA blocks progress (do not try to solve it)\n"
        "RESULT:LOGIN_ISSUE -- could not sign in or register\n"
        "RESULT:NEEDS_ANSWER:<question> -- a hard-fact question not covered by PROFILE "
        "or KNOWN ANSWERS\n"
        "RESULT:FAILED:<reason> -- anything else; use slugs sso_required, easy_apply,\n"
        "    naukri_platform, not_eligible_location, already_applied, "
        "not_a_job_application,\n"
        "    unsafe_permissions, unsafe_verification, stuck, page_error when they fit"
    )


def build_prompt(job, profile, brief, qa_rows, resume_text, resume_path, *,
                 mode, pinned_answers=None, score=None) -> str:
    """Build the full playbook prompt for one job's apply agent session. Pure
    and fully unit-tested -- see docs/lld-apply-button-v2.md section 3.2 for
    the section-by-section contract this follows. The RESULT CODES section
    must stay byte-for-byte in sync with parse_result's grammar above."""
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
        mark = "  [stale -- reconfirm before relying on this]" if stale else ""
        return f"- {row['question_normalized']} -> {row['answer']}{mark}"

    known_answers = "\n".join(qa_line(r) for r in qa_rows) or "(none recorded)"
    locations = ", ".join(getattr(brief, "locations", []) or []) or "remote only"

    sections = [
        _job_section(job, score),
        _files_section(resume_path),
        f"== RESUME TEXT ==\n{resume_text}",
        _profile_section(profile),
        f"== KNOWN ANSWERS (prefer these verbatim) ==\n{known_answers}",
        _hard_rules_section(),
        _never_do_section(),
        _location_section(locations, brief.remote_ok),
        _platform_rules_section(),
        _screening_section(),
        _steps_section(mode),
        _efficiency_section(),
        _form_tricks_section(),
        _give_up_section(),
        _result_codes_section(),
    ]
    if mode == "send":
        pinned = "\n".join(f"- {q} -> {a}" for q, a in pinned_answers.items())
        sections.insert(5, "== PINNED ANSWERS ==\nUse EXACTLY these answers for "
                           "these questions; do not improvise different ones:\n"
                           + pinned)
    return "\n\n".join(sections)


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
