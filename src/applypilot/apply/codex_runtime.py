"""Isolated Codex App Server runtime for one browser-automation turn."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from applypilot import __version__
from applypilot import config

PLAYWRIGHT_MCP_VERSION = "0.0.83"
MAX_TOOL_CALLS = 80
MAX_OUTPUT_CHARS = 64_000
DEFAULT_TURN_TIMEOUT = 300
DEFAULT_IDLE_TIMEOUT = 90
MCP_STARTUP_TIMEOUT = 120

_PLAYWRIGHT_TOOLS = (
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_hover",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_take_screenshot",
    "browser_tabs",
    "browser_file_upload",
    "browser_wait_for",
    "browser_close",
    "browser_resize",
)

# Signup/login handoff turns inspect the destination only. The generic MCP form
# tools cannot reliably distinguish a safe profile field from a password box.
_HANDOFF_PLAYWRIGHT_TOOLS = (
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_tabs",
    "browser_wait_for",
)

_MCP_ENV_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA",
    "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES",
)


class CodexRuntimeError(RuntimeError):
    """A sanitized Codex runtime setup or protocol failure."""


@dataclass
class TurnResult:
    status: str
    text: str
    tool_calls: int = 0
    error_code: str | None = None


def resolve_model(auth, client_id: str, requested_model: str | None = None) -> str:
    """Choose a visible model for the selected SIWC account and reject stale slugs."""
    account = auth.active_account()
    if not account or account.get("client_id") != client_id:
        raise CodexRuntimeError("The active ChatGPT account changed. Select the account again and retry.")
    models = auth.list_models(client_id)
    visible = {item["slug"] for item in models}
    selected = requested_model or account.get("selected_model")
    if not selected:
        selected = models[0]["slug"]
    if selected not in visible:
        raise CodexRuntimeError(
            f"Model '{selected}' is not visible to the active ChatGPT account. "
            "Run `applypilot auth models` and select a currently available model."
        )
    return str(selected)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _application_origin(job_url: str) -> str:
    """Return a canonical initial origin and reject malformed/credentialed URLs."""
    try:
        parts = urlsplit(job_url)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise CodexRuntimeError("The application URL is malformed or has an invalid port.") from exc
    if parts.scheme.casefold() not in {"http", "https"} or not hostname:
        raise CodexRuntimeError("The application URL must use HTTP or HTTPS and include a hostname.")
    if parts.username is not None or parts.password is not None:
        raise CodexRuntimeError("The application URL must not contain embedded credentials.")
    if port == 0:
        raise CodexRuntimeError("The application URL contains an invalid port.")
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        try:
            host = hostname.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise CodexRuntimeError("The application URL has an invalid hostname.") from exc
    default_port = 443 if parts.scheme.casefold() == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parts.scheme.casefold()}://{host}{port_suffix}"


def _clean_process_environment(token: str, codex_home: Path) -> dict[str, str]:
    """Build the small App Server environment; never inherit API provider keys."""
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA",
        "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES",
        "PROGRAMFILES(X86)", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    isolated_home = str(codex_home.resolve())
    # Codex honors CODEX_HOME for its own config, while HOME/USERPROFILE also
    # keep any home-directory lookups (including project instructions) inside
    # this short-lived per-run directory.
    env["HOME"] = isolated_home
    env["USERPROFILE"] = isolated_home
    if os.name == "nt":
        drive, tail = os.path.splitdrive(isolated_home)
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
    env["CODEX_HOME"] = isolated_home
    env["ACCESS_TOKEN"] = token
    return env


def _resolve_codex_command() -> list[str]:
    codex = shutil.which("codex")
    node = shutil.which("node")
    if not codex or not node:
        raise CodexRuntimeError("Codex CLI and Node.js must both be installed and available on PATH.")
    codex_path = Path(codex)
    if os.name == "nt" and codex_path.suffix.casefold() in {".cmd", ".bat"}:
        script = codex_path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if not script.is_file():
            raise CodexRuntimeError(
                "Could not locate the npm-installed Codex CLI entry point next to its Windows launcher."
            )
        return [node, str(script), "app-server", "--listen", "stdio://"]
    # On Unix, npm's codex shim is directly executable; native package-manager
    # installations may also expose a binary rather than a Windows .cmd shim.
    return [codex, "app-server", "--listen", "stdio://"]


def _npx_entrypoint() -> tuple[str, str]:
    npx = shutil.which("npx")
    node = shutil.which("node")
    if not npx or not node:
        raise CodexRuntimeError("Node.js and npx are required to start the Playwright MCP server.")
    npx_path = Path(npx)
    resolved = npx_path.resolve()
    if resolved.is_file() and resolved.name.casefold() == "npx-cli.js":
        return node, str(resolved)
    script_candidates = (
        npx_path.parent / "node_modules" / "npm" / "bin" / "npx-cli.js",
        resolved.parent / "node_modules" / "npm" / "bin" / "npx-cli.js",
    )
    script = next((candidate for candidate in script_candidates if candidate.is_file()), None)
    if script is None:
        raise CodexRuntimeError("Could not locate the installed npx entry point.")
    return node, str(script)


def validate_runtime_paths() -> None:
    """Fail early when the installed Codex or npm command cannot be launched safely."""
    _resolve_codex_command()
    _npx_entrypoint()


def _write_codex_config(
    codex_home: Path,
    model: str,
    *,
    worker_dir: Path | None,
    cdp_port: int | None,
    allowed_origin: str | None,
    handoff_only: bool,
) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(codex_home, 0o700)
    lines = [
        'model_provider = "openai_chatgpt_plan"',
        f"model = {_toml_string(model)}",
        'project_root_markers = [".applypilot-codex-root"]',
        "project_doc_max_bytes = 0",
        "project_doc_fallback_filenames = []",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
        "",
        "[model_providers.openai_chatgpt_plan]",
        'name = "ChatGPT plan"',
        'base_url = "https://api.openai.com/v1"',
        'env_key = "ACCESS_TOKEN"',
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "supports_websockets = false",
        "",
        "[shell_environment_policy]",
        'inherit = "none"',
        "ignore_default_excludes = false",
        "",
        "[features]",
        "shell_tool = false",
        "",
        "[analytics]",
        "enabled = false",
    ]
    if worker_dir is not None and cdp_port is not None:
        node, npx_script = _npx_entrypoint()
        wrapper = Path(__file__).with_name("codex_mcp_proxy.py").resolve()
        npm_config = (worker_dir / ".npmrc").resolve()
        npm_global_config = (worker_dir / ".npm-globalrc").resolve()
        npm_config.touch(exist_ok=True)
        npm_global_config.touch(exist_ok=True)
        lines.extend([
            "",
            "[mcp_servers.playwright]",
            f"command = {_toml_string(str(Path(sys.executable).resolve()))}",
            "args = [",
            f"  {_toml_string('-I')},",
            f"  {_toml_string(str(wrapper))},",
            f"  {_toml_string('--node-executable')}, {_toml_string(node)},",
            f"  {_toml_string('--npx-entrypoint')}, {_toml_string(npx_script)},",
            f"  {_toml_string('--npm-config')}, {_toml_string(str(npm_config))},",
            f"  {_toml_string('--npm-global-config')}, {_toml_string(str(npm_global_config))},",
            f"  {_toml_string('--mcp-package')}, {_toml_string(f'@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}')},",
            f"  {_toml_string('--worker-dir')}, {_toml_string(str(worker_dir.resolve()))},",
            f"  {_toml_string('--cdp-port')}, {_toml_string(str(cdp_port))},",
            f"  {_toml_string('--viewport')}, {_toml_string(config.DEFAULTS['viewport'])},",
            *([f"  {_toml_string('--allowed-origin')}, {_toml_string(allowed_origin)},"] if allowed_origin else []),
            "]",
            f"cwd = {_toml_string(str(worker_dir.resolve()))}",
            "env_vars = [" + ", ".join(_toml_string(name) for name in _MCP_ENV_ALLOWLIST) + "]",
            f"startup_timeout_sec = {MCP_STARTUP_TIMEOUT}",
            "tool_timeout_sec = 60",
            'default_tools_approval_mode = "auto"',
            "enabled_tools = [" + ", ".join(
                _toml_string(name) for name in (
                    _HANDOFF_PLAYWRIGHT_TOOLS if handoff_only else _PLAYWRIGHT_TOOLS
                )
            ) + "]",
        ])
    target = codex_home / "config.toml"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(target, 0o600)


class CodexAppServer:
    """Own one App Server process and its JSON-RPC stdio session."""

    def __init__(
        self,
        *,
        token: str,
        model: str,
        codex_home: Path,
        cwd: Path,
        worker_dir: Path | None = None,
        cdp_port: int | None = None,
        job_url: str | None = None,
        handoff_only: bool = False,
    ) -> None:
        self.codex_home = codex_home.resolve()
        self.cwd = cwd.resolve()
        self.model = model
        self.cwd.mkdir(parents=True, exist_ok=True)
        (self.cwd / ".applypilot-codex-root").touch(exist_ok=True)
        allowed_origin = _application_origin(job_url) if job_url else None
        _write_codex_config(
            self.codex_home, model, worker_dir=worker_dir,
            cdp_port=cdp_port, allowed_origin=allowed_origin,
            handoff_only=handoff_only,
        )
        env = _clean_process_environment(token, self.codex_home)
        try:
            self.proc = subprocess.Popen(
                _resolve_codex_command(),
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            raise CodexRuntimeError("Could not start the Codex App Server process.") from exc
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._pending_messages: list[dict[str, Any]] = []
        self._next_id = 1
        self._closed = False
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._unexpected_request = False
        self.tool_calls = 0
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_reader = None

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
        finally:
            self._messages.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        if self.proc.poll() is not None or self.proc.stdin is None:
            raise CodexRuntimeError("The Codex App Server stopped unexpectedly.")
        try:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise CodexRuntimeError("The Codex App Server connection closed unexpectedly.") from exc

    def _deny_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if request_id is None:
            return
        self._unexpected_request = True
        self._send({
            "id": request_id,
            "error": {"code": -32000, "message": "ApplyPilot does not allow interactive approval requests."},
        })

    def _next_message(self, timeout: float) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=max(0.0, timeout))
        except queue.Empty as exc:
            raise TimeoutError from exc
        if message is None:
            code = self.proc.poll()
            raise CodexRuntimeError(f"The Codex App Server exited unexpectedly (exit {code}).")
        if "method" in message and "id" in message:
            self._deny_server_request(message)
            return {"_denied_request": True}
        return message

    def request(self, method: str, params: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self._next_message(deadline - time.monotonic())
            except TimeoutError as exc:
                raise CodexRuntimeError(f"Codex App Server timed out during {method}.") from exc
            if message.get("_denied_request"):
                continue
            if message.get("id") != request_id:
                # Notifications can arrive before an RPC response. Preserve them for the caller.
                self._pending_messages.append(message)
                continue
            if "error" in message:
                error = message.get("error") or {}
                code = error.get("code")
                suffix = f" (code {code})" if code is not None else ""
                raise CodexRuntimeError(f"Codex App Server rejected {method}{suffix}.")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def initialize(self) -> None:
        from applypilot.siwc import APP_NAME

        self.request("initialize", {
            "clientInfo": {
                "name": APP_NAME,
                "title": "ApplyPilot",
                "version": __version__,
            },
        })
        self.notify("initialized")

    @staticmethod
    def _extract_thread_id(result: dict[str, Any]) -> str | None:
        thread = result.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
        value = result.get("threadId") or result.get("thread_id")
        return str(value) if value else None

    def run_turn(
        self,
        prompt: str,
        *,
        total_timeout: int = DEFAULT_TURN_TIMEOUT,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    ) -> TurnResult:
        self.initialize()
        thread_result = self.request("thread/start", {
            "model": self.model,
            "cwd": str(self.cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
        }, timeout=MCP_STARTUP_TIMEOUT + 10)
        self._thread_id = self._extract_thread_id(thread_result)
        if not self._thread_id:
            raise CodexRuntimeError("Codex App Server did not return a thread identifier.")
        turn_result = self.request("turn/start", {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": prompt}],
        }, timeout=MCP_STARTUP_TIMEOUT + 10)
        turn = turn_result.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            self._turn_id = str(turn["id"])

        start = last_activity = time.monotonic()
        deadline = start + total_timeout
        messages: dict[str, list[str]] = {}
        final_texts: list[str] = []
        turn_texts: list[str] = []
        completed_status: str | None = None
        while time.monotonic() < deadline:
            if time.monotonic() - last_activity > idle_timeout:
                self.interrupt()
                return TurnResult("timeout", "", self.tool_calls, "idle_timeout")
            if self._pending_messages:
                message = self._pending_messages.pop(0)
            else:
                try:
                    message = self._next_message(min(1.0, deadline - time.monotonic()))
                except TimeoutError:
                    continue
            if message.get("_denied_request"):
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if not method:
                continue
            last_activity = time.monotonic()
            if method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or "agent-message")
                delta = params.get("delta")
                if isinstance(delta, str):
                    messages.setdefault(item_id, []).append(delta)
                    if sum(sum(len(part) for part in parts) for parts in messages.values()) > MAX_OUTPUT_CHARS:
                        self.interrupt()
                        return TurnResult("limit", "", self.tool_calls, "output_limit")
            elif method in {"item/started", "item/completed"}:
                item = params.get("item") or {}
                if isinstance(item, dict) and item.get("type") in {"mcpToolCall", "mcp_tool_call"}:
                    if method == "item/started":
                        self.tool_calls += 1
                        if self.tool_calls > MAX_TOOL_CALLS:
                            self.interrupt()
                            return TurnResult("limit", "", self.tool_calls, "tool_call_limit")
                if method == "item/completed" and isinstance(item, dict):
                    if item.get("type") in {"agentMessage", "agent_message"}:
                        text = item.get("text")
                        if isinstance(text, str) and text:
                            turn_texts.append(text)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if not isinstance(turn, dict):
                    turn = {}
                completed_status = str(turn.get("status") or params.get("status") or "")
                items = turn.get("items") or []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("type") in {"agentMessage", "agent_message"}:
                            text = item.get("text")
                            if isinstance(text, str) and text:
                                final_texts.append(text)
                break

        else:
            self.interrupt()
            return TurnResult("timeout", "", self.tool_calls, "turn_timeout")

        if completed_status != "completed":
            return TurnResult(completed_status or "incomplete", "", self.tool_calls, "turn_not_completed")
        text = turn_texts[-1] if turn_texts else (final_texts[-1] if final_texts else "")
        if not text and messages:
            text = "".join(next(reversed(messages.values())))
        if len(text) > MAX_OUTPUT_CHARS:
            return TurnResult("limit", "", self.tool_calls, "output_limit")
        if self._unexpected_request:
            return TurnResult("needs_review", "", self.tool_calls, "unexpected_server_request")
        return TurnResult("completed", text, self.tool_calls)

    def interrupt(self) -> None:
        if not self._thread_id or not self._turn_id or self.proc.poll() is not None:
            return
        request_id = self._next_id
        self._next_id += 1
        try:
            self._send({
                "id": request_id,
                "method": "turn/interrupt",
                "params": {"threadId": self._thread_id, "turnId": self._turn_id},
            })
        except CodexRuntimeError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            from applypilot.apply.chrome import _kill_process_tree
            _kill_process_tree(self.proc.pid)
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self._reader.join(timeout=2)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


def run_smoke_request(token: str, model: str, run_dir: Path) -> str:
    """Send one small model request without starting Chrome or an MCP server."""
    work_dir = run_dir / "codex-smoke-work"
    codex_home = run_dir / "codex-smoke-home"
    work_dir.mkdir(parents=True, exist_ok=True)
    runtime = CodexAppServer(
        token=token, model=model, codex_home=codex_home, cwd=work_dir,
    )
    try:
        result = runtime.run_turn(
            "Reply with exactly: ApplyPilot Codex App Server connection is working.",
            total_timeout=60,
            idle_timeout=30,
        )
        if result.status != "completed" or not result.text.strip():
            raise CodexRuntimeError(
                f"Codex App Server self-test did not complete ({result.error_code or result.status})."
            )
        return result.text.strip()
    finally:
        runtime.close()
