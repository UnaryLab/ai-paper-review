"""GoogleClient — Gemini via the google-genai SDK.

The SDK is imported lazily inside ``__init__`` so users don't need the
``google-genai`` package unless they actually select this provider.

When ``pdf_path`` is given, the PDF is attached as a ``Part.from_bytes``
so Gemini sees the original file (figures, tables, layout) rather than
the lossy pypdf-extracted text.

**Explicit context caching.** Gemini supports two caching modes:
implicit (automatic, Gemini 2.5+) and explicit (``client.caches.create``
→ ``cached_content``). This client uses the **explicit** path when a
PDF is attached: on the first call for a given ``pdf_path`` it creates
a context cache holding ``system_instruction`` + PDF, and every
subsequent call reuses the cache via ``cached_content=<name>`` while
only sending the per-reviewer user text. The cache TTL is set to 15
minutes — enough for a full parallel review run. If cache creation
fails (small PDFs below the per-model minimum, quota, network), the
client silently falls back to the un-cached path for the rest of the
run so the review still produces output.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("llm_client")


class GoogleClient:
    """Gemini via the google-genai SDK."""

    def __init__(self, model: str, api_key: str, base_url: Optional[str] = None):
        try:
            from google import genai
            from google.genai import types as gtypes
        except ImportError as e:
            raise ImportError("Install the Google GenAI SDK: pip install google-genai") from e
        self._client = genai.Client(api_key=api_key)
        self._types = gtypes
        self.model = model
        # pdf_path → cache resource name (or None sentinel if creation
        # failed previously so subsequent calls don't keep retrying).
        self._cache_names: Dict[str, Optional[str]] = {}
        self._cache_lock = threading.Lock()

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        if pdf_path:
            cache_name = self._get_or_create_cache(system, pdf_path)
            if cache_name:
                # Cached-content path: the (system + PDF) prefix lives
                # on Gemini's side already; this request only ships
                # the per-reviewer user text.
                resp = self._client.models.generate_content(
                    model=self.model,
                    config=self._types.GenerateContentConfig(
                        cached_content=cache_name,
                        max_output_tokens=max_tokens,
                    ),
                    contents=user,
                )
                return (resp.text or "").strip()
            # Fall through to the un-cached PDF path when cache
            # creation failed — behaviour matches the pre-cache code.
            pdf_part = self._types.Part.from_bytes(
                data=Path(pdf_path).read_bytes(),
                mime_type="application/pdf",
            )
            contents = [pdf_part, user]
        else:
            contents = user
        resp = self._client.models.generate_content(
            model=self.model,
            config=self._types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
            ),
            contents=contents,
        )
        return (resp.text or "").strip()

    def _get_or_create_cache(self, system: str, pdf_path: str) -> Optional[str]:
        """Return a cached-content resource name for ``(system, pdf)``,
        creating one on the first call. Thread-safe because the review
        dispatcher runs many reviewers in parallel — without the lock
        every parallel reviewer would race to create its own cache.

        Returns ``None`` when creation fails (PDF below Gemini's
        minimum cacheable size, quota exhaustion, network error, etc.).
        The None is memoised so subsequent calls for the same PDF
        don't keep pinging the API.
        """
        with self._cache_lock:
            if pdf_path in self._cache_names:
                return self._cache_names[pdf_path]
            try:
                pdf_part = self._types.Part.from_bytes(
                    data=Path(pdf_path).read_bytes(),
                    mime_type="application/pdf",
                )
                cached = self._client.caches.create(
                    model=self.model,
                    config=self._types.CreateCachedContentConfig(
                        contents=[pdf_part],
                        system_instruction=system,
                        ttl="900s",
                    ),
                )
                self._cache_names[pdf_path] = cached.name
                logger.info(
                    "Gemini: created explicit context cache %s for %s",
                    cached.name, pdf_path,
                )
                return cached.name
            except Exception as e:
                logger.warning(
                    "Gemini: explicit context-cache creation failed for "
                    "%s (%s: %s). Falling back to un-cached PDF for the "
                    "rest of this run — review still proceeds, just "
                    "without prefix caching. Typical cause: PDF is below "
                    "the model's minimum cacheable token count.",
                    pdf_path, type(e).__name__, e,
                )
                self._cache_names[pdf_path] = None
                return None

    def cleanup_caches(self) -> None:
        """Best-effort delete of every explicit context cache this
        client created. Gemini caches have a TTL so they auto-expire,
        but long-lived processes (the web server) accumulate one cache
        per paper until TTL. Call between runs to reclaim storage
        proactively; failures are logged, never raised."""
        for pdf_path, name in list(self._cache_names.items()):
            if not name:
                continue
            try:
                self._client.caches.delete(name=name)
                logger.debug("Gemini: deleted cache %s", name)
            except Exception as e:
                logger.warning(
                    "Gemini: failed to delete cache %s (%s: %s)",
                    name, type(e).__name__, e,
                )
        self._cache_names.clear()
