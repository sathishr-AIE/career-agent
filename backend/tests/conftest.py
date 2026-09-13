import docx


def build_tailor_template(path):
    """A minimal DOCX carrying tailor.py's two marker paragraphs, built with
    python-docx rather than committed as a binary fixture -- keeps the
    fixture readable and diffable in the test files that use it."""
    doc = docx.Document()
    doc.add_paragraph("Sathish R -- AI Engineer")
    doc.add_paragraph("<<SUMMARY>>")
    doc.add_paragraph("<<PROJECT_BULLET>>", style="List Bullet")
    doc.add_paragraph("Education")
    doc.save(str(path))



import subprocess
from pathlib import Path

import pytest

_PAID_BINARIES = {"claude", "npx", "chrome"}


@pytest.fixture(autouse=True)
def _no_live_apply_agent(monkeypatch):
    """No test may reach the real apply engine (a real Chrome plus a real
    `claude` session): tests/test_web.py's override test did, on every
    suite run, spending real credits on the fixture's placeholder URL.

    Hits are recorded and asserted at teardown rather than only raised:
    ats._run catches every exception into `agent_error`, so a raise alone
    let the leaking test pass green. Yields the hit list so a test that
    trips the guard on purpose can check and clear it."""
    from career_agent.apply import ats, chrome

    hits = []

    async def _refuse(prompt, job_id, nonce, *a, **kw):
        hits.append(("live_run_agent", job_id))
        raise RuntimeError("test reached the live apply engine")

    def _no_chrome(*a, **kw):
        hits.append(("launch_chrome",))
        raise RuntimeError("test reached chrome.launch_chrome")

    real_popen = subprocess.Popen

    def _guarded_popen(args, *a, **kw):
        if isinstance(args, str):   # a command line: "quoted exe" or bare exe
            s = args.strip()
            argv0 = s[1:s.index('"', 1)] if s.startswith('"') else s.split()[0]
        else:
            argv0 = str(args[0])
        if Path(argv0).stem.lower() in _PAID_BINARIES:
            hits.append(("popen", argv0))
            raise RuntimeError(f"test tried to spawn {argv0}")
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(ats, "_live_run_agent", _refuse)
    monkeypatch.setattr(chrome, "launch_chrome", _no_chrome)
    monkeypatch.setattr(subprocess, "Popen", _guarded_popen)
    yield hits
    assert not hits, (f"test reached the live apply engine: {hits} -- "
                      "stub submit() or inject run_agent")
