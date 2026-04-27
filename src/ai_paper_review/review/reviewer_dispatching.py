"""Per-reviewer LLM dispatch.

Each selected reviewer gets one LLM call (system prompt = persona, user
message = paper text). Calls run in parallel via ``ThreadPoolExecutor``
with the concurrency cap from ``LLMConfig.max_concurrent``.

Three layers of resilience wrap the bare LLM call:

1. **Repair retry** in :func:`_call_and_parse` — one shot at re-asking
   the LLM to fix its own malformed output.
2. **Empty-comment retry** in :func:`_run_single_reviewer` — up to
   :data:`EMPTY_COMMENT_RETRIES` extra attempts when parsing succeeds
   but yields zero usable comments.
3. **Rate-limit retry** in :class:`RetryClient` (composed by
   :func:`make_client`) — exponential backoff on 429 / 5xx.
"""
from __future__ import annotations

import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from ai_paper_review import prompts
from ai_paper_review.llm.clients.base import LLMClient
from ai_paper_review.llm.config import load_config
from ai_paper_review.llm.factory import make_client
from ai_paper_review.llm.utils import provider_supports_pdf

from .constants import EMPTY_COMMENT_RETRIES
from .parsing import _parse_llm_output
from .reviewer_db import Reviewer

logger = logging.getLogger("review_system")


# The LLM ``system`` parameter is SHARED across every review session
# (N persona reviewers + the clarity reviewer, all on the same paper).
# Keeping it stable lets provider-side prompt caching hit on the
# ``(system_prompt, PDF)`` prefix: the first review seeds the cache,
# every subsequent review reuses it. Per-reviewer differences (persona,
# domain, lens, priorities) move into the user message, AFTER the PDF
# block so they don't perturb the cached prefix.
SHARED_REVIEWER_SYSTEM = prompts.load("shared_reviewer_system")


def _user_msg(
    reviewer: Reviewer,
    paper: Dict[str, str],
    pdf_path: Optional[str],
) -> str:
    """Build the per-reviewer user message.

    Layout (the provider clients put the PDF content block at the very
    start of the user message; this string is what comes AFTER it):

        ## Your reviewing role for this paper
        <reviewer.system_prompt — persona expertise, lens, priorities, …>

        ---

        <task line: "review the attached PDF" or the paper body text>

    The persona block is no longer an LLM ``system`` prompt; the
    shared system sets the role and format constraints, and the
    per-reviewer persona sits in the user message so different
    reviewers share the cached prefix (shared system + PDF) while
    varying only the tail of the user message.
    """
    persona_block = (
        f"## Your reviewing role for this paper\n\n"
        f"{reviewer.system_prompt}\n\n"
        f"---\n\n"
    )
    if pdf_path:
        task = (
            "Please read the attached paper PDF (at the start of this "
            "message) and produce your review now, following the format "
            "the system prompt specifies."
        )
    else:
        task = (
            f"Paper title: {paper['title']}\n\n"
            f"Abstract:\n{paper['abstract']}\n\n"
            f"Paper body (truncated):\n{paper['full_text'][:18000]}"
        )
    return persona_block + task


def _has_valid_comments(p: Dict[str, Any]) -> bool:
    """A review is useful only if at least one comment has summary OR
    description content. LLMs sometimes emit a rating template with
    empty comment bodies — parsing succeeds, but there's nothing for
    clustering / ranking to latch onto."""
    comments = p.get("comments") or []
    if not comments:
        return False
    return any(c.get("summary") or c.get("description") for c in comments)


def _call_and_parse(
    reviewer: Reviewer,
    user_msg: str,
    llm: LLMClient,
    pdf_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Call the LLM, parse the response, and if the output is non-empty
    but didn't produce usable comments (either a parse failure OR a
    parse that yielded 0 summary/description content), run a
    markdown-repair pass on the raw output before giving up.

    The repair pass is preferred over the caller's full re-query loop
    when the LLM *did* produce content: reformatting cached output is
    cheaper and more likely to recover the reviewer's reasoning than
    asking the LLM to start over, which may hit the same template /
    content-filter / empty-body behavior that produced the first dud.
    If repair still can't salvage valid comments, the result flows up
    and the caller's outer loop handles the full re-query.

    Empty-raw is treated specially: re-calling an LLM that returned 0
    chars almost always hits the same failure (quota, auth, content
    filter), so we raise immediately with a clear error.
    """
    # System prompt is shared across every reviewer on this paper so the
    # provider's prefix cache (Anthropic ``cache_control``, OpenAI auto
    # caching) hits on the (system + PDF) prefix across all N reviewers.
    # The per-reviewer persona sits inside ``user_msg`` after the PDF —
    # see :func:`_user_msg` for the layout.
    raw = llm.complete(SHARED_REVIEWER_SYSTEM, user_msg, pdf_path=pdf_path)

    if not raw or not raw.strip():
        raise ValueError(
            f"Reviewer {reviewer.id}: LLM returned empty response "
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
        "Reviewer %s: %s. Running markdown-repair prompt on raw output "
        "(%d chars) — prioritized over a full re-query. First 300: %s",
        reviewer.id, reason, len(raw), raw[:300],
    )
    repair_prompt = prompts.load("markdown_repair_user", raw_output=raw[:8000])
    # Repair pass is pure text-format fixing — don't re-attach the PDF.
    raw2 = llm.complete(prompts.load("markdown_repair_system"), repair_prompt)
    repaired = _parse_llm_output(raw2)
    # Tag so the pipeline can count how many reviewers needed a
    # markdown-repair pass to produce usable output. Surfaced in the
    # final report and the web review-result page as an operational
    # quality signal (many repairs → consider a different model or
    # tightening the reviewer persona prompt).
    repaired["_format_repaired"] = True
    return repaired


def _run_single_reviewer(
    reviewer: Reviewer,
    paper: Dict[str, str],
    llm: LLMClient,
    pdf_path: Optional[str] = None,
) -> Dict[str, Any]:
    user_msg = _user_msg(reviewer, paper, pdf_path)
    try:
        # A real review must produce at least one comment with a summary or
        # description (see module-level ``_has_valid_comments``). On the
        # "no valid comments" path, ``_call_and_parse`` already attempted
        # a markdown-repair pass on the raw output; only if repair also
        # failed do we fall through to this outer loop's full re-query.
        def _attempt() -> Optional[Dict[str, Any]]:
            try:
                return _call_and_parse(reviewer, user_msg, llm, pdf_path=pdf_path)
            except Exception as e:
                logger.warning(
                    "Reviewer %s: parse/call failed on this attempt — "
                    "will retry. (%s)", reviewer.id, e,
                )
                return None

        parsed = _attempt()
        attempt = 0
        while (parsed is None or not _has_valid_comments(parsed)) \
                and attempt < EMPTY_COMMENT_RETRIES:
            attempt += 1
            if parsed is None:
                reason = "the LLM/parse produced no review at all"
            else:
                n_existing = len(parsed.get("comments") or [])
                reason = (f"{n_existing} comments parsed but none had a "
                          f"summary or description")
            logger.warning(
                "Reviewer %s: %s (attempt %d/%d). Retrying…",
                reviewer.id, reason, attempt, EMPTY_COMMENT_RETRIES,
            )
            parsed = _attempt()

        if parsed is None or not _has_valid_comments(parsed):
            logger.warning(
                "Reviewer %s: still no valid comments after %d retries — "
                "giving up, this reviewer will contribute 0 comments to "
                "clustering.",
                reviewer.id, EMPTY_COMMENT_RETRIES,
            )
            if parsed is None:
                parsed = {"comments": []}
        else:
            # Diagnostic: clustering uses summary+description for embeddings,
            # so partial extraction degrades cluster quality (visible as
            # too many singleton clusters).
            comments = parsed["comments"]
            n_total = len(comments)
            n_with_summary = sum(1 for c in comments if c.get("summary"))
            n_with_desc = sum(1 for c in comments if c.get("description"))
            if n_with_summary < n_total or n_with_desc < n_total:
                logger.warning(
                    "Reviewer %s: parsed %d comments but only %d have summary and "
                    "%d have description. Clustering quality will suffer — likely "
                    "the LLM's comment format deviates from the expected "
                    "`- **Summary:** ...` pattern. First comment: %r",
                    reviewer.id, n_total, n_with_summary, n_with_desc,
                    {k: (v[:100] if isinstance(v, str) else v) for k, v in comments[0].items()},
                )
            else:
                logger.info("Reviewer %s: %d comments parsed cleanly (all have summary+description).",
                            reviewer.id, n_total)

        parsed["_reviewer_id"] = reviewer.id
        parsed["_persona"] = reviewer.persona
        parsed["_domain"] = reviewer.domain
        return parsed
    except Exception as e:
        logger.error("Reviewer %s failed: %s", reviewer.id, e)
        return {
            "_reviewer_id": reviewer.id,
            "_persona": reviewer.persona,
            "_domain": reviewer.domain,
            "error": str(e),
            "comments": [],
        }


def node_run_reviewers(
    state,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
):
    """Run selected reviewers in parallel with throttled dispatch.

    ``on_progress``, when given, is called with a dict
    ``{current, total, reviewer_id, persona, event, n_comments}`` on
    every dispatch and completion. ``event`` is ``"dispatched"`` or
    ``"done"``. The web UI uses this to update the status page in real time.
    """
    cfg = load_config()
    if state.get("llm_provider"):
        cfg.review_provider = state["llm_provider"]
    if state.get("llm_model"):
        cfg.review_model = state["llm_model"]
    llm = make_client(cfg, use_case="review")

    # PDF-native providers (Anthropic, OpenAI, Google, Claude SDK) get
    # the PDF itself; text-only ones (xAI, GitHub Models, openai_compatible,
    # Copilot SDK) fall back to the pre-extracted paper text in state["paper"].
    pdf_path = (
        state.get("pdf_path")
        if provider_supports_pdf(cfg.review_provider)
        else None
    )

    n_selected = len(state["selected"])
    logger.info("Review LLM: provider=%s model=%s — running %d reviewers "
                "(max_concurrent=%d, delay=%.1fs, retries=%d, pdf_input=%s)",
                cfg.review_provider, llm.model, n_selected,
                cfg.max_concurrent, cfg.request_delay, cfg.max_retries,
                "yes" if pdf_path else "no")

    results: List[Dict[str, Any]] = []
    done_count = 0

    with ThreadPoolExecutor(max_workers=cfg.max_concurrent) as ex:
        futs = {}
        for i, (r, _score) in enumerate(state["selected"]):
            if i > 0 and cfg.request_delay > 0:
                _time.sleep(cfg.request_delay)
            fut = ex.submit(_run_single_reviewer, r, state["paper"], llm, pdf_path)
            futs[fut] = (i, r)
            logger.info("Dispatched reviewer %d/%d: %s (%s)",
                        i + 1, n_selected, r.id, r.persona)
            if on_progress:
                on_progress({
                    "current": i + 1,
                    "total": n_selected,
                    "reviewer_id": r.id,
                    "persona": r.persona,
                    "event": "dispatched",
                    "n_comments": 0,
                })

        for fut in as_completed(futs):
            _i, r = futs[fut]
            rv = fut.result()
            results.append(rv)
            done_count += 1
            n_comments = len(rv.get("comments", []))
            logger.info("Completed reviewer %d/%d: %s (%s) — %d comments",
                        done_count, n_selected, r.id, r.persona, n_comments)
            if on_progress:
                on_progress({
                    "current": done_count,
                    "total": n_selected,
                    "reviewer_id": r.id,
                    "persona": r.persona,
                    "event": "done",
                    "n_comments": n_comments,
                })

    state["raw_reviews"] = results

    flat: List[Dict[str, Any]] = []
    for rv in results:
        for c in rv.get("comments", []):
            c["_reviewer_id"] = rv["_reviewer_id"]
            c["_persona"] = rv["_persona"]
            c["_domain"] = rv["_domain"]
            flat.append(c)
    state["all_comments"] = flat
    logger.info("Collected %d comments from %d reviewers", len(flat), len(results))
    return state
