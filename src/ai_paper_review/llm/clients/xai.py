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
Only grok-4-class models accept file attachments
(``grok-4`` / ``grok-4-fast`` / ``grok-4.20-reasoning``); picking
grok-3 / grok-2-vision with a PDF will return an error from the API
and the pipeline's retry wrapper will surface it.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("llm_client")

_DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"


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
        return resp.choices[0].message.content or ""

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
        # The OpenAI SDK exposes ``output_text`` as a convenience that
        # concatenates every ``output_text`` content block from the
        # response — standard across xAI's + OpenAI's Responses API.
        output = getattr(resp, "output_text", None)
        if output is None:
            # Fall back to digging through the response structure manually
            # in case the SDK version doesn't expose the convenience field.
            chunks = []
            for item in getattr(resp, "output", None) or []:
                for block in getattr(item, "content", None) or []:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        chunks.append(text)
            output = "".join(chunks)
        return output or ""

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
