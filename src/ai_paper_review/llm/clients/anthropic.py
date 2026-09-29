"""AnthropicClient — Claude via the Anthropic Messages API.

The SDK is imported lazily inside ``__init__`` so users don't need the
``anthropic`` package unless they actually select this provider.

When ``pdf_path`` is given, the PDF is attached as a base64 ``document``
content block so Claude sees the original PDF (figures, tables,
layout) rather than the lossy pypdf-extracted text.

Requests with a large ``max_tokens`` budget go through the streaming
API (``messages.stream``). The Anthropic SDK refuses non-streaming
``messages.create`` calls whose expected duration exceeds 10 minutes
(computed from ``max_tokens`` times a slow-model per-token latency),
which trips on the validator's batch-similarity call (up to 32 K
output tokens on Opus / extended thinking). Streaming bypasses that
ceiling; the client reads the final message and returns its text,
same shape as the non-streaming path.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from .base import ReplyBlockedError


# Above this budget, use ``messages.stream`` to avoid the SDK's
# 10-minute non-streaming ceiling. Picked conservatively — Opus at
# ~40 tok/s finishes 16 K tokens in ~6.7 min, comfortably below 10.
_STREAMING_MAX_TOKENS_THRESHOLD = 16000


def _nonstreaming_limit(model: str) -> int:
    """Largest max_tokens sent without streaming. The SDK also refuses
    non-streaming calls above its per-model cap (8192 for Opus 4.0 / 4.1)."""
    try:
        from anthropic._constants import MODEL_NONSTREAMING_TOKENS
    except ImportError:
        return _STREAMING_MAX_TOKENS_THRESHOLD
    return min(_STREAMING_MAX_TOKENS_THRESHOLD,
               MODEL_NONSTREAMING_TOKENS.get(model, _STREAMING_MAX_TOKENS_THRESHOLD))


def _reply_text(msg, max_tokens: int) -> str:
    """Text of a Messages API reply. Raises ReplyBlockedError when Claude
    refused, or used the whole budget without text."""
    if msg.stop_reason == "refusal":
        raise ReplyBlockedError("Claude refused the request (stop_reason=refusal).")
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    )
    if not text and msg.stop_reason == "max_tokens":
        raise ReplyBlockedError(
            f"Claude used up its output budget (max_tokens={max_tokens}) "
            f"before writing any text; raise max_tokens."
        )
    return text


class AnthropicClient:
    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None):
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise ImportError("Install the Anthropic SDK: pip install anthropic") from e
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = Anthropic(**kwargs)
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        if pdf_path:
            pdf_b64 = base64.standard_b64encode(
                Path(pdf_path).read_bytes()
            ).decode("ascii")
            # ``cache_control: ephemeral`` marks the document block as a
            # cache breakpoint so the ``(system + PDF)`` prefix is reused
            # across sibling reviewer calls on the same paper. Only
            # helps when every caller sends the *same* system prompt —
            # the review pipeline does (see
            # :data:`ai_paper_review.review.reviewer_dispatching.SHARED_REVIEWER_SYSTEM`),
            # per-reviewer persona text sits in the user ``text`` block
            # after this one and stays un-cached.
            content = [
                {"type": "document", "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                }, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": user},
            ]
        else:
            content = user
        messages = [{"role": "user", "content": content}]

        if max_tokens > _nonstreaming_limit(self.model):
            return self._complete_streaming(system, messages, max_tokens)

        msg = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        return _reply_text(msg, max_tokens)

    def _complete_streaming(self, system, messages, max_tokens: int) -> str:
        """Stream a high-budget response and return its text, checked the
        same way as the non-streaming path."""
        with self._client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        ) as stream:
            msg = stream.get_final_message()
        return _reply_text(msg, max_tokens)
