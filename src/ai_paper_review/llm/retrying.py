"""Retry wrapper — catches 429 / rate-limit / transient errors and retries
with exponential backoff. Crucial for free-tier API keys.

Wraps any :class:`LLMClient` into another LLMClient (same interface);
``make_client`` in ``factory.py`` composes this automatically so callers
never see a bare provider client.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

from .clients.base import LLMClient

logger = logging.getLogger("llm_client")


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect rate-limit (HTTP 429) or transient (5xx) errors across SDKs.

    Works with anthropic.RateLimitError, openai.RateLimitError,
    httpx.HTTPStatusError(429), and google's ResourceExhausted. Falls back
    to checking the string representation for 429/rate keywords so unknown
    SDK wrappers also get caught.
    """
    cls_name = type(exc).__name__.lower()
    if "ratelimit" in cls_name:
        return True

    # Check for status_code attribute (httpx, openai, anthropic)
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (429, 529):
        return True

    # Check for HTTP 5xx (server overloaded) — worth retrying
    if isinstance(status, int) and 500 <= status < 600:
        return True

    # Fallback: string match
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg or "resource exhausted" in msg


class RetryClient:
    """Wraps any LLMClient and adds retry-with-backoff on rate-limit errors.

    On each retry, sleeps for `base_delay * 2^attempt` seconds (jittered)
    before re-calling the underlying client. If all retries are exhausted,
    the original exception propagates.
    """

    def __init__(self, inner: LLMClient, max_retries: int = 2,
                 base_delay: float = 60.0):
        self._inner = inner
        self._max_retries = max_retries
        self._base_delay = base_delay
        # Proxy the model attribute so callers can still inspect it.
        self.model = inner.model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._inner.complete(system, user, max_tokens, pdf_path=pdf_path)
            except Exception as exc:
                if not _is_rate_limit_error(exc) or attempt == self._max_retries:
                    raise
                last_exc = exc
                delay = self._base_delay * (2 ** attempt) + random.uniform(0, 2)
                logger.warning(
                    "Rate-limited (attempt %d/%d): %s. Retrying in %.1fs…",
                    attempt + 1, self._max_retries, exc, delay,
                )
                time.sleep(delay)
        raise last_exc  # unreachable, but makes type-checkers happy
