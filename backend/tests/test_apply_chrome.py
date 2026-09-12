from pathlib import Path

import pytest

from career_agent.apply import agent as agent_mod
from career_agent.apply import ats as ats_apply
from career_agent.apply import chrome
from career_agent.apply.chrome import chrome_command


def test_chrome_command_flags():
    cmd = chrome_command("C:/chrome.exe", Path("C:/prof"), port=9223)
    assert cmd[0] == "C:/chrome.exe"
    assert "--remote-debugging-port=9223" in cmd
    assert any(a.startswith("--user-data-dir=") and a.endswith("prof") for a in cmd)
    for flag in ("--no-first-run", "--deny-permission-prompts",
                 "--use-fake-ui-for-media-stream", "--disable-notifications",
                 "--disable-session-crashed-bubble", "--password-store=basic"):
        assert flag in cmd
    assert "--headless=new" not in cmd
    assert "--headless=new" in chrome_command("c", Path("p"), headless=True)


def test_a_bogus_chrome_path_override_is_refused(monkeypatch, tmp_path):
    """get_chrome_path() returned $CHROME_PATH unchecked, so a typo passed
    ats.preflight() and the launch failure landed downstream as
    agent_error/held_unknown -- exactly the class preflight exists to catch
    before any application row is written."""
    monkeypatch.setenv("CHROME_PATH", str(tmp_path / "nope" / "chrome.exe"))
    with pytest.raises(RuntimeError, match="CHROME_PATH"):
        chrome.get_chrome_path()


def test_a_real_chrome_path_override_is_used(monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("CHROME_PATH", str(exe))
    assert chrome.get_chrome_path() == str(exe)


def test_preflight_reports_a_bad_override_as_a_precondition(monkeypatch, tmp_path):
    """preflight() must turn it into PreconditionError, the one failure that
    writes no row at all."""
    monkeypatch.setenv("CHROME_PATH", str(tmp_path / "nope.exe"))
    monkeypatch.setattr(agent_mod, "require_binaries", lambda: None)
    with pytest.raises(agent_mod.PreconditionError, match="CHROME_PATH"):
        ats_apply.preflight()
