"""GET/PUT /api/profile: the candidate_profile.toml editor (Task 17's half
of slice S6). Same deferred-import rule as api.py/api_chat.py, and
deliberately NOT mounted in app.py -- Task 18 wires routers together."""
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


@router.get("/profile")
def api_get_profile():
    path = _app().CANDIDATE_PROFILE_PATH
    if not path.exists():
        return {"exists": False, "profile": _empty_profile()}
    return {"exists": True, "profile": load_candidate_profile(path).model_dump()}


@router.put("/profile")
def api_put_profile(body: dict = Body(...)):
    try:
        profile = CandidateProfile(**_strip(body))
    except ValidationError as exc:
        errors = {".".join(str(p) for p in err["loc"]): err["msg"]
                  for err in exc.errors()}
        return JSONResponse(status_code=422, content={"ok": False, "errors": errors})
    save_candidate_profile(_app().CANDIDATE_PROFILE_PATH, profile)
    return {"ok": True, "message": "Profile saved"}
