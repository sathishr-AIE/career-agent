"""The backend -- never the agent -- types a password into the browser.

A job page is untrusted: it can prompt-inject the agent, including making it
lie about which page it is on. So the LLM must never hold a secret.
fill_password attaches to the apply Chrome over CDP (apply/chrome.py, port
9222) and fills only frames whose REAL url is on the credential's domain.

Sync Playwright API on purpose: the callers are AgentRun's reader thread
(ats._chat_events) and FastAPI's threadpool (api_chat's answer route is a
plain def). Neither has a running asyncio loop, which is the one place the
sync API refuses to run."""
from contextlib import contextmanager

from career_agent import credentials

CDP_URL = "http://127.0.0.1:9222"
_PASSWORD_INPUT = "input[type=password]"


@contextmanager
def _live_connect(cdp_url: str):
    """Attach to the running Chrome. Leaving stops the Playwright driver, which
    drops the CDP connection without touching Chrome or its pages --
    browser.close() is deliberately not called: on a connected browser it also
    "clears all created contexts" (Playwright's Browser.close docs)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p.chromium.connect_over_cdp(cdp_url)


def _pages(browser) -> list:
    return [page for ctx in browser.contexts for page in ctx.pages]


def page_urls(*, cdp_url: str = CDP_URL, connect=None) -> list[str]:
    """The browser's real page urls -- never what the agent says it is on."""
    with (connect or _live_connect)(cdp_url) as browser:
        return [page.url for page in _pages(browser)]


def fill_password(domain: str, password: str, *, cdp_url: str = CDP_URL,
                  connect=None) -> dict:
    """Fill every visible, empty password input (a sign-up's confirm field
    too) in frames whose real url host_matches `domain`. Returns
    {"filled": n, "page_url": url} or {"filled": 0, "pages": [real page urls]};
    never logs or returns the password. `connect` (cdp_url -> context manager
    yielding a browser) is the test seam."""
    with (connect or _live_connect)(cdp_url) as browser:
        pages = _pages(browser)
        filled, page_url = 0, None
        for page in pages:
            for frame in page.frames:
                if not credentials.host_matches(frame.url, domain):
                    continue
                fields = frame.locator(_PASSWORD_INPUT)
                for i in range(fields.count()):
                    field = fields.nth(i)
                    if field.is_visible() and not field.input_value():
                        field.fill(password)
                        filled += 1
                        page_url = page_url or frame.url
        if filled:
            return {"filled": filled, "page_url": page_url}
        return {"filled": 0, "pages": [page.url for page in pages]}
