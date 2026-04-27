"""LLM-based conversion of unstructured human reviews into AI-review markdown.

Lives inside ``validation`` because its only consumer is the validation
pipeline — converted human reviews are the ground-truth input the
aligner compares against.

This module is **LLM-only**. There is no heuristic parser, no format
dispatcher, no ``--no-llm`` escape hatch. An LLM is prompted to emit
the reshaped reviews in the same single-AI-reviewer markdown format
produced by the reviewer pipeline (see ``review_data.md``), and we
parse that markdown back into a canonical ``{"actual_reviews": [...]}``
dict.

If the LLM call or its markdown parsing fails, this module raises — the
caller is expected to surface the error, not silently fall back to
weaker extraction.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from ai_paper_review import prompts
from ai_paper_review.review.parsing import parse_multi_review_markdown

from .constants import (
    RECOMMENDATION_VOCAB,
    SEVERITY_VOCAB,
)

logger = logging.getLogger("convert_reviews")


def _build_system_prompt(category_vocab: "list[str]") -> str:
    """Render the human-review-extraction system prompt with the
    category vocab pulled from the active reviewer DB. Kept as a plain
    function (not a module-level cache) so different DBs produce
    different prompts in the same process."""
    return prompts.load(
        "human_review_extraction_system",
        recommendation_vocab=", ".join(RECOMMENDATION_VOCAB),
        severity_vocab=", ".join(SEVERITY_VOCAB),
        category_vocab=", ".join(category_vocab),
    )


def llm_extract(
    text: str,
    category_vocab: "list[str]",
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
    run_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """Extract structured review data from unstructured human review text
    using the configured LLM.

    This is the **only** extraction path. There is no heuristic fallback.
    If the LLM call or its markdown parsing fails, this function raises
    — the caller should surface the error so the user can retry with a
    different provider/model.

    The provider and model come from ``config.yaml`` (or env-var
    overrides) via ``llm.make_client()`` — works with Anthropic, OpenAI,
    Google, xAI, or any OpenAI-compatible endpoint.

    Per-call overrides (``provider_override``, ``model_override``) let
    callers (e.g. the web UI) pick a provider for a single conversion
    without mutating global state.

    When ``run_dir`` is provided, the LLM's raw output is written to
    ``run_dir/actual_raw_llm.md`` *before* parsing, so users can inspect
    the model's literal response if the downstream parse fails or the
    structured result looks wrong.

    Returns ``{"actual_reviews": [...]}`` — same shape the downstream
    ``normalize_extracted`` and validator expect.
    """
    from ai_paper_review.llm.config import load_config
    from ai_paper_review.llm.factory import make_client
    cfg = load_config()
    if provider_override:
        cfg.validation_provider = provider_override
    if model_override:
        cfg.validation_model = model_override
    client = make_client(cfg, use_case="validation")

    system_prompt = _build_system_prompt(category_vocab)
    logger.info("Calling %s/%s to extract human-review structure (%d chars input)",
                cfg.resolve_provider("validation"), client.model, len(text))
    raw = client.complete(system=system_prompt, user=text, max_tokens=8000)

    # Persist the raw response BEFORE parsing so users can inspect what
    # the model actually returned when the structured result looks wrong.
    # Provenance isn't prepended here — the validation run's metadata
    # banner lives only on validation_report.md.
    if run_dir is not None:
        try:
            import pathlib
            rd = pathlib.Path(run_dir)
            rd.mkdir(parents=True, exist_ok=True)
            body = (
                "# Raw LLM output — human-review conversion\n"
                "\n"
                f"**Input size:** {len(text)} chars  \n"
                f"**Output size:** {len(raw)} chars\n"
                "\n"
                "This is the verbatim text the LLM returned when asked to "
                "convert the unstructured human review into AI-review-format "
                "markdown. The downstream parser attempts to turn it into "
                "`actual_converted.md`. If that conversion looks wrong, the "
                "root cause is usually visible here.\n"
                "\n"
                "---\n"
                "\n"
            )
            (rd / "actual_raw_llm.md").write_text(body + raw)
            logger.info("Wrote raw LLM conversion output to %s",
                        rd / "actual_raw_llm.md")
        except Exception as e:
            logger.warning("Failed to write actual_raw_llm.md to %s "
                           "(%s: %s) — continuing.",
                           run_dir, type(e).__name__, e)

    extracted = parse_llm_markdown(raw)

    if not extracted.get("actual_reviews"):
        raise ValueError(
            f"LLM returned no parseable review blocks ({len(raw)} chars output). "
            f"The model may have ignored the markdown format spec. "
            f"First 300 chars of output: {raw[:300]!r}. "
            f"Try a different provider/model."
        )

    # Coverage sanity check — a low ratio means the LLM paraphrased or
    # dropped content rather than reshaping verbatim.
    input_chars = len(text.strip())
    out_chars = sum(
        len(c.get("text", "") or "")
        for rv in extracted["actual_reviews"]
        for c in rv.get("comments", [])
    )
    ratio = (out_chars / input_chars) if input_chars else 1.0
    logger.info(
        "Extraction coverage: %d / %d input chars preserved in Description "
        "(%.0f%%)", out_chars, input_chars, 100 * ratio,
    )
    if ratio < 0.30 and input_chars > 500:
        logger.warning(
            "Extraction preserved only %.0f%% of input in Description fields — "
            "the LLM may have dropped reviewer content. Consider a stronger model.",
            100 * ratio,
        )

    return extracted


def parse_llm_markdown(raw: str) -> Dict[str, Any]:
    """Parse the LLM's markdown output (single-AI-reviewer format, one
    block per reviewer, separated by ``---``) into the canonical
    ``{"actual_reviews": [...]}`` dict that ``normalize_extracted``
    expects.

    Strips any stray code fences around the LLM's output before parsing.
    Converts the pipeline's AI-review field names (``description``) back
    to the human-review field names (``text``) used by the validator.

    Also **sanitizes reviewer and comment IDs** to ``^[A-Za-z0-9_-]+$`` so
    downstream string-manipulation code (especially the alignment parser
    in :mod:`ai_paper_review.validation.alignment`) can capture full IDs
    with simple word-character regexes. Without this, a reviewer labelled
    ``"Reviewer #1"`` ends up with comment IDs like ``"Reviewer #1-C1"``
    — the space and ``#`` truncate regex matches and the alignment flow
    drops every match silently, producing "0 similarity".
    """
    t = raw.strip()
    # Strip stray ```markdown / ``` fences the LLM sometimes adds.
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)

    # Our prompt asks the LLM to omit the top-level header, so synthesize
    # one — otherwise parse_multi_review_markdown's split eats the first
    # `# Review 1` block.
    if not t.lstrip().startswith("#"):
        t = "# Converted Reviews\n\n" + t
    elif t.lstrip().startswith("# Review"):
        t = "# Converted Reviews\n\n---\n" + t

    parsed = parse_multi_review_markdown(t)

    def _sanitize_id(raw_id: str, fallback: str) -> str:
        """Collapse any non-alphanumeric run to a single underscore, trim
        leading/trailing underscores. Guarantees the id fits ``[A-Za-z0-9_-]+``
        with no spaces, ``#``, or other characters that break regex capture
        in downstream parsers."""
        if not raw_id:
            return fallback
        clean = re.sub(r"[^A-Za-z0-9]+", "_", str(raw_id)).strip("_")
        return clean or fallback

    for ri, rv in enumerate(parsed.get("actual_reviews", [])):
        original_label = rv.get("reviewer_id") or f"Reviewer_{ri+1}"
        safe_id = _sanitize_id(original_label, f"Reviewer_{ri+1}")
        rv["reviewer_id"] = safe_id
        rv["reviewer_label"] = safe_id

        for ci, c in enumerate(rv.get("comments", []), start=1):
            c["comment_id"] = f"{safe_id}-C{ci}"
            if "text" not in c and "description" in c:
                c["text"] = c["description"]

    return {"actual_reviews": parsed.get("actual_reviews", [])}


def normalize_extracted(
    data: Dict[str, Any],
    category_vocab: Optional["list[str]"] = None,
) -> Dict[str, Any]:
    """Snap the LLM's extracted values onto the canonical vocabularies and
    ensure every comment has the fields ``review_dict_to_markdown`` needs
    to emit a complete block.

    Unknown severity values default to ``moderate``; unknown categories
    default to an empty string (the downstream renderer simply omits the
    ``Category:`` line). Missing ``section_reference``, ``summary``, and
    ``keywords`` are derived heuristically from the comment text so the
    output markdown is always complete.

    ``category_vocab`` comes from the reviewer DB's Validation
    Attribution Tables block. When ``None``, no vocab is enforced (any
    category string is kept verbatim) — useful for tests and for runs
    against a DB that didn't declare a vocab.
    """
    if "actual_reviews" not in data or not isinstance(data["actual_reviews"], list):
        raise ValueError("Extracted data missing 'actual_reviews' list")

    for ri, rv in enumerate(data["actual_reviews"]):
        rec = rv.get("recommendation")
        if rec:
            r = str(rec).lower().strip().replace(" ", "_")
            rv["recommendation"] = r if r in RECOMMENDATION_VOCAB else None

        c = rv.get("confidence")
        try:
            rv["confidence"] = int(c) if c is not None and str(c).strip() != "" else None
        except (ValueError, TypeError):
            rv["confidence"] = None

        rv.setdefault("paper_summary", None)
        rv.setdefault("strengths", [])
        rv["strengths"] = [str(s).strip() for s in (rv.get("strengths") or []) if str(s).strip()]

        label_safe = re.sub(r"[^A-Za-z0-9]+", "_",
                            rv.get("reviewer_label", f"Reviewer_{ri+1}")).strip("_") or "Reviewer"
        for ci, cm in enumerate(rv.get("comments", [])):
            sev = (cm.get("severity") or "moderate").lower().strip()
            cm["severity"] = sev if sev in SEVERITY_VOCAB else "moderate"

            cat = (cm.get("category") or "").lower().strip()
            if category_vocab is None:
                cm["category"] = cat
            else:
                cm["category"] = cat if cat in category_vocab else ""

            cm["text"] = (cm.get("text") or "").strip()
            cm.setdefault("comment_id", f"{label_safe}-C{ci+1}")

            # Fill missing fields heuristically so the serializer always
            # emits complete Comment blocks.
            if not cm.get("section_reference"):
                cm["section_reference"] = _infer_section_reference(cm["text"])
            if not cm.get("summary"):
                cm["summary"] = _derive_summary(cm["text"])
            kws = cm.get("keywords")
            if not isinstance(kws, list):
                kws = []
            cm["keywords"] = [str(k).strip() for k in kws if str(k).strip()]

    return data


_SECTION_PATTERNS = [
    (re.compile(r"\bSection\s+(\d+(?:\.\d+)*)\b", re.IGNORECASE), "Section {}"),
    (re.compile(r"\bSec\.?\s+(\d+(?:\.\d+)*)\b", re.IGNORECASE), "Section {}"),
    (re.compile(r"\bTable\s+(\d+[A-Za-z]?)\b", re.IGNORECASE), "Table {}"),
    (re.compile(r"\bFigure\s+(\d+[A-Za-z]?)\b", re.IGNORECASE), "Figure {}"),
    (re.compile(r"\bFig\.?\s+(\d+[A-Za-z]?)\b", re.IGNORECASE), "Figure {}"),
    (re.compile(r"\b(?:Eq(?:uation)?|Eqn)\.?\s+\(?(\d+)\)?\b", re.IGNORECASE), "Eq. {}"),
    (re.compile(r"\bAlgorithm\s+(\d+)\b", re.IGNORECASE), "Algorithm {}"),
    (re.compile(r"\b(Abstract|Introduction|Background|Related Work|Conclusion|Conclusions|References)\b", re.IGNORECASE), "{}"),
]


def _infer_section_reference(text: str) -> str:
    """Pull out the first section/table/figure/equation reference we can
    find in the comment text, else return 'general'. Safety net when the
    LLM omits the ``section_reference`` field."""
    if not text:
        return "general"
    for pat, template in _SECTION_PATTERNS:
        m = pat.search(text)
        if m:
            return (template.format(m.group(1).strip().title())
                    if template == "{}"
                    else template.format(m.group(1).strip()))
    return "general"


def _derive_summary(text: str, max_words: int = 15) -> str:
    """Take the first sentence of a comment's body, capped at ~15 words,
    with trailing punctuation stripped. Safety net when the LLM omits the
    ``summary`` field."""
    if not text:
        return ""
    clean = text.strip().replace("\n", " ")
    first = re.split(r"(?<=[.!?])\s+", clean, maxsplit=1)[0]
    words = first.split()
    if len(words) > max_words:
        words = words[:max_words]
    return " ".join(words).rstrip(".!?,;:")
