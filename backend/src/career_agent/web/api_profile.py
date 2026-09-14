"""GET/PUT /api/profile: the candidate_profile.toml editor (slice
S6). Same deferred-import rule as api.py/api_chat.py. Mounted in app.py."""
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from career_agent.config import (Address, CandidateProfile,
                                 load_candidate_profile,
                                 save_candidate_profile)

router = APIRouter(prefix="/api")


def _app():
    from career_agent.web import app as app_module
    return app_module


def _empty_profile() -> dict:
    """Same shape as CandidateProfile.model_dump(), but the required
    string fields (candidate_name/email/phone carry Field(min_length=1))
    are "" rather than absent -- there's no valid CandidateProfile instance
    to dump when no file exists yet."""
    return {
        "candidate_name": "", "candidate_email": "", "candidate_phone": "",
        "linkedin_url": None, "portfolio_url": None, "gender": "decline",
        "address": Address().model_dump(),
        "work_history": [], "education": [],
    }


def _strip(value):
    """Trim whitespace on every string in the body, recursively -- form
    inputs routinely carry leading/trailing spaces from copy-paste."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items()}
    return value


def _clear_end_when_current(body: dict) -> None:
    """Defence in depth: Profile.tsx already clears End when "currently work
    here" is checked, but another client (or a hand-edited request) must not
    be able to save a stale end date that _profile_section would render
    instead of the "-present" it means to say."""
    for w in body.get("work_history") or []:
        if isinstance(w, dict) and w.get("current"):
            w["end"] = ""


def _reject_blank_rows(body: dict) -> dict:
    """A fully- or partially-blank row still validates against the pydantic
    model (WorkEntry.company/EduEntry.institution are required strings, but
    "" satisfies `str`) and would otherwise reach the apply agent's prompt as
    a real, garbled row. Same dotted-path {field: message} shape as a
    pydantic error, so the frontend's error lookup doesn't need to branch on
    where a message came from."""
    errors: dict[str, str] = {}
    for i, w in enumerate(body.get("work_history") or []):
        if not isinstance(w, dict):
            continue
        if not w.get("company"):
            errors[f"work_history.{i}.company"] = "Company is required"
        if not w.get("title"):
            errors[f"work_history.{i}.title"] = "Title is required"
    for i, e in enumerate(body.get("education") or []):
        if not isinstance(e, dict):
            continue
        if not e.get("institution"):
            errors[f"education.{i}.institution"] = "Institution is required"
    return errors


@router.get("/profile")
def api_get_profile():
    path = _app().CANDIDATE_PROFILE_PATH
    if not path.exists():
        return {"exists": False, "profile": _empty_profile()}
    return {"exists": True, "profile": load_candidate_profile(path).model_dump()}


@router.put("/profile")
def api_put_profile(body: dict = Body(...)):
    body = _strip(body)
    _clear_end_when_current(body)

    # Both checks run and their errors merge (setdefault: a blank-row
    # message wins over a pydantic one at the same dotted path) rather than
    # short-circuiting on the blank-row check -- a request that's wrong in
    # two places (e.g. a blank name AND a blank work row) must report both,
    # matching PUT /api/settings' validate-everything-before-writing-anything
    # rule instead of dribbling errors out one submit at a time.
    errors = _reject_blank_rows(body)
    profile = None
    try:
        profile = CandidateProfile(**body)
    except ValidationError as exc:
        for err in exc.errors():
            errors.setdefault(".".join(str(p) for p in err["loc"]), err["msg"])

    if errors:
        return JSONResponse(status_code=422, content={"ok": False, "errors": errors})

    save_candidate_profile(_app().CANDIDATE_PROFILE_PATH, profile)
    return {"ok": True, "message": "Profile saved"}
