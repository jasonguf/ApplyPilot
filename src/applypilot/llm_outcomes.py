"""Durable guard against silently resending requests with uncertain outcomes."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout

from applypilot.config import APP_DIR, ensure_dirs
from applypilot.llm_errors import LLMError, LLMOutcomeUnknown


def _path() -> Path:
    return APP_DIR / "llm_outcomes.json"


def _lock_path() -> Path:
    return APP_DIR / "llm_outcomes.lock"


def _active_lock_path(request_id: str) -> Path:
    return APP_DIR / f"llm_outcome_{request_id}.active.lock"


class RequestLease:
    """Hold a process lock while one request with this stable key is active."""

    def __init__(self, request_id: str, lock: FileLock) -> None:
        self.request_id = request_id
        self._lock = lock

    def __enter__(self) -> str:
        return self.request_id

    def __exit__(self, *_exc: object) -> None:
        if self._lock.is_locked:
            self._lock.release()


def _request_id(request_key: str) -> str:
    return hashlib.sha256(request_key.encode("utf-8")).hexdigest()


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"version": 1, "pending": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LLMError(
            f"ApplyPilot could not read its uncertain-request journal at {path}. "
            "AI requests are blocked to avoid accidental resends. Repair or move that file before continuing."
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("pending"), dict):
        raise LLMError(
            f"ApplyPilot's uncertain-request journal at {path} is invalid. AI requests are blocked."
        )
    return payload


def _save(payload: dict[str, Any]) -> None:
    ensure_dirs()
    if os.name != "nt":
        try:
            os.chmod(APP_DIR, 0o700)
        except OSError:
            pass
    path = _path()
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(APP_DIR, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise LLMError(f"Could not securely update ApplyPilot's uncertain-request journal: {exc}") from exc


def _locked():
    ensure_dirs()
    return FileLock(str(_lock_path()), timeout=15)


def begin(request_key: str, label: str) -> RequestLease:
    """Persist an in-flight request before sending it to OpenAI."""
    request_id = _request_id(request_key)
    active_lock = FileLock(str(_active_lock_path(request_id)))
    try:
        active_lock.acquire(timeout=0)
    except FileLockTimeout as exc:
        raise LLMOutcomeUnknown(
            f"ApplyPilot is already sending or handling this {label} request "
            f"(request ID {request_id[:12]}). Wait for that process to finish before retrying."
        ) from exc
    try:
        with _locked():
            payload = _load()
            existing = payload["pending"].get(request_id)
            if existing:
                state = existing.get("state", "unknown")
                raise LLMOutcomeUnknown(
                    f"ApplyPilot will not resend this {existing.get('label', label)} request because its "
                    f"previous outcome is {state} (request ID {request_id[:12]}). Review it with "
                    "`applypilot auth pending`, then explicitly release it with "
                    f"`applypilot auth retry-unknown {request_id[:12]}` if a retry is intended."
                )
            payload["pending"][request_id] = {
                "label": label[:180],
                "state": "in_flight",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _save(payload)
    except FileLockTimeout as exc:
        active_lock.release()
        raise LLMError("Another ApplyPilot process is updating the uncertain-request journal.") from exc
    except Exception:
        active_lock.release()
        raise
    return RequestLease(request_id, active_lock)


def complete(request_id: str) -> None:
    """Remove an in-flight marker after a known terminal success or failure."""
    try:
        with _locked():
            payload = _load()
            payload["pending"].pop(request_id, None)
            _save(payload)
    except FileLockTimeout as exc:
        raise LLMOutcomeUnknown(
            f"ApplyPilot could not confirm the request outcome journal update (request ID {request_id[:12]}). "
            "The response was discarded; inspect `applypilot auth pending` before retrying."
        ) from exc


def mark_unknown(request_id: str) -> None:
    """Mark a request whose stream ended without a definitive terminal event."""
    try:
        with _locked():
            payload = _load()
            entry = payload["pending"].get(request_id)
            if entry:
                entry["state"] = "unknown"
                entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                _save(payload)
    except FileLockTimeout as exc:
        raise LLMOutcomeUnknown(
            f"The request outcome is unknown (request ID {request_id[:12]}) and its journal lock timed out. "
            "Do not retry until `applypilot auth pending` has been checked."
        ) from exc


def pending() -> list[dict[str, str]]:
    try:
        with _locked():
            payload = _load()
            return [
                {"id": request_id, **entry}
                for request_id, entry in payload["pending"].items()
                if isinstance(entry, dict)
            ]
    except FileLockTimeout as exc:
        raise LLMError("Another ApplyPilot process is updating the uncertain-request journal.") from exc


def release(identifier: str) -> dict[str, str]:
    """Explicitly remove one unknown/in-flight guard so the user may retry."""
    try:
        with _locked():
            payload = _load()
            matches = [
                request_id for request_id in payload["pending"]
                if request_id == identifier or request_id.startswith(identifier)
            ]
            if len(matches) != 1:
                raise LLMError(
                    "Request ID was unmatched or ambiguous; use `applypilot auth pending` for the full ID."
                )
            request_id = matches[0]
    except FileLockTimeout as exc:
        raise LLMError("Another ApplyPilot process is updating the uncertain-request journal.") from exc

    active_lock = FileLock(str(_active_lock_path(request_id)))
    try:
        active_lock.acquire(timeout=0)
    except FileLockTimeout as exc:
        raise LLMError(
            "That request is still active in another ApplyPilot process. Wait for it to stop before releasing its guard."
        ) from exc
    try:
        with _locked():
            payload = _load()
            matches = [
                request_id for request_id in payload["pending"]
                if request_id == identifier or request_id.startswith(identifier)
            ]
            if len(matches) != 1:
                raise LLMError("Request ID was unmatched or ambiguous; use `applypilot auth pending` for the full ID.")
            current_id = matches[0]
            if current_id != request_id:
                raise LLMError("The request journal changed while releasing its guard. Review `auth pending` again.")
            entry = payload["pending"].pop(current_id)
            _save(payload)
            return {"id": current_id, **entry}
    except FileLockTimeout as exc:
        raise LLMError("Another ApplyPilot process is updating the uncertain-request journal.") from exc
    finally:
        if active_lock.is_locked:
            active_lock.release()
