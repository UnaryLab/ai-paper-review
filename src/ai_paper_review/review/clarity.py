"""Always-on writing-clarity reviewer.

Runs once per paper — not subject to the top-N selector — and emits a
dedicated review focused strictly on writing quality (flow, terminology,
grammar, figure captions, etc.). The output lives in its own file
(``writing_clarity_review.md``); it is **not** merged into
``review_data.md`` and **not** compared against human reviews during
validation. Writing feedback belongs to the author's own polishing
workflow, not to the persona-calibration feedback loop.

Uses the same markdown comment schema as the persona reviewers so the
parser and UI render it consistently.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ai_paper_review import prompts
from ai_paper_review.llm.clients.base import LLMClient
from ai_paper_review.llm.config import load_config
from ai_paper_review.llm.factory import make_client
from ai_paper_review.llm.utils import provider_supports_pdf

from .constants import EMPTY_COMMENT_RETRIES
from .parsing import _parse_llm_output

logger = logging.getLogger("review_system")


CLARITY_REVIEWER_ID = "G001"
CLARITY_REVIEWER_PERSONA = "Writing Clarity Reviewer"
CLARITY_REVIEWER_DOMAIN = "Writing"


def _user_msg(paper: Dict[str, str], pdf_path: Optional[str]) -> str:
    """Mirror of :func:`ai_paper_review.review.reviewer_dispatching._user_msg`.

    The clarity reviewer shares ``SHARED_REVIEWER_SYSTEM`` with every
    persona reviewer so the provider's prompt cache sees the same
    ``(system + PDF)`` prefix across every review session on this
    paper. The clarity-specific role text moves into the user message
    AFTER the PDF content block, where it only affects the uncached
    tail of the request.
    """
    clarity_role = prompts.load("writing_clarity_system")
    persona_block = (
        f"## Your reviewing role for this paper\n\n"
        f"{clarity_role}\n\n"
        f"---\n\n"
    )
    if pdf_path:
        task = (
            "Please read the attached paper PDF (at the start of this "
            "message) and produce your writing-clarity review now, "
            "following the format the system prompt specifies."
        )
    else:
        task = (
            f"Paper title: {paper['title']}\n\n"
            f"Abstract:\n{paper['abstract']}\n\n"
            f"Paper body (truncated):\n{paper['full_text'][:18000]}"
        )
    return persona_block + task


def _has_valid_comments(p: Dict[str, Any]) -> bool:
    """True when at least one comment carries usable summary/description.
    Mirrors the per-reviewer dispatcher's criterion."""
    comments = p.get("comments") or []
    if not comments:
        return False
    return any(c.get("summary") or c.get("description") for c in comments)


def _call_and_parse(
    user_msg: str,
    llm: LLMClient,
    pdf_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Call the LLM and salvage the output: if it's non-empty but didn't
    produce usable comments (parse exception OR 0 summary/description
    content), run the markdown-repair prompt on the raw output before
    falling through to the outer re-query loop.

    Mirrors :func:`ai_paper_review.review.reviewer_dispatching._call_and_parse`
    — the repair pass is preferred over a fresh query because the LLM
    already generated content; reformatting it is cheaper and usually
    recovers the reviewer's reasoning rather than starting over with a
    whole new pass that may repeat the same mistake.
    """
    # Shared with every persona reviewer so the (system + PDF) prefix
    # can be cached across every review session on this paper. The
    # clarity-specific role text lives inside ``user_msg`` (see
    # :func:`_user_msg`), after the PDF block.
    from .reviewer_dispatching import SHARED_REVIEWER_SYSTEM
    raw = llm.complete(SHARED_REVIEWER_SYSTEM, user_msg, pdf_path=pdf_path)

    if not raw or not raw.strip():
        raise ValueError(
            f"Clarity reviewer: LLM returned empty response "
            f"({len(raw) if raw else 0} chars)."
        )

    parse_err: Optional[Exception] = None
    parsed: Optional[Dict[str, Any]] = None
    try:
        parsed = _parse_llm_output(raw)
    except ValueError as e:
        parse_err = e

    if parsed is not None and _has_valid_comments(parsed):
        parsed["_format_repaired"] = False
        return parsed

    if parse_err is not None:
        reason = f"parse failed ({parse_err})"
    else:
        n_existing = len(parsed.get("comments") or []) if parsed else 0
        reason = (f"{n_existing} comments parsed but none had usable "
                  f"summary/description content")
    logger.warning(
        "Clarity reviewer: %s. Running markdown-repair prompt on raw "
        "output (%d chars) — prioritized over a full re-query. "
        "First 300: %s",
        reason, len(raw), raw[:300],
    )
    repair = prompts.load("markdown_repair_user", raw_output=raw[:8000])
    # Repair pass is pure text-format fixing — don't re-attach the PDF.
    raw2 = llm.complete(prompts.load("markdown_repair_system"), repair)
    repaired = _parse_llm_output(raw2)
    # Tag so the pipeline can count this clarity-reviewer repair
    # alongside the per-reviewer repairs.
    repaired["_format_repaired"] = True
    return repaired


def run_clarity_review(
    paper: Dict[str, str],
    llm: Optional[LLMClient] = None,
    pdf_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the always-on clarity reviewer against one paper.

    Returns the parsed review dict with authoritative ``_reviewer_id``,
    ``_persona``, ``_domain`` overrides applied (same shape as the
    per-reviewer dispatcher produces), so downstream renderers don't
    have to special-case it.

    ``pdf_path`` is only used if the active provider supports PDF
    input — caller is responsible for that capability check.
    """
    if llm is None:
        llm = make_client(load_config(), use_case="review")

    user_msg = _user_msg(paper, pdf_path)

    # Use the module-level ``_has_valid_comments``. ``_call_and_parse``
    # already prioritises a markdown-repair pass over a fresh re-query
    # when the LLM returned non-empty but unusable output; only if that
    # repair pass also fails do we fall through to the outer loop below.
    def _attempt() -> Optional[Dict[str, Any]]:
        try:
            return _call_and_parse(user_msg, llm, pdf_path=pdf_path)
        except Exception as e:
            logger.warning("Clarity reviewer: attempt failed, will retry. (%s)", e)
            return None

    try:
        parsed = _attempt()
        attempt = 0
        while (parsed is None or not _has_valid_comments(parsed)) \
                and attempt < EMPTY_COMMENT_RETRIES:
            attempt += 1
            logger.warning("Clarity reviewer: no valid comments (attempt %d/%d). Retrying…",
                           attempt, EMPTY_COMMENT_RETRIES)
            parsed = _attempt()

        if parsed is None or not _has_valid_comments(parsed):
            logger.warning("Clarity reviewer: giving up after %d retries — "
                           "this review will be empty.", EMPTY_COMMENT_RETRIES)
            if parsed is None:
                parsed = {"comments": []}
        else:
            n = len(parsed.get("comments") or [])
            logger.info("Clarity reviewer: %d comments parsed.", n)
    except Exception as e:
        logger.error("Clarity reviewer failed: %s", e)
        parsed = {"comments": [], "error": str(e)}

    parsed["_reviewer_id"] = CLARITY_REVIEWER_ID
    parsed["_persona"] = CLARITY_REVIEWER_PERSONA
    parsed["_domain"] = CLARITY_REVIEWER_DOMAIN
    return parsed


def node_run_clarity_review(state):
    """Pipeline node — runs the clarity reviewer, stashes result in
    ``state["clarity_review"]``. Orthogonal to the persona pipeline:
    output is **not** added to ``all_comments`` (so it doesn't feed
    clustering) and **not** merged into ``raw_reviews`` (so it doesn't
    reach ``review_data.md`` / validation)."""
    cfg = load_config()
    if state.get("llm_provider"):
        cfg.review_provider = state["llm_provider"]
    if state.get("llm_model"):
        cfg.review_model = state["llm_model"]
    llm = make_client(cfg, use_case="review")
    pdf_path = (
        state.get("pdf_path")
        if provider_supports_pdf(cfg.review_provider)
        else None
    )
    logger.info("Running always-on writing clarity reviewer (provider=%s model=%s, pdf_input=%s)",
                cfg.review_provider, llm.model, "yes" if pdf_path else "no")
    state["clarity_review"] = run_clarity_review(
        state["paper"], llm=llm, pdf_path=pdf_path,
    )
    return state
