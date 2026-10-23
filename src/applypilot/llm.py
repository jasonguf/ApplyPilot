"""LLM client using the selected user's eligible ChatGPT plan authorization.

Requests go only to the public Responses API. ApplyPilot does not fall back to
API-key billing or a local provider when subscription-backed inference fails.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Any

import httpx

from applypilot.llm_errors import (
    ChatGPTAuthError,
    ChatGPTUsageLimitError,
    ChatGPTUnsupportedCapabilityError,
    LLMCallBudgetExceeded,
    LLMError,
    LLMOutcomeUnknown,
    LLMOutputError,
)
from applypilot import llm_outcomes
from applypilot.siwc import API_BASE_URL, ChatGPTAuth, USAGE_SETTINGS_URL

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=20, read=180, write=30, pool=30)
_MAX_TRANSIENT_ATTEMPTS = 3
_MAX_RETRY_WAIT = 20.0


class _RetryableProviderError(LLMError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class LLMClient:
    """ChatGPT plan-backed Responses client with bounded retries and call limits."""

    def __init__(self) -> None:
        self.auth = ChatGPTAuth()
        self.account_id = self.auth.active_client_id()
        if not self.account_id:
            raise ChatGPTAuthError("No ChatGPT account is connected. Run `applypilot auth login`.")

        account = self.auth.active_account()
        self.models = self.auth.list_models(self.account_id)
        visible_slugs = {item["slug"] for item in self.models}
        requested_model = (account or {}).get("selected_model") or self.models[0]["slug"]
        if os.environ.get("LLM_MODEL"):
            log.warning("LLM_MODEL is no longer used. Select an account-visible model with `applypilot auth use-model`.")
        if requested_model not in visible_slugs:
            raise ChatGPTAuthError(
                f"Model '{requested_model}' is not available to the active ChatGPT account. "
                "Run `applypilot auth models`, then select a visible model with "
                "`applypilot auth use-model <slug>`."
            )
        self.model = requested_model
        self._http = httpx.Client(timeout=_TIMEOUT)
        self._semaphore = threading.BoundedSemaphore(_positive_env("APPLYPILOT_LLM_CONCURRENCY", 2))
        self._call_limit = _positive_env("APPLYPILOT_LLM_MAX_CALLS", 0)
        self._request_count = 0
        self._count_lock = threading.Lock()
        self._fatal_error: LLMError | None = None
        self._fatal_lock = threading.Lock()

    @property
    def request_count(self) -> int:
        with self._count_lock:
            return self._request_count

    def _reserve_call(self) -> None:
        with self._count_lock:
            if self._call_limit and self._request_count >= self._call_limit:
                raise LLMCallBudgetExceeded(
                    f"ApplyPilot's local LLM request limit ({self._call_limit}) was reached. "
                    "The pending item was not sent. Increase APPLYPILOT_LLM_MAX_CALLS to continue."
                )
            self._request_count += 1

    def trip_circuit(self, error: LLMError) -> None:
        """Prevent additional inference after a stage-wide provider failure."""
        with self._fatal_lock:
            if self._fatal_error is None:
                self._fatal_error = error

    def _make_payload(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        instructions: list[str] = []
        inputs: list[dict[str, str]] = []
        supported_roles = {"developer", "user", "assistant"}

        for message in messages:
            if not isinstance(message, dict):
                raise LLMError("Each LLM message must be a role/content object.")
            role = str(message.get("role", ""))
            content = message.get("content")
            if not isinstance(content, str):
                raise LLMError("Responses requests currently require text message content.")
            if role == "system":
                instructions.append(content)
            elif role in supported_roles:
                inputs.append({"role": role, "content": content})
            else:
                raise LLMError(f"The ChatGPT Responses route does not support message role '{role}'.")

        if not inputs:
            raise LLMError("An LLM request needs at least one user or assistant message.")

        payload: dict[str, Any] = {
            "model": self.model,
            "input": inputs,
            "store": False,
            "stream": True,
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        return payload

    @staticmethod
    def _request_id(headers: httpx.Headers) -> str | None:
        return headers.get("x-request-id") or headers.get("request-id")

    @staticmethod
    def _parse_api_error(body: Any, status_code: int, request_id: str | None) -> LLMError:
        error: Any = body.get("error", body) if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            param = str(error.get("param") or "")
            message = str(error.get("message") or error.get("detail") or "")
        else:
            code = ""
            param = ""
            message = str(error or "")
        if not message and isinstance(body, dict):
            message = str(body.get("detail") or body.get("message") or "")
        message = message[:400]
        context = f"HTTP {status_code}"
        if code:
            context += f" ({code})"
        if param:
            context += f"; parameter={param}"
        if message:
            context += f": {message}"
        if request_id:
            context += f" [request ID {request_id}]"

        if code == "subscription_sharing_usage_limit_exceeded":
            return ChatGPTUsageLimitError(
                "ChatGPT plan usage is currently limited. ApplyPilot paused AI work; no API billing fallback "
                f"was used. Check ChatGPT Settings → Usage: {USAGE_SETTINGS_URL}. {context}"
            )
        if status_code == 429 or code == "rate_limit_exceeded":
            return _RetryableProviderError("ChatGPT temporarily rate limited this request. " + context)
        if code == "subscription_sharing_unsupported_capability":
            return ChatGPTUnsupportedCapabilityError(
                "ChatGPT rejected an unsupported model capability or request field. " + context
            )
        if code == "subscription_sharing_user_not_eligible":
            return ChatGPTAuthError("This ChatGPT account or workspace is not eligible for plan usage. " + context)
        if code in {"chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context"}:
            return ChatGPTAuthError("The ChatGPT authorization does not permit this request. " + context)
        if status_code == 401:
            return ChatGPTAuthError("ChatGPT did not accept the current sign-in. Run `applypilot auth login`. " + context)
        if code in {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"}:
            return _RetryableProviderError("ChatGPT plan usage is temporarily unavailable. " + context)
        if status_code == 503:
            return _RetryableProviderError("ChatGPT inference is temporarily unavailable. " + context)
        return LLMError("ChatGPT Responses request failed. " + context)

    @staticmethod
    def _retry_after(headers: httpx.Headers) -> float | None:
        raw = headers.get("retry-after")
        if not raw:
            return None
        try:
            return min(max(float(raw), 0.0), _MAX_RETRY_WAIT)
        except ValueError:
            return None

    @staticmethod
    def _response_text(response: dict[str, Any]) -> str:
        output = response.get("output", [])
        chunks: list[str] = []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content", [])
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "refusal"}:
                        value = part.get("text")
                        if isinstance(value, str):
                            chunks.append(value)
        return "".join(chunks)

    def _consume_stream(self, response: httpx.Response) -> str:
        text_parts: list[str] = []
        data_lines: list[str] = []
        completed = False
        request_id = self._request_id(response.headers)

        def consume_event(raw_data: str) -> None:
            nonlocal completed
            if not raw_data or raw_data == "[DONE]":
                return
            try:
                event = json.loads(raw_data)
            except json.JSONDecodeError as exc:
                raise LLMOutcomeUnknown(
                    "ChatGPT returned an unreadable stream event before completion. The request was not resent."
                ) from exc
            if not isinstance(event, dict):
                return
            event_type = event.get("type")
            if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                delta = event.get("delta")
                if isinstance(delta, str):
                    text_parts.append(delta)
            elif event_type == "response.completed":
                completed = True
                final_response = event.get("response")
                if not text_parts and isinstance(final_response, dict):
                    fallback = self._response_text(final_response)
                    if fallback:
                        text_parts.append(fallback)
            elif event_type == "response.failed":
                final_response = event.get("response")
                error = final_response.get("error") if isinstance(final_response, dict) else None
                raise self._parse_api_error(
                    {"error": error} if isinstance(error, dict) else {"error": str(error or "Response failed")},
                    200,
                    request_id,
                )
            elif event_type == "error":
                raise self._parse_api_error(event, 200, request_id)
            elif event_type == "response.incomplete":
                details = event.get("response")
                status = details.get("status") if isinstance(details, dict) else None
                raise LLMError(
                    f"ChatGPT returned an incomplete response (status: {status or 'unknown'}). "
                    "ApplyPilot discarded the partial output and did not resend it."
                )

        try:
            for line in response.iter_lines():
                if line == "":
                    consume_event("\n".join(data_lines))
                    data_lines.clear()
                    if completed:
                        break
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines and not completed:
                consume_event("\n".join(data_lines))
        except _RetryableProviderError:
            raise
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMOutcomeUnknown(
                "The ChatGPT response stream ended unexpectedly. ApplyPilot discarded partial output and "
                "did not resend the request."
            ) from exc

        if not completed:
            raise LLMOutcomeUnknown(
                "The ChatGPT stream ended without response.completed. ApplyPilot discarded partial output "
                "and did not resend the request."
            )
        result = "".join(text_parts)
        if not result.strip():
            raise LLMOutputError(
                "ChatGPT completed the request without returning text. ApplyPilot discarded the empty output."
            )
        return result

    def _call_once(self, payload: dict[str, Any]) -> str:
        token = self.auth.get_access_token(self.account_id)
        self._reserve_call()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            with self._http.stream(
                "POST",
                f"{API_BASE_URL}/responses",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    try:
                        body = response.json()
                    except (ValueError, json.JSONDecodeError):
                        body = {"detail": response.read().decode("utf-8", errors="replace")[:400]}
                    error = self._parse_api_error(
                        body,
                        response.status_code,
                        self._request_id(response.headers),
                    )
                    if isinstance(error, _RetryableProviderError):
                        error.retry_after = self._retry_after(response.headers)
                    raise error
                return self._consume_stream(response)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _RetryableProviderError(f"Could not connect to ChatGPT inference: {exc}") from exc
        except _RetryableProviderError:
            raise
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMOutcomeUnknown(
                "The ChatGPT request ended unexpectedly. Its outcome is unknown, so ApplyPilot did not resend it."
            ) from exc

    def _send_with_journal(self, payload: dict[str, Any], journal_id: str | None) -> str:
        with self._semaphore:
            with self._fatal_lock:
                if self._fatal_error is not None:
                    if journal_id:
                        llm_outcomes.complete(journal_id)
                    raise self._fatal_error
            for attempt in range(_MAX_TRANSIENT_ATTEMPTS):
                try:
                    result = self._call_once(payload)
                    if journal_id:
                        llm_outcomes.complete(journal_id)
                    return result
                except _RetryableProviderError as exc:
                    if attempt + 1 >= _MAX_TRANSIENT_ATTEMPTS:
                        if journal_id:
                            llm_outcomes.complete(journal_id)
                        self.trip_circuit(exc)
                        raise
                    delay = exc.retry_after
                    if delay is None:
                        delay = min(0.75 * (2 ** attempt) + random.uniform(0.0, 0.25), _MAX_RETRY_WAIT)
                    log.warning(
                        "ChatGPT plan inference temporarily unavailable; retrying in %.1fs (%d/%d).",
                        delay,
                        attempt + 1,
                        _MAX_TRANSIENT_ATTEMPTS - 1,
                    )
                    time.sleep(delay)
                except LLMOutcomeUnknown as exc:
                    if journal_id:
                        llm_outcomes.mark_unknown(journal_id)
                    self.trip_circuit(exc)
                    raise
                except LLMError as exc:
                    if journal_id:
                        llm_outcomes.complete(journal_id)
                    self.trip_circuit(exc)
                    raise
                except Exception as exc:
                    error = LLMOutcomeUnknown(
                        "The ChatGPT request ended unexpectedly. ApplyPilot recorded the outcome as unknown "
                        "and will not resend it automatically."
                    )
                    if journal_id:
                        llm_outcomes.mark_unknown(journal_id)
                    self.trip_circuit(error)
                    raise error from exc
        raise LLMError("ChatGPT inference failed after bounded transient retries.")

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_output_tokens: int | None = None,
        request_key: str | None = None,
        request_label: str | None = None,
    ) -> str:
        """Send a text request and return content only after response.completed.

        Legacy generation controls remain accepted by the method signature so
        callers fail with a useful message rather than a Python argument error.
        The SIWC Responses route rejects these fields; callers must express the
        desired style or brevity in prompt instructions instead.
        """
        unsupported = []
        if temperature is not None:
            unsupported.append("temperature")
        if max_tokens is not None:
            unsupported.append("max_tokens")
        if max_output_tokens is not None:
            unsupported.append("max_output_tokens")
        if unsupported:
            error = LLMError(
                "The ChatGPT plan route does not accept " + ", ".join(unsupported) +
                ". Express style or length requirements in the prompt."
            )
            self.trip_circuit(error)
            raise error
        if not request_key:
            raise LLMError("Every ChatGPT inference request needs a stable request key for safe recovery.")
        payload = self._make_payload(messages)

        with self._fatal_lock:
            if self._fatal_error is not None:
                raise self._fatal_error
        request_lease = llm_outcomes.begin(request_key, request_label or "LLM request")
        journal_id = request_lease.request_id
        try:
            return self._send_with_journal(payload, journal_id)
        finally:
            request_lease.__exit__()

    def ask(
        self,
        prompt: str,
        *,
        request_key: str | None = None,
        request_label: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Convenience wrapper for a single user prompt."""
        return self.chat(
            [{"role": "user", "content": prompt}],
            request_key=request_key,
            request_label=request_label,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()


def _positive_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise LLMError(f"{name} must be a positive integer.") from exc
    if parsed < (0 if default == 0 else 1):
        raise LLMError(f"{name} must be {'zero or a positive' if default == 0 else 'a positive'} integer.")
    return parsed


_instance: LLMClient | None = None
_instance_lock = threading.Lock()


def get_client() -> LLMClient:
    """Return the process-wide ChatGPT Responses client."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LLMClient()
                log.info("LLM provider: ChatGPT plan  model: %s", _instance.model)
    return _instance


def reset_client() -> None:
    """Close and discard the process-wide client (useful after account changes)."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.close()
            _instance = None


def trip_circuit(error: LLMError) -> None:
    """Trip the active process-wide provider circuit after a stage failure."""
    instance = _instance
    if instance is not None:
        instance.trip_circuit(error)


def usage_stats() -> dict[str, int | str] | None:
    """Return this process's request count without estimating plan quota."""
    instance = _instance
    if instance is None:
        return None
    return {
        "model": instance.model,
        "requests": instance.request_count,
        "max_calls": instance._call_limit,
        "concurrency": _positive_env("APPLYPILOT_LLM_CONCURRENCY", 2),
    }
