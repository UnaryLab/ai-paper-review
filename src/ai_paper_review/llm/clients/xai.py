"""XaiClient — xAI Grok via the OpenAI-compatible API.

Text-only calls go through the standard OpenAI Chat Completions
endpoint, same as the shared :class:`OpenAIClient`. PDF calls use xAI's
**Responses API** path because Grok does not accept the inline
``{"type":"file","file_data":...}`` content block on Chat Completions —
only the upload-then-reference pattern:

1. ``POST /v1/files`` (multipart, ``purpose="assistants"``) → returns
   ``{"id": "file_..."}``.
2. ``POST /v1/responses`` with an ``{"type":"input_file","file_id":...}``
   content block on the Responses shape.

We reuse the ``openai`` Python SDK — it's drop-in compatible with xAI's
Files + Responses endpoints when pointed at ``https://api.x.ai/v1``.
That keeps the client small: no custom HTTP / multipart code.

Uploaded file IDs are cached per ``pdf_path`` on the client instance so
that multiple reviewers running in parallel against the same paper only
upload it once. xAI does not auto-delete uploaded files — use the xAI
dashboard or call :meth:`XaiClient.cleanup_uploaded_files` to purge.
Only models with agentic tool calling accept file attachments
(e.g. ``grok-4.20``, ``grok-4.5``, ``grok-4.6``, ``grok-4.7``); other
models return an API error for a PDF, which the pipeline's retry
wrapper surfaces.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, Optional

from .base import ReplyBlockedError
from .openai import chat_reply_text

logger = logging.getLogger("llm_client")

_DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"


def _responses_reply_text(resp, model: str, max_tokens: int) -> str:
    """Text of a Responses API reply (``output_text`` joins every
    ``output_text`` block). Raises ReplyBlockedError when the model
    refused, a content filter stopped it, or it used the whole budget
    without text."""
    for item in getattr(resp, "output", None) or []:
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) == "refusal":
                raise ReplyBlockedError(
                    f"{model} refused the request: {getattr(block, 'refusal', '')}")
    reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
    if reason == "content_filter":
        raise ReplyBlockedError(
            f"{model} reply was stopped by the content filter "
            f"(incomplete_details.reason=content_filter).")
    output = resp.output_text or ""
    if not output and reason == "max_output_tokens":
        raise ReplyBlockedError(
            f"{model} used up its output budget (max_tokens="
            f"{max_tokens}) before writing any text. Reasoning tokens "
            f"count against this budget; raise max_tokens."
        )
    return output


class XaiClient:
    """xAI Grok via the OpenAI-compatible Chat Completions + Responses APIs."""

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "The xAI client uses the OpenAI SDK under the hood. "
                "Install it with `pip install openai`."
            ) from e
        self._client = OpenAI(api_key=api_key, base_url=base_url or _DEFAULT_XAI_BASE_URL)
        self._base_url = base_url or _DEFAULT_XAI_BASE_URL
        self.model = model
        # pdf_path → uploaded file_id; avoids re-uploading the same PDF
        # across N parallel reviewers on the same paper.
        self._uploaded_files: Dict[str, str] = {}
        self._upload_lock = threading.Lock()

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        if pdf_path:
            return self._complete_with_pdf(system, user, max_tokens, pdf_path)
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=max_tokens,
        )
        return chat_reply_text(resp.choices[0], self.model, max_tokens)

    def _complete_with_pdf(
        self,
        system: str,
        user: str,
        max_tokens: int,
        pdf_path: str,
    ) -> str:
        """Upload (or reuse a cached) file_id, then call the Responses API."""
        file_id = self._get_or_upload(pdf_path)
        # xAI's Responses shape: system prompt in top-level ``instructions``;
        # user message's content mixes ``input_text`` + ``input_file`` blocks.
        # ``attachment_search`` is auto-activated server-side when a file is
        # attached — do NOT pass it explicitly via ``tools=[...]``.
        resp = self._client.responses.create(
            model=self.model,
            instructions=system,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user},
                    {"type": "input_file", "file_id": file_id},
                ],
            }],
            max_output_tokens=max_tokens,
        )
        return _responses_reply_text(resp, self.model, max_tokens)

    def _get_or_upload(self, pdf_path: str) -> str:
        """Return a cached file_id for this PDF, uploading it once if needed.

        Thread-safe because the reviewer dispatcher runs many reviewers
        concurrently in a ThreadPoolExecutor — without the lock, each
        parallel call would race to upload the same PDF.
        """
        with self._upload_lock:
            cached = self._uploaded_files.get(pdf_path)
            if cached:
                return cached
            with open(pdf_path, "rb") as f:
                uploaded = self._client.files.create(file=f, purpose="assistants")
            self._uploaded_files[pdf_path] = uploaded.id
            logger.info("xai: uploaded %s → file_id=%s (%d bytes)",
                        pdf_path, uploaded.id, Path(pdf_path).stat().st_size)
            return uploaded.id

    def cleanup_uploaded_files(self) -> None:
        """Best-effort delete of every file this client uploaded.

        xAI doesn't auto-purge uploaded files, so long-lived processes
        that run many reviews should call this between runs to avoid
        accumulating storage on the xAI account. Failures are logged
        but never raised — cleanup must not mask the primary result.
        """
        for pdf_path, file_id in list(self._uploaded_files.items()):
            try:
                self._client.files.delete(file_id)
                logger.debug("xai: deleted uploaded file %s", file_id)
            except Exception as e:
                logger.warning("xai: failed to delete uploaded file %s "
                               "(%s: %s)", file_id, type(e).__name__, e)
        self._uploaded_files.clear()
