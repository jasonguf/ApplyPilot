"""Local Sign in with ChatGPT (SIWC) OAuth and session management.

OAuth tokens are stored in the operating system keyring. The adjacent JSON
manifest contains account labels and keyring generation IDs only; it never
contains access, refresh, or ID tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from filelock import FileLock, Timeout as FileLockTimeout
from jwt import PyJWKClient

from applypilot.config import APP_DIR, ensure_dirs
from applypilot.llm_errors import ChatGPTAuthError

AUTHORIZATION_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
API_BASE_URL = "https://api.openai.com/v1"
RESOURCE = API_BASE_URL
ISSUER = "https://auth.openai.com"
APP_NAME = "ApplyPilot"
CALLBACK_PATH = "/auth/callback"
CALLBACK_TIMEOUT_SECONDS = 300
KEYRING_SERVICE = "ApplyPilot.ChatGPT"
USAGE_SETTINGS_URL = "https://chatgpt.com/#settings/usage"

REQUESTED_SCOPES = (
    "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
)
PLAN_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
_KEYRING_FIELDS = ("access_token", "refresh_token", "id_token")
# Windows Credential Locker limits each generic credential blob to 2,560
# bytes. The keyring Windows backend passes password strings as UTF-16, so
# keeping chunks to 1,000 UTF-16 code units leaves room below that limit.
_KEYRING_CHUNK_SIZE = 1_000
_KEYRING_CHUNK_MARKER = "\x1fApplyPilotTokenChunksV1:"
_STATE_LOCK = threading.RLock()


def _state_path() -> Path:
    return APP_DIR / "chatgpt_auth.json"


def _lock_path() -> Path:
    return APP_DIR / "chatgpt_auth.lock"


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "host_id": None, "active_client_id": None, "accounts": {}}


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatGPTAuthError(
            f"Could not read the ChatGPT account manifest at {path}. "
            "Move the damaged file aside, then sign in again."
        ) from exc
    if not isinstance(state, dict) or not isinstance(state.get("accounts", {}), dict):
        raise ChatGPTAuthError(f"The ChatGPT account manifest at {path} is invalid.")
    state.setdefault("version", 1)
    state.setdefault("host_id", None)
    state.setdefault("active_client_id", None)
    state.setdefault("accounts", {})
    return state


def _write_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    if os.name != "nt":
        try:
            os.chmod(APP_DIR, 0o700)
        except OSError:
            pass
    path = _state_path()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(APP_DIR, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ChatGPTAuthError(f"Could not securely save ChatGPT account settings: {exc}") from exc


def _state_guard() -> FileLock:
    ensure_dirs()
    return FileLock(str(_lock_path()), timeout=30)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _keyring_backend():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - dependency is installed with ApplyPilot
        raise ChatGPTAuthError(
            "Secure credential storage is unavailable. Reinstall ApplyPilot so its "
            "OS keyring dependency is installed."
        ) from exc

    try:
        backend = keyring.get_keyring()
        priority = float(getattr(backend, "priority", 0))
        trusted_backend = _is_trusted_os_keyring(backend)
    except Exception as exc:
        raise ChatGPTAuthError(
            "ApplyPilot could not access the operating-system credential store. "
            "Unlock or configure the OS keyring, then retry."
        ) from exc
    if priority <= 0 or not trusted_backend:
        raise ChatGPTAuthError(
            "No supported secure operating-system credential store is available. ApplyPilot supports "
            "Windows Credential Locker, macOS Keychain, Freedesktop Secret Service, and KDE Wallet. "
            "It will not store ChatGPT tokens in a plain-text or unverified backend."
        )
    return keyring


def _is_trusted_os_keyring(backend: Any, seen: set[int] | None = None) -> bool:
    """Allow only known OS stores, including a chainer made entirely of them."""
    seen = seen or set()
    if id(backend) in seen:
        return False
    seen.add(id(backend))
    module = type(backend).__module__
    class_name = type(backend).__name__
    trusted = {
        ("keyring.backends.Windows", "WinVaultKeyring"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.OS_X", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
    }
    if (module, class_name) in trusted:
        return True
    if module == "keyring.backends.chainer" and class_name == "ChainerBackend":
        backends = getattr(backend, "backends", ())
        return bool(backends) and all(_is_trusted_os_keyring(item, seen.copy()) for item in backends)
    return False


def _secret_username(client_id: str, generation: str, field: str) -> str:
    return f"{client_id}:{generation}:{field}"


def _utf16_code_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _split_secret(value: str) -> list[str]:
    """Split a secret into chunks that fit Windows Credential Locker blobs."""
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for char in value:
        char_units = 2 if ord(char) > 0xFFFF else 1
        if current and current_units + char_units > _KEYRING_CHUNK_SIZE:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(char)
        current_units += char_units
    if current:
        chunks.append("".join(current))
    return chunks


def _chunk_username(client_id: str, generation: str, field: str, index: int) -> str:
    return _secret_username(client_id, generation, f"{field}:chunk:{index}")


def _chunk_count(value: str | None) -> int | None:
    if value is None or not value.startswith(_KEYRING_CHUNK_MARKER):
        return None
    raw_count = value[len(_KEYRING_CHUNK_MARKER):]
    try:
        count = int(raw_count)
    except ValueError as exc:
        raise ChatGPTAuthError("A saved ChatGPT credential has an invalid keyring chunk marker. Sign in again.") from exc
    if count <= 0:
        raise ChatGPTAuthError("A saved ChatGPT credential has an invalid keyring chunk marker. Sign in again.")
    return count


def _store_secret_generation(client_id: str, tokens: dict[str, str]) -> str:
    keyring = _keyring_backend()
    generation = uuid.uuid4().hex
    written: list[str] = []
    failed_field: str | None = None
    failed_size: int | None = None
    try:
        for field in _KEYRING_FIELDS:
            value = tokens.get(field)
            if not value:
                continue
            username = _secret_username(client_id, generation, field)
            failed_field = field
            failed_size = _utf16_code_units(value)
            if failed_size <= _KEYRING_CHUNK_SIZE:
                keyring.set_password(KEYRING_SERVICE, username, value)
                written.append(username)
                continue

            chunks = _split_secret(value)
            keyring.set_password(
                KEYRING_SERVICE,
                username,
                f"{_KEYRING_CHUNK_MARKER}{len(chunks)}",
            )
            written.append(username)
            for index, chunk in enumerate(chunks):
                chunk_user = _chunk_username(client_id, generation, field, index)
                keyring.set_password(KEYRING_SERVICE, chunk_user, chunk)
                written.append(chunk_user)
    except Exception as exc:
        for username in reversed(written):
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
            except Exception:
                pass
        error_code = getattr(exc, "winerror", None)
        if error_code is None:
            error_code = getattr(exc, "errno", None)
        error_detail = type(exc).__name__
        if error_code is not None:
            error_detail += f", system error {error_code}"
        failed_detail = f"{failed_field} ({failed_size} UTF-16 code units)" if failed_field else "a token"
        raise ChatGPTAuthError(
            f"Could not save ChatGPT {failed_detail} to the operating-system keyring "
            f"({error_detail}). No plain-text fallback was used."
        ) from exc
    return generation


def _read_secret(client_id: str, generation: str, field: str) -> str | None:
    keyring = _keyring_backend()
    try:
        username = _secret_username(client_id, generation, field)
        stored = keyring.get_password(KEYRING_SERVICE, username)
        count = _chunk_count(stored)
        if count is None:
            return stored
        chunks: list[str] = []
        for index in range(count):
            chunk = keyring.get_password(
                KEYRING_SERVICE,
                _chunk_username(client_id, generation, field, index),
            )
            if chunk is None:
                raise ChatGPTAuthError(
                    "A saved ChatGPT credential is incomplete in the operating-system keyring. "
                    "Run `applypilot auth login` to reconnect."
                )
            chunks.append(chunk)
        return "".join(chunks)
    except ChatGPTAuthError:
        raise
    except Exception as exc:
        raise ChatGPTAuthError(
            "Could not read ChatGPT credentials from the operating-system keyring. "
            "Unlock or configure the keyring, then retry."
        ) from exc


def _delete_secret_generation(client_id: str, generation: str | None) -> None:
    if not generation:
        return
    keyring = _keyring_backend()
    for field in _KEYRING_FIELDS:
        username = _secret_username(client_id, generation, field)
        try:
            marker = keyring.get_password(KEYRING_SERVICE, username)
            count = _chunk_count(marker)
        except Exception:
            count = None
        if count is not None:
            for index in range(count):
                try:
                    keyring.delete_password(
                        KEYRING_SERVICE,
                        _chunk_username(client_id, generation, field, index),
                    )
                except Exception:
                    pass
        try:
            keyring.delete_password(KEYRING_SERVICE, username)
        except Exception:
            pass


def _response_error(response: httpx.Response) -> tuple[str | None, str | None, str]:
    """Extract API error fields without assuming a single response shape."""
    code = None
    param = None
    message = ""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            code = error.get("code")
            param = error.get("param")
            message = str(error.get("message") or error.get("detail") or "")
        elif isinstance(error, str):
            message = error
        if not message:
            message = str(body.get("detail") or body.get("message") or "")
    if not message:
        message = response.text[:400]
    return (str(code) if code else None, str(param) if param else None, message[:400])


def _request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = httpx.request(method, url, timeout=20, **kwargs)
    except httpx.HTTPError as exc:
        raise ChatGPTAuthError(f"Could not reach OpenAI authentication services: {exc}") from exc
    if response.status_code >= 400:
        code, _param, message = _response_error(response)
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        detail = f"HTTP {response.status_code}"
        if code:
            detail += f" ({code})"
        if message:
            detail += f": {message}"
        if request_id:
            detail += f" [request ID {request_id}]"
        raise ChatGPTAuthError(detail)
    try:
        value = response.json()
    except ValueError as exc:
        raise ChatGPTAuthError("OpenAI authentication returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise ChatGPTAuthError("OpenAI authentication returned an unexpected response.")
    return value


def _oidc_metadata() -> dict[str, Any]:
    metadata = _request_json("GET", DISCOVERY_URL)
    if metadata.get("issuer") != ISSUER or not metadata.get("jwks_uri"):
        raise ChatGPTAuthError("OpenAI returned unexpected OIDC discovery metadata.")
    return metadata


def _validate_id_token(
    token: str,
    client_id: str,
    nonce: str | None = None,
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in {"RS256", "ES256"}:
            raise ChatGPTAuthError("OpenAI returned an ID token using an unsupported signing algorithm.")
        jwks_uri = _oidc_metadata()["jwks_uri"]
        jwks = PyJWKClient(jwks_uri, timeout=10)
        signing_key = jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=[algorithm],
            audience=client_id,
            issuer=ISSUER,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except ChatGPTAuthError:
        raise
    except Exception as exc:
        raise ChatGPTAuthError("Could not validate the ChatGPT sign-in token.") from exc

    if nonce is not None:
        actual_nonce = str(claims.get("nonce", ""))
        if not actual_nonce or not hmac.compare_digest(actual_nonce, nonce):
            raise ChatGPTAuthError("ChatGPT sign-in nonce validation failed. Start sign-in again.")
    if not claims.get("sub"):
        raise ChatGPTAuthError("ChatGPT sign-in did not return an account identity.")
    return claims


class ChatGPTAuth:
    """Manage per-account SIWC registrations and their local credentials."""

    def accounts(self) -> list[dict[str, Any]]:
        state = _read_state()
        return list(state["accounts"].values())

    def active_account(self) -> dict[str, Any] | None:
        state = _read_state()
        client_id = state.get("active_client_id")
        return state["accounts"].get(client_id) if client_id else None

    def active_client_id(self) -> str | None:
        state = _read_state()
        return state.get("active_client_id")

    @staticmethod
    def is_plan_enabled(account: dict[str, Any] | None) -> bool:
        if not account or account.get("needs_login"):
            return False
        return PLAN_SCOPES.issubset(set(account.get("scopes", [])))

    def has_active_account(self) -> bool:
        account = self.active_account()
        if (
            not self.is_plan_enabled(account)
            or account.get("refresh_state") in {"in_progress", "unknown"}
        ):
            return False
        generation = account.get("generation") if account else None
        if not generation or not account:
            return False
        return bool(
            _read_secret(account["client_id"], generation, "access_token")
            and _read_secret(account["client_id"], generation, "refresh_token")
        )

    def _host_id(self) -> str:
        with _STATE_LOCK, _state_guard():
            state = _read_state()
            host_id = state.get("host_id")
            if not host_id:
                host_id = f"urn:uuid:{uuid.uuid4()}"
                state["host_id"] = host_id
                _write_state(state)
            return str(host_id)

    def login(
        self,
        client_id: str | None = None,
        *,
        new_account: bool = False,
    ) -> dict[str, Any]:
        """Authorize a new account or reauthorize a saved issued client ID."""
        state = _read_state()
        if client_id and new_account:
            raise ChatGPTAuthError("Choose an existing account or add a new account, not both.")
        selected = state["accounts"].get(client_id) if client_id else None
        if client_id and not selected:
            raise ChatGPTAuthError("That ChatGPT account registration is not saved on this device.")
        if not client_id and not new_account and len(state["accounts"]) == 1:
            client_id, selected = next(iter(state["accounts"].items()))
        elif not client_id and not new_account and len(state["accounts"]) > 1:
            raise ChatGPTAuthError(
                "Choose the account registration to sign in again with `applypilot auth login --account <client-id>`, "
                "or add one with `--new-account`."
            )

        host_id = self._host_id()
        is_new = client_id is None
        oauth_client_id = "dynamic_agent_client" if is_new else str(client_id)
        state_token = _b64url(secrets.token_bytes(32))
        nonce = _b64url(secrets.token_bytes(32))
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        # Request consent again only when explicitly signing into a saved client
        # whose stored grant lacks ChatGPT plan usage.
        request_consent = bool(selected and not self.is_plan_enabled(selected))

        params: dict[str, str] = {
            "client_id": oauth_client_id,
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": "",  # filled after binding the loopback listener
            "scope": REQUESTED_SCOPES,
            "resource": RESOURCE,
            "state": state_token,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if is_new:
            params["agent_name_hint"] = APP_NAME
        elif selected and selected.get("email"):
            params["login_hint"] = str(selected["email"])
        if request_consent:
            params["prompt"] = "consent"

        # Build the loopback listener before opening the browser so the exact
        # callback URI can be reused in both authorization and code exchange.
        callback_uri, callback = _authorize_with_callback(params)
        if callback.get("state") is None or not hmac.compare_digest(callback.get("state", ""), state_token):
            raise ChatGPTAuthError("ChatGPT sign-in state validation failed. Start sign-in again.")
        if callback.get("error"):
            error = callback.get("error")
            description = callback.get("error_description") or ""
            if error == "access_denied":
                raise ChatGPTAuthError(
                    "ChatGPT plan access was not authorized. Run `applypilot auth login` and approve "
                    "the requested ChatGPT plan permission to enable AI stages."
                )
            raise ChatGPTAuthError(f"ChatGPT sign-in failed ({error}): {description[:250]}")
        code = callback.get("code")
        if not code:
            raise ChatGPTAuthError("ChatGPT sign-in returned no authorization code.")

        returned_client_id = callback.get("client_id")
        if is_new:
            resolved_client_id = returned_client_id
            if not resolved_client_id or resolved_client_id == "dynamic_agent_client":
                raise ChatGPTAuthError("ChatGPT did not issue a client ID; registration did not complete.")
        else:
            if returned_client_id and returned_client_id != client_id:
                raise ChatGPTAuthError("ChatGPT returned a different account registration than the one selected.")
            resolved_client_id = str(client_id)

        # Preserve dynamically issued registrations before exchanging the
        # single-use authorization code. If token exchange fails, the next
        # sign-in can reuse this client ID rather than creating another one.
        with _STATE_LOCK, _state_guard():
            state = _read_state()
            if str(resolved_client_id) not in state["accounts"]:
                state["accounts"][str(resolved_client_id)] = {
                    "client_id": str(resolved_client_id),
                    "subject": "",
                    "email": "",
                    "generation": None,
                    "expires_at": 0,
                    "scopes": [],
                    "selected_model": None,
                    "needs_login": True,
                    "refresh_state": None,
                    "registration_pending": True,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }
                _write_state(state)

        tokens = _request_json(
            "POST",
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "client_id": resolved_client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": callback_uri,
                "resource": RESOURCE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        id_token = tokens.get("id_token")
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not id_token or not access_token or not refresh_token:
            raise ChatGPTAuthError(
                "ChatGPT sign-in did not return the complete token set needed for secure, renewable access."
            )
        claims = _validate_id_token(str(id_token), str(resolved_client_id), nonce)

        scopes = sorted(set(str(tokens.get("scope", "")).split()))
        expires_in = _positive_int(tokens.get("expires_in"), default=3600)
        generation: str | None = None
        try:
            with _STATE_LOCK, _state_guard():
                state = _read_state()
                previous = state["accounts"].get(str(resolved_client_id))
                if previous and previous.get("subject") and previous.get("subject") != claims.get("sub"):
                    raise ChatGPTAuthError(
                        "The ChatGPT account identity changed for a saved registration. "
                        "Its credentials were not replaced."
                    )
                generation = _store_secret_generation(
                    str(resolved_client_id),
                    {
                        "access_token": str(access_token),
                        "refresh_token": str(refresh_token),
                        "id_token": str(id_token),
                    },
                )
                account = {
                    "client_id": str(resolved_client_id),
                    "subject": str(claims["sub"]),
                    "email": str(claims.get("email", "")),
                    "generation": generation,
                    "expires_at": time.time() + expires_in,
                    "scopes": scopes,
                    "selected_model": (previous or {}).get("selected_model"),
                    "needs_login": False,
                    "refresh_state": None,
                    "registration_pending": False,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }
                state["accounts"][str(resolved_client_id)] = account
                state["active_client_id"] = str(resolved_client_id)
                _write_state(state)
        except Exception:
            if generation:
                _delete_secret_generation(str(resolved_client_id), generation)
            raise
        if previous:
            _delete_secret_generation(str(resolved_client_id), previous.get("generation"))
        return account

    def list_models(self, client_id: str | None = None) -> list[dict[str, str]]:
        token = self.get_access_token(client_id)
        try:
            response = httpx.get(
                f"{API_BASE_URL}/models",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError(f"Could not reach the ChatGPT model catalog: {exc}") from exc
        if response.status_code >= 400:
            code, _param, message = _response_error(response)
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            detail = f"Model catalog request failed with HTTP {response.status_code}"
            if code:
                detail += f" ({code})"
            if message:
                detail += f": {message}"
            if request_id:
                detail += f" [request ID {request_id}]"
            raise ChatGPTAuthError(detail)
        try:
            body = response.json()
        except ValueError as exc:
            raise ChatGPTAuthError("The ChatGPT model catalog returned invalid JSON.") from exc
        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            raise ChatGPTAuthError("The ChatGPT model catalog returned an unexpected shape.")
        visible = [
            {"slug": str(item["slug"]), "display_name": str(item.get("display_name") or item["slug"])}
            for item in models
            if isinstance(item, dict) and item.get("visibility") == "list" and item.get("slug")
        ]
        if not visible:
            raise ChatGPTAuthError("No models are currently available to this ChatGPT account.")
        return visible

    def get_access_token(self, client_id: str | None = None,
                         min_ttl_seconds: int = 120) -> str:
        if min_ttl_seconds < 0:
            raise ValueError("min_ttl_seconds cannot be negative")
        state = _read_state()
        selected_id = client_id or state.get("active_client_id")
        if not selected_id:
            raise ChatGPTAuthError(
                "No ChatGPT account is connected. Run `applypilot auth login` to sign in."
            )
        account = state["accounts"].get(selected_id)
        if not account:
            raise ChatGPTAuthError("The selected ChatGPT account is not saved on this device.")
        if not self.is_plan_enabled(account):
            raise ChatGPTAuthError(
                "This ChatGPT sign-in has no permission for subscription-backed inference. "
                "Run `applypilot auth login --account <client-id>` and approve ChatGPT plan access."
            )
        if account.get("refresh_state") in {"in_progress", "unknown"}:
            raise ChatGPTAuthError(
                "A previous ChatGPT token refresh could not be confirmed. Run "
                "`applypilot auth login --account <client-id>` before sending more AI requests."
            )
        generation = account.get("generation")
        if not generation:
            raise ChatGPTAuthError(
                "The ChatGPT session needs a new sign-in. Run `applypilot auth login --account <client-id>`."
            )

        try:
            with _STATE_LOCK, _state_guard():
                state = _read_state()
                account = state["accounts"].get(selected_id)
                if not account or not account.get("generation"):
                    raise ChatGPTAuthError("The selected ChatGPT session needs a new sign-in.")
                if account.get("refresh_state") in {"in_progress", "unknown"}:
                    raise ChatGPTAuthError(
                        "A previous ChatGPT token refresh could not be confirmed. Sign in again before inference."
                    )
                generation = account["generation"]
                access_token = _read_secret(selected_id, generation, "access_token")
                if not access_token:
                    raise ChatGPTAuthError("The ChatGPT access token is missing. Sign in again.")
                if float(account.get("expires_at", 0)) > time.time() + min_ttl_seconds:
                    return access_token

                refresh_token = _read_secret(selected_id, generation, "refresh_token")
                old_id_token = _read_secret(selected_id, generation, "id_token")
                if not refresh_token:
                    raise ChatGPTAuthError("The ChatGPT refresh token is missing. Sign in again.")
                account["refresh_state"] = "in_progress"
                state["accounts"][selected_id] = account
                _write_state(state)
                try:
                    tokens = _request_json(
                        "POST",
                        TOKEN_ENDPOINT,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": selected_id,
                            "refresh_token": refresh_token,
                            "resource": RESOURCE,
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                except ChatGPTAuthError as exc:
                    if any(code in str(exc) for code in (
                        "invalid_grant", "invalid_refresh_token", "token_expired",
                        "refresh_token_expired", "refresh_token_invalidated",
                        "refresh_token_reused",
                    )):
                        self._mark_needs_login(state, selected_id, account)
                        raise ChatGPTAuthError(
                            f"The ChatGPT refresh token is no longer usable: {exc}. "
                            "Run `applypilot auth login --account <client-id>`."
                        ) from exc
                    account["refresh_state"] = "unknown"
                    state["accounts"][selected_id] = account
                    _write_state(state)
                    raise ChatGPTAuthError(
                        f"The ChatGPT refresh outcome could not be confirmed: {exc}. "
                        "ApplyPilot preserved the session and requires sign-in before retrying."
                    ) from exc

                new_access = tokens.get("access_token")
                if not new_access:
                    raise ChatGPTAuthError("ChatGPT session renewal returned no access token.")
                new_refresh = tokens.get("refresh_token") or refresh_token
                new_id_token = tokens.get("id_token") or old_id_token
                scopes = sorted(set(str(tokens.get("scope", " ".join(account.get("scopes", [])))).split()))
                expires_in = _positive_int(tokens.get("expires_in"), default=3600)

                if tokens.get("id_token"):
                    claims = _validate_id_token(str(tokens["id_token"]), selected_id)
                    if claims.get("sub") != account.get("subject"):
                        raise ChatGPTAuthError("The renewed ChatGPT session belongs to a different account.")

                new_generation = _store_secret_generation(
                    selected_id,
                    {
                        "access_token": str(new_access),
                        "refresh_token": str(new_refresh),
                        "id_token": str(new_id_token or ""),
                    },
                )
                updated = dict(account)
                updated.update({
                    "generation": new_generation,
                    "expires_at": time.time() + expires_in,
                    "scopes": scopes,
                    "needs_login": False,
                    "refresh_state": None,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                })
                state["accounts"][selected_id] = updated
                try:
                    _write_state(state)
                except Exception as exc:
                    raise ChatGPTAuthError(
                        "ChatGPT returned renewed credentials but ApplyPilot could not save the new session. "
                        "The previous refresh token may no longer work. Run `applypilot auth login --account <client-id>`."
                    ) from exc
                _delete_secret_generation(selected_id, generation)
                if not PLAN_SCOPES.issubset(set(scopes)):
                    raise ChatGPTAuthError(
                        "The renewed session no longer includes the permission for ChatGPT plan inference. "
                        "Run `applypilot auth login --account <client-id>` and approve the requested permission."
                    )
                if float(updated["expires_at"]) <= time.time() + min_ttl_seconds:
                    raise ChatGPTAuthError(
                        "The renewed ChatGPT token does not remain valid long enough for this browser task. "
                        "Retry when the account can issue a longer-lived session."
                    )
                return str(new_access)
        except FileLockTimeout as exc:
            raise ChatGPTAuthError(
                "Another ApplyPilot process is renewing this ChatGPT session. Wait and retry."
            ) from exc

    @staticmethod
    def _mark_needs_login(state: dict[str, Any], client_id: str, account: dict[str, Any]) -> None:
        generation = account.get("generation")
        updated = dict(account)
        updated.update({"generation": None, "needs_login": True, "expires_at": 0, "refresh_state": None})
        state["accounts"][client_id] = updated
        _write_state(state)
        _delete_secret_generation(client_id, generation)

    def select_account(self, account_ref: str) -> dict[str, Any]:
        with _STATE_LOCK, _state_guard():
            state = _read_state()
            matches = [
                account for client_id, account in state["accounts"].items()
                if client_id == account_ref
                or (account_ref and account.get("email", "").casefold() == account_ref.casefold())
            ]
            if len(matches) != 1:
                raise ChatGPTAuthError(
                    "Account selection was ambiguous or unmatched. Use the full issued client ID from `auth accounts`."
                )
            account = matches[0]
            state["active_client_id"] = account["client_id"]
            _write_state(state)
        return account

    def select_model(self, model_slug: str) -> dict[str, Any]:
        selected_id = self.active_client_id()
        if not selected_id:
            raise ChatGPTAuthError("No active ChatGPT account is selected.")
        models = self.list_models(selected_id)
        match = next((model for model in models if model["slug"] == model_slug), None)
        if not match:
            raise ChatGPTAuthError(
                f"Model '{model_slug}' is not visible to the active ChatGPT account. "
                "Choose a slug shown by `applypilot auth models`."
            )
        with _STATE_LOCK, _state_guard():
            state = _read_state()
            client_id = state.get("active_client_id")
            if client_id != selected_id:
                raise ChatGPTAuthError("The active ChatGPT account changed while loading models. Please try again.")
            state["accounts"][client_id]["selected_model"] = model_slug
            _write_state(state)
        return match

    def logout(self, client_id: str | None = None) -> tuple[bool, str | None]:
        state = _read_state()
        selected_id = client_id or state.get("active_client_id")
        if not selected_id or selected_id not in state["accounts"]:
            raise ChatGPTAuthError("No saved ChatGPT account is selected.")
        account = state["accounts"][selected_id]
        generation = account.get("generation")
        revoked = False
        revoke_error: str | None = None
        if generation:
            try:
                refresh_token = _read_secret(selected_id, generation, "refresh_token")
                if refresh_token:
                    metadata = _oidc_metadata()
                    endpoint = metadata.get("revocation_endpoint")
                    if not endpoint:
                        raise ChatGPTAuthError("OpenAI OIDC metadata has no revocation endpoint.")
                    response = httpx.post(
                        endpoint,
                        data={
                            "token": refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": selected_id,
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=20,
                    )
                    if response.status_code != 200:
                        code, _param, message = _response_error(response)
                        revoke_error = f"HTTP {response.status_code} {code or ''} {message}".strip()
                    else:
                        revoked = True
            except Exception as exc:
                revoke_error = str(exc)

        with _STATE_LOCK, _state_guard():
            state = _read_state()
            removed = state["accounts"].pop(selected_id, None)
            if state.get("active_client_id") == selected_id:
                remaining = list(state["accounts"])
                state["active_client_id"] = remaining[0] if remaining else None
            _write_state(state)
        if removed:
            try:
                _delete_secret_generation(selected_id, generation)
            except Exception as exc:
                revoke_error = revoke_error or f"The local keyring entries could not be cleared: {exc}"
        return revoked, revoke_error


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
        return number if number > 0 else default
    except (TypeError, ValueError):
        return default


def _authorize_with_callback(params: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Create the fixed-path loopback callback first, then open authorization."""
    # Build a listener using the same implementation as the general callback
    # helper, while adding redirect_uri before the browser opens.
    callback_event = threading.Event()

    class CallbackServer(HTTPServer):
        callback: dict[str, str] | None = None

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlsplit(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            values = parse_qs(parsed.query, keep_blank_values=True)
            callback = {key: entries[0] for key, entries in values.items() if len(entries) == 1}
            self.server.callback = callback  # type: ignore[attr-defined]
            callback_event.set()
            body = (
                "<!doctype html><html><head><meta charset='utf-8'><title>ApplyPilot</title></head>"
                "<body><h2>ApplyPilot sign-in received</h2>"
                "<p>You can close this tab and return to ApplyPilot.</p></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    try:
        server = CallbackServer(("127.0.0.1", 0), CallbackHandler)
    except OSError as exc:
        raise ChatGPTAuthError(f"Could not start the local sign-in callback listener: {exc}") from exc
    server.timeout = 0.5
    callback_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK_PATH}"
    params["redirect_uri"] = callback_uri
    authorization_url = AUTHORIZATION_ENDPOINT + "?" + urlencode(params)
    try:
        opened = False
        try:
            opened = webbrowser.open(authorization_url, new=1, autoraise=True)
        except Exception:
            pass
        if not opened:
            print("Open this URL to continue ChatGPT sign-in:\n" + authorization_url)

        deadline = time.monotonic() + CALLBACK_TIMEOUT_SECONDS
        while not callback_event.is_set() and time.monotonic() < deadline:
            server.handle_request()
        if not callback_event.is_set() or not server.callback:
            raise ChatGPTAuthError(
                "ChatGPT sign-in timed out before the browser returned. Run `applypilot auth login` again."
            )
        return callback_uri, server.callback
    finally:
        server.server_close()
