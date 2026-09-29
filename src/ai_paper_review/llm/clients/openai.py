"""OpenAIClient — Chat Completions API.

Also serves ``openai_compatible_api``: any OpenAI-protocol endpoint at a
user-supplied base_url (Ollama, Azure, Together, ...). ``xai_api`` has its
own :class:`~ai_paper_review.llm.clients.xai.XaiClient`.

The SDK is imported lazily inside ``__init__`` so users don't need the
``openai`` package unless they actually select this provider.

PDF passthrough (``pdf_path=...``) uses the OpenAI-style ``file``
content block on Chat Completions, sent only when
:func:`ai_paper_review.llm.utils.provider_supports_pdf` allows it for
this client's provider and base_url (``openai_api`` on OpenAI's own
endpoint). Every other endpoint gets the extracted text in ``user``.

``openai_api`` on OpenAI's own endpoint or on Azure OpenAI sends the
output budget as ``max_completion_tokens`` (reasoning models reject
``max_tokens``); every other endpoint (``openai_compatible_api``, or
``openai_api`` pointed at a proxy such as LiteLLM) sends ``max_tokens``,
which other OpenAI-protocol servers expect.

A refusal, a ``content_filter`` stop, or a ``length`` stop with no text
raises :class:`ReplyBlockedError`.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ..utils import is_openai_endpoint, provider_supports_pdf
from .base import ReplyBlockedError


def _pdf_cache_key(pdf_path: str) -> str:
    """Short stable string for OpenAI's ``prompt_cache_key`` routing
    hint. Same PDF path → same key → OpenAI routes every reviewer
    call on that paper to the same backend, maximising cache-hit rate
    on the ``(system + PDF)`` prefix. 16 hex chars are plenty — the
    key is opaque to us; only equality matters."""
    return hashlib.sha256(str(pdf_path).encode()).hexdigest()[:16]


def _uses_max_completion_tokens(provider: str, base_url: Optional[str]) -> bool:
    """OpenAI and Azure OpenAI accept only ``max_completion_tokens`` for
    reasoning models on Chat Completions; other servers expect ``max_tokens``."""
    if provider != "openai_api":
        return False
    host = (urlparse(base_url).hostname or "") if base_url else ""
    return is_openai_endpoint(base_url) or host.endswith(".openai.azure.com")


def chat_reply_text(choice, model: str, max_tokens: int) -> str:
    """Text of a Chat Completions choice. Raises ReplyBlockedError when
    the model refused, a content filter stopped it, or it used the whole
    budget without text."""
    refusal = getattr(choice.message, "refusal", None)
    if refusal:
        raise ReplyBlockedError(f"{model} refused the request: {refusal}")
    if choice.finish_reason == "content_filter":
        raise ReplyBlockedError(
            f"{model} reply was stopped by the content filter "
            f"(finish_reason=content_filter).")
    content = choice.message.content or ""
    if not content and choice.finish_reason == "length":
        raise ReplyBlockedError(
            f"{model} used up its output budget (max_tokens="
            f"{max_tokens}) before writing any text. Reasoning tokens "
            f"count against this budget; raise max_tokens."
        )
    return content


class OpenAIClient:
    """Also used for xAI (Grok) and any OpenAI-compatible endpoint."""

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 provider: str = "openai_api"):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("Install the OpenAI SDK: pip install openai") from e
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        self._client = OpenAI(**kwargs)
        self._base_url = base_url
        self._provider = provider
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        if pdf_path and provider_supports_pdf(self._provider, self._base_url):
            pdf_b64 = base64.standard_b64encode(
                Path(pdf_path).read_bytes()
            ).decode("ascii")
            user_content: Any = [
                {"type": "file", "file": {
                    "filename": Path(pdf_path).name,
                    "file_data": f"data:application/pdf;base64,{pdf_b64}",
                }},
                {"type": "text", "text": user},
            ]
        else:
            user_content = user
        create_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_content},
            ],
        }
        token_key = ("max_completion_tokens"
                     if _uses_max_completion_tokens(self._provider, self._base_url)
                     else "max_tokens")
        create_kwargs[token_key] = max_tokens
        # When we have a PDF, hint OpenAI's cache router to send every
        # reviewer call on this paper to the same backend — OpenAI's
        # automatic prompt cache works on server-local prefix hashes,
        # so consistent routing maximises cache-hit rate. ``extra_body``
        # passes the field through even on older SDK versions that
        # don't type it as a named parameter.
        if pdf_path:
            create_kwargs["extra_body"] = {
                "prompt_cache_key": _pdf_cache_key(pdf_path),
            }
        resp = self._client.chat.completions.create(**create_kwargs)
        return chat_reply_text(resp.choices[0], self.model, max_tokens)
