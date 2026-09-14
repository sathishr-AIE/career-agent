"""The backend -- never the agent -- types a password into the browser, submits
the form, and clears the field, all in one CDP session while the agent is
blocked waiting on its ANSWER.

A job page is untrusted: it can prompt-inject the agent, including making it
lie about where it is. And a value left in a field reaches the agent anyway:
@playwright/mcp 0.0.80's snapshot renders a password input as a textbox whose
child is its value, and click/navigate responses attach a snapshot. So the LLM
never holds the secret, and never gets a turn while one sits in the page.

fill_and_submit attaches to the apply Chrome over CDP (apply/chrome.py, port
9222) and fills only https frames whose REAL url is on the credential's domain,
re-checked before every fill.

Sync Playwright API on purpose: the callers are AgentRun's reader thread
(ats._chat_events) and the answer route's worker thread (non-Home answers run
through run_in_threadpool). Neither has a running asyncio loop, the one
place the sync API refuses -- _require_no_running_loop makes a regression fail
loudly instead of stalling the server."""
import asyncio
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from career_agent import credentials

CDP_URL = "http://127.0.0.1:9222"
CONNECT_TIMEOUT_MS = 5000
FIELD_TIMEOUT_MS = 2000
SUBMIT_WAIT_S = 10          # for the post-submit navigation or DOM change
_PASSWORD_INPUT = "input[type=password]"
_SUBMIT_FORM = "e => { if (!e.form) return false; e.form.requestSubmit(); return true }"
_CONNECTED = "e => e.isConnected"
_FORM_INDEX = "e => e.form ? Array.prototype.indexOf.call(document.forms, e.form) : -1"
SCRUB_SETTLE_MS = 500       # an SPA re-render after a failed login lands within this


@contextmanager
def _live_connect(cdp_url: str):
    """Attach to the running Chrome. Leaving stops the Playwright driver, which
    drops the CDP connection without touching Chrome or its pages --
    browser.close() is deliberately not called: on a connected browser it also
    "clears all created contexts" (Playwright's Browser.close docs)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p.chromium.connect_over_cdp(cdp_url, timeout=CONNECT_TIMEOUT_MS)


def display_url(url: str) -> str:
    """scheme://host[:port]/path -- no userinfo, query or fragment, so a
    redirect's token never reaches chat or an error body."""
    try:
        parts = urlsplit(url)
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return ""
    if not parts.netloc:
        return f"{parts.scheme}:{parts.path}" if parts.scheme else parts.path
    return f"{parts.scheme}://{parts.hostname or ''}{port}{parts.path}"


def _require_no_running_loop() -> None:
    """Fail loudly on an asyncio event loop: Playwright's sync API raises there
    anyway, and a blocking CDP fill would stall the whole server. Callers run
    this from a worker thread (the runner's reader thread, run_in_threadpool)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("secret_fill must not run on an asyncio event loop -- "
                       "call it from a worker thread (e.g. run_in_threadpool)")


def _pages(browser) -> list:
    return [page for ctx in browser.contexts for page in ctx.pages]


def _fillable(url: str, domain: str) -> bool:
    return credentials.secure_url(url) and credentials.host_matches(url, domain)


def _attached(handle) -> bool:
    try:
        return bool(handle.evaluate(_CONNECTED))
    except Exception:
        return False            # a navigation destroyed its execution context


def _try_clear(handle) -> None:
    try:
        handle.fill("", timeout=FIELD_TIMEOUT_MS)
    except Exception:
        pass                    # detached meanwhile; the re-scan decides


def _clear(handles) -> bool:
    """Empty every filled field still on the page. True when none is left
    holding a value."""
    clean = True
    for handle in handles:
        try:
            if _attached(handle) and handle.input_value():
                handle.fill("", timeout=FIELD_TIMEOUT_MS)
        except Exception:
            clean = False
    return clean


def _holds(handle, password: str) -> bool:
    try:
        return handle.input_value() == password
    except Exception:
        return False


def _scrub(pages, page, domain: str, password: str) -> bool:
    """I-2: a failed login's SPA re-render can put the password back into a NEW
    input (from framework state) that _clear's old handles never see -- and
    the agent's next snapshot would show it. The backend knows the exact
    value: clear every input holding it on the domain's frames (type=text
    included), wait SCRUB_SETTLE_MS, and look once more. True when clean."""
    def holders():
        return [h for p in pages for frame in p.frames
                if credentials.host_matches(frame.url, domain)
                for h in frame.query_selector_all("input") if _holds(h, password)]
    for handle in holders():
        _try_clear(handle)
    page.wait_for_timeout(SCRUB_SETTLE_MS)
    stubborn = holders()
    for handle in stubborn:
        _try_clear(handle)
    return not stubborn


def _password_groups(pages, domain: str) -> list:
    """(page, frame, handles) per owning form -- a field with no form is its
    own group -- for visible, empty password inputs in https frames on the
    domain."""
    groups: dict = {}
    for page in pages:
        for frame in page.frames:
            if not _fillable(frame.url, domain):
                continue
            for handle in frame.query_selector_all(_PASSWORD_INPUT):
                if not handle.is_visible() or handle.input_value():
                    continue
                form = handle.evaluate(_FORM_INDEX)
                key = (id(frame), form) if form >= 0 else (id(frame), "solo", id(handle))
                groups.setdefault(key, (page, frame, []))[2].append(handle)
    return list(groups.values())


def _submit_and_wait(page, frame, handles, wait_s: float) -> bool:
    """requestSubmit() on the last filled field's form, or Enter when it has
    none. True only once the frame navigates or the fields detach (checked at
    least once, then polled until wait_s) -- M-3: nothing else proves a submit."""
    url, last = frame.url, handles[-1]
    if not last.evaluate(_SUBMIT_FORM):
        last.press("Enter", timeout=FIELD_TIMEOUT_MS)
    deadline = time.monotonic() + wait_s
    while True:
        if frame.url != url or not all(_attached(h) for h in handles):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


def page_urls(*, cdp_url: str = CDP_URL, connect=None) -> list[str]:
    """The browser's real page urls (display form) -- never what the agent says."""
    _require_no_running_loop()
    with (connect or _live_connect)(cdp_url) as browser:
        return [display_url(page.url) for page in _pages(browser)]


def fill_and_submit(domain: str, password: str, *, cdp_url: str = CDP_URL,
                    connect=None, wait_s: float | None = None, max_fields: int = 2,
                    after_submit=None) -> dict:
    """Fill the one form's visible, empty password inputs (1..max_fields: 1
    for a sign-in, up to 2 for a sign-up's confirm) in an https frame on
    `domain`, submit it, clear and scrub the value from the page, and only
    after a proven, clean submit run `after_submit` (the caller's store).

    Returns {"submitted": True, "cleared": True, "page_url": display url} or
    {"submitted": False, "reason": ..., "pages": [display urls]}, reason one of
    no_field, ambiguous_form (I-3: a page with both a sign-in and a register
    form), navigated, no_submit, value_persists. Never logs or returns the
    password. `connect` (cdp_url -> context manager yielding a browser) is the
    test seam."""
    _require_no_running_loop()
    wait_s = SUBMIT_WAIT_S if wait_s is None else wait_s
    with (connect or _live_connect)(cdp_url) as browser:
        pages = _pages(browser)

        def refused(reason: str) -> dict:
            return {"submitted": False, "reason": reason,
                    "pages": [display_url(p.url) for p in pages]}

        qualifying = [g for g in _password_groups(pages, domain)
                      if 1 <= len(g[2]) <= max_fields]
        if len(qualifying) != 1:
            return refused("ambiguous_form" if qualifying else "no_field")
        page, frame, handles = qualifying[0]
        filled, reason = [], None
        try:
            for handle in handles:
                if not _fillable(frame.url, domain):        # navigated since the check
                    reason = "navigated"
                    break
                handle.fill(password, timeout=FIELD_TIMEOUT_MS)
                filled.append(handle)
            if reason is None:
                page_url = display_url(frame.url)
                if not _submit_and_wait(page, frame, filled, wait_s):
                    reason = "no_submit"
        finally:
            cleared = _clear(filled)
            clean = _scrub(pages, page, domain, password) if filled else True
        if reason is None and not clean:
            reason = "value_persists"
        if reason:
            return refused(reason)
        if after_submit is not None:
            after_submit()
        return {"submitted": True, "cleared": cleared and clean, "page_url": page_url}
