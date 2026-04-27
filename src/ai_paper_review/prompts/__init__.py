"""
ai_paper_review.prompts
=======================

All LLM prompts used anywhere in the pipeline are externalized to
``<name>.md`` files in this package, one prompt per file. Names follow
the convention ``<stage>_<role>.md`` where ``<role>`` is ``system`` or
``user``:

    human_review_extraction_system.md   → validation/conversion.py
    markdown_repair_system.md           → review.py (repair retry)
    markdown_repair_user.md             → review.py (repair retry — has {raw_output})
    batch_alignment_system.md           → validation.py
    batch_alignment_user.md             → validation.py (has {human_block}, ...)
    shared_reviewer_system.md           → reviewer_dispatching.py + clarity.py
                                          (THE LLM ``system`` argument for every
                                          review session on a paper — identical
                                          across N persona reviewers + clarity so
                                          the provider's prompt cache hits on the
                                          (system + PDF) prefix; per-reviewer
                                          persona text lives in the user message)
    writing_clarity_system.md           → clarity.py (loaded INTO the user message
                                          as the clarity reviewer's role/scope,
                                          no longer an LLM system prompt)

Edit the ``.md`` files to tune prompts; no Python change required.

Prompts that need runtime values use ``{placeholder}`` syntax
(Python :func:`str.format`). Pass values as keyword arguments to
:func:`load`.
"""
from __future__ import annotations

from importlib.resources import files
from typing import Any


def load(name: str, **kwargs: Any) -> str:
    """Read ``prompts/<name>.md`` and substitute ``{placeholder}`` values.

    If ``kwargs`` is empty the file is returned unchanged — safe for
    prompts with no placeholders. Raises ``KeyError`` only when the
    prompt declares a placeholder that the caller forgot to pass, which
    is the error we want: the caller needs to know.
    """
    text = files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return text.format(**kwargs) if kwargs else text
