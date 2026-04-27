"""OpenAIClient — Chat Completions API.

Also serves every OpenAI-compatible provider:

* ``xai``                → ``https://api.x.ai/v1`` (default in _DEFAULT_BASE_URLS)
* ``github``             → ``https://models.github.ai/inference``
* ``openai_compatible``  → user-supplied base_url (Ollama, Azure, Together, ...)

The SDK is imported lazily inside ``__init__`` so users don't need the
``openai`` package unless they actually select this provider.

PDF passthrough (``pdf_path=...``) is attempted only when the client is
pointed at an endpoint known to honour the OpenAI-style ``file`` content
block on Chat Completions: OpenAI-proper. xAI technically supports PDFs
but only via a separate upload-then-reference path on its Responses
API (``POST /v1/files`` → ``{"type":"input_file","file_id":...}`` on
``/v1/responses``), not inline on Chat Completions. Implementing that
would need a dedicated xAI client; until then, xAI / GitHub Models /
Ollama / generic ``openai_compatible`` base URLs all fall back to the
text path — see :func:`ai_paper_review.llm.utils.provider_supports_pdf`.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional


def _pdf_cache_key(pdf_path: str) -> str:
    """Short stable string for OpenAI's ``prompt_cache_key`` routing
    hint. Same PDF path → same key → OpenAI routes every reviewer
    call on that paper to the same backend, maximising cache-hit rate
    on the ``(system + PDF)`` prefix. 16 hex chars are plenty — the
    key is opaque to us; only equality matters."""
    return hashlib.sha256(str(pdf_path).encode()).hexdigest()[:16]


def _supports_file_block(base_url: Optional[str]) -> bool:
    """True when the endpoint is known to accept OpenAI-style ``file``
    content blocks for PDFs on Chat Completions: only OpenAI-proper
    (no base_url set, or ``openai.com``). Other OpenAI-compatible base
    URLs (xAI, GitHub Models, Ollama, Together, local proxies) either
    don't implement the block at all, or implement it behind a
    different endpoint (xAI's Responses API) that this client can't
    reach. All return False and the caller falls back to text."""
    if not base_url:
        return True  # default is OpenAI
    return "openai.com" in base_url


class OpenAIClient:
    """Also used for xAI (Grok) and any OpenAI-compatible endpoint."""

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None,
                 extra_headers: Optional[Dict[str, str]] = None):
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
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        if pdf_path and _supports_file_block(self._base_url):
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
            "max_tokens": max_tokens,
        }
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
        return resp.choices[0].message.content or ""
