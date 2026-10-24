"""Start the pinned Playwright MCP server without inheriting Codex credentials."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


_ENV_ALLOWLIST = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA",
    "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES",
}


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--node-executable", required=True)
    parser.add_argument("--npx-entrypoint", required=True)
    parser.add_argument("--npm-config", required=True)
    parser.add_argument("--npm-global-config", required=True)
    parser.add_argument("--mcp-package", required=True)
    parser.add_argument("--worker-dir", required=True)
    parser.add_argument("--cdp-port", required=True, type=int)
    parser.add_argument("--viewport", required=True)
    parser.add_argument("--allowed-origin")
    args = parser.parse_args()

    worker_dir = Path(args.worker_dir).resolve()
    npm_config = Path(args.npm_config).resolve()
    npm_global_config = Path(args.npm_global_config).resolve()
    if (not worker_dir.is_dir() or npm_config.parent != worker_dir
            or npm_global_config.parent != worker_dir or not npm_config.is_file()
            or not npm_global_config.is_file()):
        return 2
    if not 1 <= args.cdp_port <= 65535:
        return 2

    # Only copy basic OS paths into Node/npm. ACCESS_TOKEN, CODEX_HOME, API keys,
    # proxy credentials, and arbitrary user variables stay with the App Server.
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in _ENV_ALLOWLIST
    }
    env["NPM_CONFIG_USERCONFIG"] = str(npm_config)
    command = [
        args.node_executable,
        args.npx_entrypoint,
        "--userconfig",
        str(npm_config),
        "--globalconfig",
        str(npm_global_config),
        "--yes",
        args.mcp_package,
        f"--cdp-endpoint=http://127.0.0.1:{args.cdp_port}",
        f"--viewport-size={args.viewport}",
    ]
    if args.allowed_origin:
        command.append(f"--allowed-origins={args.allowed_origin}")
    try:
        return subprocess.call(command, cwd=str(worker_dir), env=env)
    except OSError:
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
