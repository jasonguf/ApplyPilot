"""Errors raised by ApplyPilot's ChatGPT-backed LLM client."""


class LLMError(RuntimeError):
    """A provider or inference failure that should stop the active AI stage."""


class ChatGPTAuthError(LLMError):
    """The ChatGPT account is missing, expired, revoked, or not authorized."""


class ChatGPTUsageLimitError(LLMError):
    """ChatGPT plan or app-specific usage limits blocked inference."""


class ChatGPTUnsupportedCapabilityError(LLMError):
    """The selected route rejected an unsupported model or request capability."""


class LLMOutcomeUnknown(LLMError):
    """A request stream ended without a terminal event; it must not be resent automatically."""


class LLMOutputError(LLMError):
    """A completed model response did not satisfy the expected output contract."""


class LLMCallBudgetExceeded(LLMError):
    """The local per-process LLM request ceiling was reached."""
