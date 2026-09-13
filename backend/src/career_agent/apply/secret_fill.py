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
(ats._chat_events) and FastAPI's threadpool (api_chat's answer route is a plain
def). Neither has a running asyncio loop, the one place the sync API refuses."""
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


def _pages(browser) -> list:
    return [page for ctx in browser.contexts for page in ctx.pages]


def _fillable(url: str, domain: str) -> bool:
    return credentials.secure_url(url) and credentials.host_matches(url, domain)


def _attached(handle) -> bool:
    try:
        return bool(handle.evaluate(_CONNECTED))
    except Exception:
        return False            # a navigation destroyed its execution context


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


def _submit_and_wait(page, frame, handles, wait_s: float) -> None:
    """requestSubmit() on the last filled field's form, or Enter when it has
    none; then wait (bounded) for a navigation or the fields to go away."""
    url, last = frame.url, handles[-1]
    if not last.evaluate(_SUBMIT_FORM):
        last.press("Enter", timeout=FIELD_TIMEOUT_MS)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if frame.url != url or not all(_attached(h) for h in handles):
            return
        page.wait_for_timeout(250)


def page_urls(*, cdp_url: str = CDP_URL, connect=None) -> list[str]:
    """The browser's real page urls (display form) -- never what the agent says."""
    with (connect or _live_connect)(cdp_url) as browser:
        return [display_url(page.url) for page in _pages(browser)]


def fill_and_submit(domain: str, password: str, *, cdp_url: str = CDP_URL,
                    connect=None, wait_s: float | None = None,
                    after_submit=None) -> dict:
    """Fill every visible, empty password input (a sign-up's confirm field
    too) in the first https frame on `domain` that has one, submit its form,
    run `after_submit` (the caller's store), then clear what is left -- the
    clear runs even when the submit or after_submit raises.

    Returns {"submitted": True, "cleared": bool, "page_url": display url} or
    {"submitted": False, "pages": [display urls]}; never logs or returns the
    password. A frame that navigates away mid-fill is cleared, never
    submitted. `connect` (cdp_url -> context manager yielding a browser) is
    the test seam."""
    wait_s = SUBMIT_WAIT_S if wait_s is None else wait_s
    with (connect or _live_connect)(cdp_url) as browser:
        pages = _pages(browser)
        for page in pages:
            for frame in page.frames:
                if not _fillable(frame.url, domain):
                    continue
                handles = [h for h in frame.query_selector_all(_PASSWORD_INPUT)
                           if h.is_visible() and not h.input_value()]
                if not handles:
                    continue
                filled, submitted = [], False
                try:
                    for handle in handles:
                        if not _fillable(frame.url, domain):    # navigated since the check
                            break
                        handle.fill(password, timeout=FIELD_TIMEOUT_MS)
                        filled.append(handle)
                    if filled and len(filled) == len(handles):
                        page_url = display_url(frame.url)
                        _submit_and_wait(page, frame, filled, wait_s)
                        if after_submit is not None:
                            after_submit()
                        submitted = True
                finally:
                    cleared = _clear(filled)
                if submitted:
                    return {"submitted": True, "cleared": cleared, "page_url": page_url}
        return {"submitted": False, "pages": [display_url(p.url) for p in pages]}
