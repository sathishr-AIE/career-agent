"""secret_fill: the backend -- never the agent -- fills a password, submits
the form, and clears the field, all in one CDP session while the agent waits
(a filled value would otherwise reach the agent through browser_snapshot).
Fake CDP pages only; tests/conftest.py fails any real connect_over_cdp."""
import contextlib

import pytest

from career_agent.apply import secret_fill


class Field:
    """An ElementHandle for one input."""
    def __init__(self, visible=True, value="", form=0, stubborn=False):
        self.visible, self.value, self.form, self.stubborn = visible, value, form, stubborn
        self.attached, self.frame, self.fills = True, None, []

    def is_visible(self):
        return self.visible

    def input_value(self):
        return self.value

    def fill(self, value, timeout=None):
        assert timeout, "every fill carries a short timeout"
        self.fills.append(value)
        if value == "" and self.stubborn:      # framework state writes it straight back
            return
        self.value = value

    def evaluate(self, expr):
        if "isConnected" in expr:
            if not self.attached:
                raise RuntimeError("Execution context was destroyed")
            return True
        if "document.forms" in expr:
            return self.form if self.frame.has_form else -1
        assert "requestSubmit" in expr
        if not self.frame.has_form:
            return False
        self.frame.submit("requestSubmit")
        return True

    def press(self, key, timeout=None):
        assert key == "Enter" and timeout
        self.frame.submit("Enter")


class Frame:
    def __init__(self, url, fields=(), *, navigates=True, has_form=True, moves_to=None,
                 rerender=None):
        self.url, self.fields = url, list(fields)
        self.navigates, self.has_form, self.moves_to = navigates, has_form, moves_to
        self.rerender = rerender      # None | "clean" | "stubborn": an SPA re-render after submit
        self.submits, self.extra = [], []
        for f in self.fields:
            f.frame = self

    def query_selector_all(self, selector):
        if selector == "input[type=password]":
            if self.moves_to:             # navigated between the check and the fill
                self.url = self.moves_to
            return [f for f in self.fields if f.attached]
        assert selector == "input"
        return [f for f in self.fields + self.extra if f.attached]

    def submit(self, how):
        self.submits.append(how)
        if self.navigates or self.rerender:
            value = next((f.value for f in self.fields if f.value), "")
            for f in self.fields:         # the old DOM goes away
                f.attached = False
            if self.rerender:             # ...and a new input comes back holding the value
                self.extra.append(Field(value=value, stubborn=self.rerender == "stubborn"))


class Page:
    def __init__(self, url, fields=(), frames=(), **frame_kw):
        self.url = url
        self.main = Frame(url, fields, **frame_kw)
        self.frames = [self.main, *frames]
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


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


def test_a_form_that_never_submits_is_cleared_and_not_submitted():
    """M-3: no navigation and no detached field means nothing proves a submit."""
    field, stored = Field(), []
    page = Page("https://ses.com/login", [field], navigates=False)
    r = _fill("ses.com", page, after_submit=lambda: stored.append(1))
    assert r == {"submitted": False, "reason": "no_submit", "pages": ["https://ses.com/login"]}
    assert field.value == "" and stored == []


def test_a_re_rendered_field_holding_the_value_is_scrubbed():
    """I-2: an SPA re-renders the form with the password from framework state;
    the old handles are gone, so only a value scan finds the new input."""
    page = Page("https://ses.com/login", [Field()], rerender="clean")
    r = _fill("ses.com", page)
    assert r["submitted"]
    [reborn] = page.main.extra
    assert reborn.value == "" and 500 in page.waits


def test_a_field_that_keeps_the_value_is_not_submitted():
    stored = []
    page = Page("https://ses.com/login", [Field()], rerender="stubborn")
    r = _fill("ses.com", page, after_submit=lambda: stored.append(1))
    assert r["submitted"] is False and r["reason"] == "value_persists"
    assert stored == []


def test_a_field_with_no_form_is_submitted_with_enter():
    page = Page("https://ses.com/login", [Field()], has_form=False)
    assert _fill("ses.com", page)["submitted"]
    assert page.main.submits == ["Enter"]


def test_a_page_with_a_sign_in_and_a_register_form_fills_nothing():
    """I-3: two qualifying forms -- filling both and submitting one is wrong."""
    signin, register = Field(form=0), Field(form=1)
    page = Page("https://ses.com/login", [signin, register])
    r = _fill("ses.com", page)
    assert r["submitted"] is False and r["reason"] == "ambiguous_form"
    assert signin.fills == [] and register.fills == [] and page.main.submits == []


def test_a_one_field_login_picks_the_only_single_field_form():
    signin, new_pw, confirm = Field(form=0), Field(form=1), Field(form=1)
    page = Page("https://ses.com/login", [signin, new_pw, confirm])
    assert _fill("ses.com", page, max_fields=1)["submitted"]
    assert signin.fills == [PW] and new_pw.fills == [] and confirm.fills == []


def test_a_form_with_more_password_fields_than_allowed_is_not_filled():
    fields = [Field(), Field(), Field()]          # change-password: old, new, confirm
    r = _fill("ses.com", Page("https://ses.com/account", fields))
    assert r["reason"] == "no_field" and all(f.fills == [] for f in fields)


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
    assert _fill("ses.com", *pages) == {"submitted": False, "reason": "no_field",
                                         "pages": ["https://ses.com/a", "https://ses.com/b"]}
    assert hidden.fills == [] and typed.value == "agent-typed"


def test_a_foreign_iframe_on_a_matching_page_is_not_filled():
    evil = Frame("https://evil.com/frame", [Field()])
    assert not _fill("ses.com", Page("https://ses.com/login", frames=[evil]))["submitted"]
    assert evil.fields[0].fills == []


def test_a_frame_that_navigates_away_before_the_fill_is_not_filled():
    field = Field()
    page = Page("https://ses.com/login", [field], moves_to="https://evil.com/login")
    r = _fill("ses.com", page)
    assert r["submitted"] is False and r["reason"] == "navigated"
    assert field.fills == [] and page.main.submits == []


def test_after_submit_runs_only_after_a_proven_clean_submit():
    field = Field()
    page = Page("https://ses.com/login", [field])
    seen = []

    def store():
        seen.append((field.attached, list(page.main.submits)))
        raise RuntimeError("could not store")

    with pytest.raises(RuntimeError):
        _fill("ses.com", page, after_submit=store)
    assert seen == [(False, ["requestSubmit"])]


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
