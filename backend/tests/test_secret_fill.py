"""secret_fill: the backend -- never the agent -- fills a password, submits
the form, and clears the field, all in one CDP session while the agent waits
(a filled value would otherwise reach the agent through browser_snapshot).
Fake CDP pages only; tests/conftest.py fails any real connect_over_cdp."""
import contextlib

import pytest

from career_agent.apply import secret_fill


class Field:
    """An ElementHandle for one password input."""
    def __init__(self, visible=True, value="", fail_clear=False):
        self.visible, self.value, self.fail_clear = visible, value, fail_clear
        self.attached, self.frame, self.fills = True, None, []

    def is_visible(self):
        return self.visible

    def input_value(self):
        return self.value

    def fill(self, value, timeout=None):
        assert timeout, "every fill carries a short timeout"
        if value == "" and self.fail_clear:
            raise RuntimeError("element is not attached")
        self.value = value
        self.fills.append(value)

    def evaluate(self, expr):
        if "isConnected" in expr:
            if not self.attached:
                raise RuntimeError("Execution context was destroyed")
            return True
        assert "requestSubmit" in expr
        if not self.frame.has_form:
            return False
        self.frame.submit("requestSubmit")
        return True

    def press(self, key, timeout=None):
        assert key == "Enter" and timeout
        self.frame.submit("Enter")


class Frame:
    def __init__(self, url, fields=(), *, navigates=True, has_form=True, moves_to=None):
        self.url, self.fields = url, list(fields)
        self.navigates, self.has_form, self.moves_to = navigates, has_form, moves_to
        self.submits = []
        for f in self.fields:
            f.frame = self

    def query_selector_all(self, selector):
        assert selector == "input[type=password]"
        if self.moves_to:                 # navigated between the check and the fill
            self.url = self.moves_to
        return list(self.fields)

    def submit(self, how):
        self.submits.append(how)
        if self.navigates:                # a real login navigates; an SPA keeps the DOM
            for f in self.fields:
                f.attached = False


class Page:
    def __init__(self, url, fields=(), frames=(), **frame_kw):
        self.url = url
        self.main = Frame(url, fields, **frame_kw)
        self.frames = [self.main, *frames]

    def wait_for_timeout(self, ms):
        pass


class _Context:
    def __init__(self, pages):
        self.pages = list(pages)


def connect_to(*pages):
    """A `connect` seam: cdp_url -> context manager yielding a browser."""
    browser = type("Browser", (), {"contexts": [_Context(pages)]})()
    return lambda cdp_url: contextlib.nullcontext(browser)


PW = "Gen!Pass_1234abcdXYZ"


def _fill(domain, *pages, **kw):
    return secret_fill.fill_and_submit(domain, PW, connect=connect_to(*pages), wait_s=0, **kw)


def test_fills_every_empty_field_submits_the_form_and_reports_a_clean_url():
    pw, confirm = Field(), Field()
    page = Page("https://careers.ses.com/join?invite=abc#step2", [pw, confirm])
    r = _fill("careers.ses.com", page)
    assert r == {"submitted": True, "cleared": True, "page_url": "https://careers.ses.com/join"}
    assert pw.fills == [PW] and confirm.fills == [PW]     # confirm field too
    assert page.main.submits == ["requestSubmit"]


def test_a_value_still_on_the_page_after_submit_is_cleared():
    """An SPA or a validation error keeps the DOM: the value would show up in
    the agent's next browser_snapshot unless it is cleared."""
    field = Field()
    page = Page("https://ses.com/login", [field], navigates=False)
    r = _fill("ses.com", page)
    assert r["submitted"] and r["cleared"]
    assert field.fills == [PW, ""] and field.value == ""


def test_a_clear_that_fails_is_reported():
    field = Field(fail_clear=True)
    r = _fill("ses.com", Page("https://ses.com/login", [field], navigates=False))
    assert r["submitted"] and r["cleared"] is False


def test_a_field_with_no_form_is_submitted_with_enter():
    page = Page("https://ses.com/login", [Field()], has_form=False)
    assert _fill("ses.com", page)["submitted"]
    assert page.main.submits == ["Enter"]


@pytest.mark.parametrize("url,ok", [
    ("https://jobs.careers.ses.com/login", True),          # dot-boundary subdomain
    ("https://careers.ses.com:8443/login", True),
    ("http://careers.ses.com/login", False),                # https only
    ("https://evil.com/login", False),
    ("https://careers.ses.com.evil.com/login", False),      # look-alike
    ("about:blank", False),
])
def test_only_an_https_page_on_the_domain_is_filled(url, ok):
    field = Field()
    page = Page(url, [field])
    r = _fill("careers.ses.com", page)
    assert r["submitted"] is ok
    assert field.fills == ([PW] if ok else []) and bool(page.main.submits) is ok


def test_hidden_prefilled_or_absent_fields_are_not_filled():
    hidden, typed = Field(visible=False), Field(value="agent-typed")
    pages = [Page("https://ses.com/a?t=1", [hidden, typed]), Page("https://ses.com/b")]
    assert _fill("ses.com", *pages) == {"submitted": False,
                                         "pages": ["https://ses.com/a", "https://ses.com/b"]}
    assert hidden.fills == [] and typed.value == "agent-typed"


def test_a_foreign_iframe_on_a_matching_page_is_not_filled():
    evil = Frame("https://evil.com/frame", [Field()])
    assert not _fill("ses.com", Page("https://ses.com/login", frames=[evil]))["submitted"]
    assert evil.fields[0].fills == []


def test_a_frame_that_navigates_away_before_the_fill_is_not_filled():
    field = Field()
    page = Page("https://ses.com/login", [field], moves_to="https://evil.com/login")
    assert not _fill("ses.com", page)["submitted"]
    assert field.fills == [] and page.main.submits == []


def test_after_submit_runs_before_the_clear_and_a_failure_still_clears():
    field = Field()
    page = Page("https://ses.com/login", [field], navigates=False)
    seen = []

    def store():
        seen.append((field.value, list(page.main.submits)))
        raise RuntimeError("could not store")

    with pytest.raises(RuntimeError):
        _fill("ses.com", page, after_submit=store)
    assert seen == [(PW, ["requestSubmit"])]
    assert field.value == ""


def test_page_urls_are_the_real_browser_urls_without_query_or_fragment():
    pages = [Page("https://ses.com/a?token=x#f"), Page("https://evil.com/b")]
    assert secret_fill.page_urls(connect=connect_to(*pages)) == [
        "https://ses.com/a", "https://evil.com/b"]


def test_display_url_drops_userinfo_query_and_fragment():
    assert secret_fill.display_url("https://u:p@ses.com:8443/x?y=1#z") == "https://ses.com:8443/x"
    assert secret_fill.display_url("about:blank") == "about:blank"


async def test_it_refuses_to_run_on_an_event_loop():
    """The answer route is async: a fill reached on the loop must fail loudly,
    before any connection or keystroke, not stall the server."""
    field = Field()
    with pytest.raises(RuntimeError, match="event loop"):
        _fill("ses.com", Page("https://ses.com/login", [field]))
    with pytest.raises(RuntimeError, match="event loop"):
        secret_fill.page_urls(connect=connect_to())
    assert field.fills == []


def test_the_conftest_guard_refuses_a_real_cdp_connection(_no_live_apply_agent):
    with pytest.raises(RuntimeError):
        secret_fill.fill_and_submit("ses.com", PW)
    from playwright.sync_api import BrowserType
    with pytest.raises(RuntimeError):
        BrowserType.connect_over_cdp(object(), "http://127.0.0.1:9222")
    assert _no_live_apply_agent == [("connect_over_cdp",), ("connect_over_cdp",)]
    _no_live_apply_agent.clear()
