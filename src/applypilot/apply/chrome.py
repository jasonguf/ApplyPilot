"""Chrome lifecycle management for isolated ApplyPilot browser workers."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from applypilot import config

logger = logging.getLogger(__name__)

BASE_CDP_PORT = 9222
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


def _kill_process_tree(pid: int) -> None:
    """Kill a process tree rooted at a process ApplyPilot launched."""
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to stop owned process tree (pid %d)", pid, exc_info=True)


def _assert_port_available(port: int) -> None:
    """Fail if a port is occupied; never terminate an unrelated listener."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(
            f"Chrome debugging port {port} is already in use. Close the process using it and retry."
        ) from exc
    finally:
        probe.close()


def setup_worker_profile(worker_id: int) -> Path:
    """Create or reuse an ApplyPilot-only Chrome profile for this worker."""
    # Versioned path intentionally avoids reusing profiles cloned by older releases.
    profile_dir = config.CHROME_WORKER_DIR / "codex-isolated-v1" / f"worker-{worker_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        for directory in (config.CHROME_WORKER_DIR, profile_dir.parent, profile_dir):
            os.chmod(directory, 0o700)
    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's restore prompt inside the isolated ApplyPilot profile."""
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch isolated Chrome preferences", exc_info=True)


def _wait_for_cdp(proc: subprocess.Popen, port: int, timeout: float = 20.0) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Chrome exited during startup (exit {proc.returncode}).")
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                payload = json.loads(response.read(16_384).decode("utf-8"))
            if isinstance(payload, dict) and payload.get("Browser") and payload.get("webSocketDebuggerUrl"):
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_error = exc
        time.sleep(0.2)
    detail = f" ({type(last_error).__name__})" if last_error else ""
    raise RuntimeError(f"Chrome did not expose its local debugging endpoint in time{detail}.")


def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False,
                  startup_url: str | None = None) -> subprocess.Popen:
    """Launch Chrome in its dedicated profile and wait for its local CDP endpoint."""
    if startup_url:
        try:
            parsed = urlsplit(startup_url)
            parsed.port
        except ValueError as exc:
            raise ValueError("Chrome startup URL has an invalid port.") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password):
            raise ValueError("Chrome startup URL must be a valid HTTP(S) URL without credentials.")
    if port is None:
        port = BASE_CDP_PORT + worker_id
    _assert_port_available(port)
    profile_dir = setup_worker_profile(worker_id)
    _suppress_restore_nag(profile_dir)
    chrome_exe = config.get_chrome_path()
    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,900",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if headless:
        cmd.append("--headless=new")
    if startup_url:
        cmd.append(startup_url)
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() != "Windows":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc
    try:
        _wait_for_cdp(proc, port)
    except Exception:
        with _chrome_lock:
            if _chrome_procs.get(worker_id) is proc:
                _chrome_procs.pop(worker_id, None)
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        raise
    logger.info("[worker-%d] Chrome ready on local debugging port %d", worker_id, port)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Stop only the Chrome process tree tracked for this worker."""
    with _chrome_lock:
        tracked = _chrome_procs.get(worker_id)
        if process is not None and tracked is process:
            _chrome_procs.pop(worker_id, None)
        else:
            tracked = None
    if tracked is not None and tracked.poll() is None:
        _kill_process_tree(tracked.pid)
    logger.info("[worker-%d] owned Chrome process cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Stop every Chrome process launched and tracked by this ApplyPilot process."""
    with _chrome_lock:
        procs = list(_chrome_procs.items())
        _chrome_procs.clear()
    for _worker_id, proc in procs:
        if proc.poll() is None:
            _kill_process_tree(proc.pid)


def reset_worker_dir(worker_id: int) -> Path:
    """Create a clean, per-job working directory with no inherited files."""
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(worker_dir)
    worker_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(worker_dir, 0o700)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler that stops only processes created by this run."""
    kill_all_chrome()
