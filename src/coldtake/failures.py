"""Failure taxonomy shared by providers and the engine.

`kind` decides what the engine does next:

- fallback:        safe to try the next route (usage limit, expired login, rate limit,
                   overload or unavailability before the request was accepted). On a PAID
                   route this holds only with `proven_unsent=True`; otherwise the engine
                   treats the failure as `charge_unknown`.
- repair:          the provider answered but the output was not a valid memo.
- terminal:        stop the job (refusal, policy, unknown, security).
- timed_out:       a subscription attempt ran past its deadline. Terminal, no charge.
- charge_unknown:  a PAID attempt that may have been sent and billed. Terminal, and the
                   ledger keeps its full allowance. Never used for subscription routes.
"""

from __future__ import annotations

import re
from typing import Literal

Kind = Literal["fallback", "repair", "terminal", "timed_out", "charge_unknown"]

_SECRETS = (
    re.compile(
        r'(?i)(?:api[_-]?key|token|secret|authorization)["\']?\s*[:=]\s*'
        r'(?:bearer\s+)?(?:["\'][^"\'\r\n]+["\']|[^\s,;]+)'
    ),
    re.compile(r"(?i)([?&](?:api[_-]?key|token|secret)=)[^&\s]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)


def redact(text: str, limit: int = 400) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
    return text[:limit]


class ProviderFailure(Exception):
    """A failed attempt. `reason` is a short code; `cause_type` and `message` explain it."""

    def __init__(
        self,
        reason: str,
        kind: Kind,
        message: str = "",
        *,
        cause_type: str | None = None,
        accepted: bool = False,
        cost_usd: float | None = None,
        raw: str | None = None,
        usage: dict[str, int] | None = None,
        proven_unsent: bool = False,
    ):
        super().__init__(message or reason)
        self.reason = reason
        self.kind = kind
        self.message = redact(message or reason)
        self.cause_type = cause_type or type(self).__name__
        # True when a paid request was answered (so its cost is real) even though unusable.
        self.accepted = accepted
        self.cost_usd = cost_usd
        self.raw = raw  # the unusable reply text, kept for repair prompts and partial advice
        self.usage = usage or {}
        # True only when the provider can PROVE the request never reached inference (a
        # connection that was never made, an HTTP rejection before inference). On a paid
        # route this is the only way a failure can release its budget allowance.
        self.proven_unsent = proven_unsent
        # Set by the engine: billing ("subscription" or "paid") of the route that raised it.
        self.route_billing: str | None = None

    def cause(self) -> dict[str, str]:
        return {"type": self.cause_type, "message": self.message}


def _has(text: str, *terms: str) -> bool:
    return any(term in text for term in terms)


_ERROR_LINE = re.compile(r"^(?:\[[^\]]*\]\s*)?(?:error|fatal)\b\s*[:\-]", re.IGNORECASE)
_MARKER_LINE = re.compile(
    r"^(?:warning\b|error\b|fatal\b|codex$|thinking$|exec$|tokens used|mcp startup|\[\d{4}-)",
    re.IGNORECASE,
)
_TAIL_LINES = 5
_MAX_ERROR_TEXT = 4000


def error_text(stdout: str, stderr: str, prompt: str = "") -> str:
    """The provider's own error lines from CLI output, never the prompt it echoed.

    Some CLIs print a banner, then echo the whole prompt (a `user` block), then warnings
    and `ERROR:` lines. Text that came from the prompt must never decide how a failure is
    classified (a packet may mention a rate limit or a login), so: lines that are lines
    of the prompt are dropped, a `user` echo block is dropped up to the next CLI marker,
    and only the `ERROR:`/`error:` lines that remain are used (each once, in order). With
    no such line the last few remaining lines of the stream are used instead.
    """
    prompt_lines = {line.strip() for line in prompt.splitlines() if line.strip()}
    found: list[str] = []
    tails: list[str] = []
    for stream in (stderr, stdout):
        kept: list[str] = []
        in_echo = False
        for raw in stream.splitlines():
            line = raw.strip()
            if not line:
                continue
            if in_echo and _MARKER_LINE.match(line) and line not in prompt_lines:
                in_echo = False
            if in_echo or line in prompt_lines:
                continue
            if line.casefold() == "user":
                in_echo = True
                continue
            kept.append(line)
            if _ERROR_LINE.match(line) and line not in found:
                found.append(line)
        tails.append("\n".join(kept[-_TAIL_LINES:]))
    if found:
        return "\n".join(found)[:_MAX_ERROR_TEXT]
    return next((t for t in tails if t), "")[:_MAX_ERROR_TEXT]


def classify(message: str) -> ProviderFailure:
    """Classify CLI error text. Refusal and policy outrank quota-looking wording.
    Matching sees up to 4000 characters; the stored message keeps the first 400."""
    text = redact(message)
    low = redact(message, _MAX_ERROR_TEXT).casefold()
    if _has(low, "safety", "refused", "cannot assist", "content policy"):
        return ProviderFailure("refusal", "terminal", text, cause_type="Refusal")
    if _has(low, "privacy", "personal data", "pii"):
        return ProviderFailure("privacy", "terminal", text, cause_type="Privacy")
    if _has(low, "invalid request", "invalid input", "policy violation"):
        return ProviderFailure("invalid_input", "terminal", text, cause_type="InvalidInput")
    if _has(low, "prompt injection detected", "compromised", "integrity failure"):
        return ProviderFailure("suspected_compromise", "terminal", text, cause_type="Integrity")
    if (
        _has(
            low,
            "weekly limit",
            "usage limit",
            "quota exceeded",
            "rate limit window",
            "purchase more credits",
        )
        or re.search(r"you['\u2019]ve (?:hit|reached) your [^\r\n]*\blimit\b", low)
        or re.search(r"try again at\s+\S", low)
    ):
        return ProviderFailure("quota", "fallback", text, cause_type="QuotaExhausted")
    if _has(
        low,
        "authentication expired",
        "session expired",
        "login required",
        "refresh_token_reused",
        "refresh token",
        "invalid_grant",
        "invalid_token",
        "token expired",
        "token has expired",
        "unauthorized",
        "not authenticated",
        "not logged in",
        "please log in",
        "please run /login",
        "run /login",
        "/login",
        "logged out",
        "reauthenticate",
        "re-authenticate",
        "401",
    ):
        return ProviderFailure("auth_expired", "fallback", text, cause_type="AuthExpired")
    if _has(low, "rate limit", "rate_limit", "too many requests", "429"):
        return ProviderFailure("rate_limited", "fallback", text, cause_type="RateLimited")
    if _has(low, "overloaded", "over capacity", "try again later"):
        return ProviderFailure("overloaded", "fallback", text, cause_type="Overloaded")
    if _has(low, "service unavailable", "provider unavailable"):
        return ProviderFailure("unavailable", "fallback", text, cause_type="Unavailable")
    return ProviderFailure(
        "unknown",
        "terminal",
        text or "provider failed without a classified reason",
        cause_type="Unclassified",
    )
