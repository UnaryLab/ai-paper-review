"""LLMClient protocol — the single interface every provider client implements.

The concrete clients in sibling modules (anthropic.py, openai.py,
google.py, copilot.py, claude.py) are structural duck-typed
implementations of this protocol; :func:`ai_paper_review.llm.factory.make_client`
hands one back wrapped in :class:`RetryClient`.

``pdf_path`` is optional. Clients that support native PDF input
(see :func:`ai_paper_review.llm.utils.provider_supports_pdf`) attach
the file directly when given; clients that don't ignore it and fall
back to whatever text came through ``user``. Callers that don't want
PDF passthrough just omit the argument.
"""
from __future__ import annotations

from typing import Optional, Protocol


class LLMClient(Protocol):
    model: str
    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str: ...
