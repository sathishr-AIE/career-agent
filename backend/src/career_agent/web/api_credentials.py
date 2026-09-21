"""GET/POST/DELETE /api/logins: the Logins page. Metadata only --
credentials.list_ never returns a password or its ciphertext, and POST's
password is write-only. Same deferred-import rule as api_memory.py. Mounted
in app.py."""
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from career_agent import credentials
from career_agent.security import CredentialKeyError

router = APIRouter(prefix="/api")


def _app():
    from career_agent.web import app as app_module
    return app_module


@router.get("/logins")
def api_get_logins():
    return {"items": credentials.list_(_app()._conn())}


@router.post("/logins")
def api_add_login(body: dict = Body(...)):
    """LG1: save a login the user already has. The password goes straight to
    credentials.put (encrypted) and is never returned, logged or shown again."""
    errors: dict[str, str] = {}
    domain = None
    try:
        # put() only normalizes a created_by="user" domain; hand-entered logins
        # get the same refusals as the agent path (bare labels, IPs, shared ATS hosts).
        domain = credentials.account_domain(str(body.get("domain") or ""))
    except ValueError as exc:
        # Shown verbatim under Domain, so it has to read as a sentence. Every
        # account_domain refusal -- bare label, IP, shared suffix, wildcard,
        # shared ATS host, non-tenant Workday -- has the same answer.
        reason = str(exc)
        errors["domain"] = (f"{reason[:1].upper()}{reason[1:]}. Use the company's own careers"
                            " domain — a shared job-board host or a bare name can't hold one login.")
    login_url = str(body.get("login_url") or "").strip()
    if login_url and not (login_url.lower().startswith("https://") and domain
                          and credentials.host_matches(login_url, domain)):
        errors["login_url"] = f"Sign-in page must be an https URL on {domain or 'the site'}"
    email = str(body.get("email") or "").strip()
    if not email:
        errors["email"] = "Email is required"
    password = body.get("password")
    if not isinstance(password, str) or not password.strip():
        errors["password"] = "Password is required"
    if errors:
        return JSONResponse(status_code=422, content={
            "ok": False, "message": "; ".join(errors.values()), "errors": errors})

    conn = _app()._conn()
    existing = next((r for r in credentials.list_(conn) if r["domain"] == domain), None)
    if existing and body.get("replace") is not True:
        # put() upserts by domain, so replacing is only ever an explicit choice.
        return JSONResponse(status_code=409, content={
            "ok": False, "exists": True, "created_by": existing["created_by"],
            "message": f"A login for {domain} already exists"})
    try:
        login_id = credentials.put(conn, domain, login_url, email, password, "user")
    except CredentialKeyError:
        # encrypt() runs before any write, so nothing was saved.
        return JSONResponse(status_code=409, content={
            "ok": False, "message": "Saving logins needs CREDENTIAL_KEY in .env — nothing was saved"})
    item = next(r for r in credentials.list_(conn) if r["id"] == login_id)
    return {"ok": True, "message": f"Login saved for {domain}", "item": item}


@router.delete("/logins/{login_id}")
def api_delete_login(login_id: int):
    if not credentials.delete(_app()._conn(), login_id):
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": "No such login"})
    return {"ok": True, "message": "Deleted"}
