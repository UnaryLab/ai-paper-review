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


class FatalLLMError(RuntimeError):
    """A failure that repeating the same call cannot fix: a hard usage
    limit, a missing CLI, or failed authentication. ``RetryClient`` and
    the reviewers' empty-comment loops never retry it; it stops the run.
    """


class ReplyBlockedError(RuntimeError):
    """The model refused, a safety filter blocked the prompt or reply, or
    the output budget ran out before any text. Asking again gives the same
    result, so ``RetryClient`` and the clarity re-run loop do not retry it.
    """
