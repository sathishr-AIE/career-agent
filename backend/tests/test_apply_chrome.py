from pathlib import Path
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
