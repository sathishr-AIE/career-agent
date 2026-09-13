"""secret_fill: the backend, not the agent, types a password -- only into
pages whose REAL url is on the credential's domain. Fake CDP pages only;
tests/conftest.py fails any test that reaches a real connect_over_cdp."""
import contextlib

import pytest

from career_agent.apply import secret_fill


class Field:
    def __init__(self, visible=True, value=""):
        self.visible, self.value = visible, value

    def is_visible(self):
        return self.visible

    def input_value(self):
        return self.value

    def fill(self, value):
        self.value = value


class _Locator:
    def __init__(self, fields):
        self.fields = fields

    def count(self):
        return len(self.fields)

    def nth(self, i):
        return self.fields[i]


class Frame:
    def __init__(self, url, fields=()):
        self.url, self.fields = url, list(fields)

    def locator(self, selector):
        assert selector == "input[type=password]"
        return _Locator(self.fields)


class Page:
    def __init__(self, url, fields=(), frames=()):
        self.url = url
        self.main = Frame(url, fields)
        self.frames = [self.main, *frames]


class _Context:
    def __init__(self, pages):
        self.pages = list(pages)


def connect_to(*pages):
    """A `connect` seam: cdp_url -> context manager yielding a browser."""
    browser = type("Browser", (), {"contexts": [_Context(pages)]})()
    return lambda cdp_url: contextlib.nullcontext(browser)


PW = "Gen!Pass_1234abcdXYZ"


def test_fills_every_visible_empty_password_field_on_the_matching_page():
    pw, confirm = Field(), Field()
    page = Page("https://careers.ses.com/join", [pw, confirm])
    r = secret_fill.fill_password("careers.ses.com", PW, connect=connect_to(page))
    assert r == {"filled": 2, "page_url": "https://careers.ses.com/join"}
    assert pw.value == PW and confirm.value == PW


@pytest.mark.parametrize("url,filled", [
    ("https://jobs.careers.ses.com/login", 1),          # dot-boundary subdomain
    ("https://careers.ses.com:8443/login", 1),
    ("https://evil.com/login", 0),
    ("https://careers.ses.com.evil.com/login", 0),      # look-alike
    ("https://notcareers.ses.com.evil/login", 0),
    ("about:blank", 0),
])
def test_only_a_page_on_the_domain_is_filled(url, filled):
    field = Field()
    r = secret_fill.fill_password("careers.ses.com", PW, connect=connect_to(Page(url, [field])))
    assert r["filled"] == filled
    assert (field.value == PW) is bool(filled)
    if not filled:
        assert r == {"filled": 0, "pages": [url]}


def test_hidden_prefilled_or_absent_fields_are_not_filled():
    hidden, typed = Field(visible=False), Field(value="agent-typed")
    pages = [Page("https://ses.com/a", [hidden, typed]), Page("https://ses.com/b")]
    r = secret_fill.fill_password("ses.com", PW, connect=connect_to(*pages))
    assert r == {"filled": 0, "pages": ["https://ses.com/a", "https://ses.com/b"]}
    assert hidden.value == "" and typed.value == "agent-typed"


def test_a_foreign_iframe_on_a_matching_page_is_not_filled():
    evil = Frame("https://evil.com/frame", [Field()])
    page = Page("https://ses.com/login", frames=[evil])
    r = secret_fill.fill_password("ses.com", PW, connect=connect_to(page))
    assert r["filled"] == 0 and evil.fields[0].value == ""


def test_page_urls_are_the_real_browser_urls():
    pages = [Page("https://ses.com/a"), Page("https://evil.com/b")]
    assert secret_fill.page_urls(connect=connect_to(*pages)) == [
        "https://ses.com/a", "https://evil.com/b"]


def test_the_conftest_guard_refuses_a_real_cdp_connection(_no_live_apply_agent):
    with pytest.raises(RuntimeError):
        secret_fill.fill_password("ses.com", PW)
    from playwright.sync_api import BrowserType
    with pytest.raises(RuntimeError):
        BrowserType.connect_over_cdp(object(), "http://127.0.0.1:9222")
    assert _no_live_apply_agent == [("connect_over_cdp",), ("connect_over_cdp",)]
    _no_live_apply_agent.clear()
