"""Real-Chrome lifecycle for the apply agent: launch the system Chrome with
CDP remote debugging on an isolated profile cloned once from the user's own
profile. Single worker, port 9222. See docs/lld-apply-button-v2.md §4."""
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

PROFILE_DIR = Path("data/chrome-profile")

_SKIP_ON_CLONE = {"Cache", "Code Cache", "GPUCache", "ShaderCache",
                  "GrShaderCache", "Service Worker", "CacheStorage",
                  "Crashpad", "Temp", "SingletonLock", "SingletonSocket",
                  "SingletonCookie", "BrowserMetrics", "SafeBrowsing",
                  # Chrome's saved-password store (and its journals): the agent's
                  # browser must never autofill or expose the user's own logins.
                  # A profile cloned before this was added keeps its copy --
                  # delete data/chrome-profile to re-clone without it.
                  "Login Data*", "Login Data For Account*"}

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
]


def chrome_command(chrome_exe: str, profile_dir: Path, port: int = 9222,
                   headless: bool = False) -> list[str]:
    cmd = [chrome_exe,
           f"--remote-debugging-port={port}",
           f"--user-data-dir={profile_dir}",
           "--profile-directory=Default",
           "--no-first-run", "--no-default-browser-check",
           "--window-size=1280,800",
           "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
           "--noerrdialogs", "--password-store=basic",
           "--disable-save-password-bubble",
           "--deny-permission-prompts", "--use-fake-ui-for-media-stream",
           "--use-fake-device-for-media-stream", "--disable-notifications"]
    if headless:
        cmd.append("--headless=new")
    return cmd


def get_chrome_path() -> str:
    override = os.environ.get("CHROME_PATH")
    if override:
        # Checked, not trusted: an unchecked override passed ats.preflight()
        # and then failed at launch, which lands as agent_error and (on the
        # send path) held_unknown -- the failure class preflight exists to
        # catch before any application row is written.
        if not Path(override).is_file():
            raise RuntimeError(f"CHROME_PATH is set to {override!r}, which is"
                               " not a file -- fix it in .env, or unset it to"
                               " autodetect Chrome")
        return override
    for c in _CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    raise RuntimeError("Chrome not found -- set CHROME_PATH in .env")


def _user_profile_source() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data"
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


def ensure_profile() -> Path:
    """One-time clone of the user's Chrome profile (cookies, sessions,
    fingerprint). Chrome must be closed during the first clone or files
    are locked -- the copy skips what it can't read. Saved passwords (Login
    Data*) are never copied; an existing clone is left as it is, so one made
    before that rule keeps its copy until data/chrome-profile is deleted."""
    if (PROFILE_DIR / "Default").exists():
        return PROFILE_DIR.resolve()  # relative makes Chrome join a running session
    src = _user_profile_source()
    if not src.exists():
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)  # fresh profile fallback
        return PROFILE_DIR.resolve()  # relative makes Chrome join a running session
    log.info("Cloning Chrome profile from %s (first run)...", src)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in _SKIP_ON_CLONE:
            continue
        try:
            if item.is_dir():
                shutil.copytree(item, PROFILE_DIR / item.name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*_SKIP_ON_CLONE))
            else:
                shutil.copy2(item, PROFILE_DIR / item.name)
        except (PermissionError, OSError):
            pass  # locked file; skip
    return PROFILE_DIR.resolve()  # relative makes Chrome join a running session


def _patch_prefs(profile_dir: Path) -> None:
    prefs = profile_dir / "Default" / "Preferences"
    if not prefs.exists():
        return
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
        data.setdefault("profile", {})["exit_type"] = "Normal"
        data.setdefault("session", {})["restore_on_startup"] = 4
        data["credentials_enable_service"] = False
        data.setdefault("autofill", {})["profile_enabled"] = False
        prefs.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        log.debug("could not patch Chrome prefs", exc_info=True)


def _kill_port(port: int) -> None:
    """Zombie sweep: kill whatever still listens on the CDP port."""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                       capture_output=True, timeout=10)
        else:
            out = subprocess.run(["lsof", "-ti", f":{port}"],
                                 capture_output=True, text=True, timeout=10).stdout
            for pid in out.split():
                subprocess.run(["kill", "-9", pid], capture_output=True)
    except Exception:
        log.debug("port sweep failed", exc_info=True)


def launch_chrome(port: int = 9222, headless: bool = False) -> subprocess.Popen:
    chrome_exe = get_chrome_path()  # fail fast if Chrome not found
    profile = ensure_profile()
    _kill_port(port)
    _patch_prefs(profile)
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() != "Windows":
        kwargs["preexec_fn"] = os.setsid  # process group for tree kill
    proc = subprocess.Popen(chrome_command(chrome_exe, profile, port, headless),
                            **kwargs)
    time.sleep(3)  # let the debug port open
    return proc


def cleanup(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except Exception:
        log.debug("cleanup failed", exc_info=True)
