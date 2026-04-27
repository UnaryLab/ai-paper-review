"""Read and flatten human + AI review markdown files for validation.

Two entry points:

* :func:`load_actual` — parses a multi-reviewer human review markdown,
  flattens the ``comments`` and ``strengths`` arrays across all
  reviewers, and fills missing optional fields with empty defaults so
  downstream code can treat every record uniformly.
* :func:`load_ai` — parses an AI review (one ``# Review N`` block per
  selected reviewer), flattens comments, and projects them into the
  shape the alignment + calibration steps expect (``id``,
  ``reviewer_id``, ``persona``, ``domain``, ``severity``, ``category``,
  ``summary``, ``description``, ...).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ai_paper_review.review.parsing import (
    load_reviews_file,
    parse_multi_review_markdown,
)


def load_actual(path: str | Path) -> Dict[str, Any]:
    """Load an actual reviews file (markdown) and flatten it for downstream use.

    Any optional field (strengths, sub_ratings, recommendation_raw, etc.) is
    filled with an empty default if absent, so every downstream code path can
    treat the structure uniformly.
    """
    data = load_reviews_file(path)
    if "actual_reviews" not in data:
        raise ValueError("actual reviews file must contain reviews (found none)")

    flat_comments: List[Dict[str, Any]] = []
    flat_strengths: List[Dict[str, Any]] = []
    for ri, rv in enumerate(data["actual_reviews"]):
        label = rv.get("reviewer_label") or rv.get("reviewer_id") or f"R{ri+1}"

        # Fill any missing optional fields with empty defaults
        rv.setdefault("recommendation_raw", None)
        rv.setdefault("recommendation_scale", None)
        rv.setdefault("confidence_raw", None)
        rv.setdefault("sub_ratings", {})
        rv.setdefault("sub_rating_scale", None)
        rv.setdefault("paper_summary", None)
        rv.setdefault("strengths", [])

        for ci, c in enumerate(rv.get("comments", [])):
            cid = c.get("comment_id") or f"ACTUAL-{ri+1}-{ci+1}"
            flat_comments.append(
                {
                    "id": cid,
                    "reviewer_label": label,
                    "recommendation": rv.get("recommendation"),
                    "severity": (c.get("severity") or "moderate").lower(),
                    "category": (c.get("category") or "").lower(),
                    "text": c.get("text", ""),
                    "source_section": c.get("source_section", "unknown"),
                }
            )

        for si, s in enumerate(rv.get("strengths") or []):
            flat_strengths.append(
                {
                    "id": f"STRENGTH-{ri+1}-{si+1}",
                    "reviewer_label": label,
                    "text": s if isinstance(s, str) else s.get("text", ""),
                }
            )

    data["flat_comments"] = flat_comments
    data["flat_strengths"] = flat_strengths
    return data


def load_ai(path: str | Path) -> Dict[str, Any]:
    """Load an AI-review markdown and project comments into the alignment shape."""
    text = Path(path).read_text()
    data = parse_multi_review_markdown(text)
    # Map parsed reviews to the raw_reviews format with _reviewer_id keys
    raw_reviews = []
    for rv in data.get("actual_reviews", []):
        raw_reviews.append({
            "_reviewer_id": rv.get("reviewer_id", "?"),
            "_persona": rv.get("persona", ""),
            "_domain": rv.get("domain", ""),
            "comments": rv.get("comments", []),
        })
    data["raw_reviews"] = raw_reviews

    # Build 'selected' list for build_calibration compatibility
    data["selected"] = [
        {"id": rv["_reviewer_id"], "persona": rv["_persona"], "domain": rv["_domain"]}
        for rv in raw_reviews
    ]

    # Flatten AI comments from raw_reviews for downstream alignment.
    flat: List[Dict[str, Any]] = []
    for rv in data.get("raw_reviews", []):
        for c in rv.get("comments", []):
            flat.append(
                {
                    "id": c.get("comment_id", f"AI-{rv.get('_reviewer_id','?')}-?"),
                    "reviewer_id": rv.get("_reviewer_id"),
                    "persona": rv.get("_persona"),
                    "domain": rv.get("_domain"),
                    "severity": (c.get("severity") or "minor").lower(),
                    "category": (c.get("category") or "").lower(),
                    "summary": c.get("summary", ""),
                    "description": c.get("description", ""),
                    "suggestion": c.get("suggestion", ""),
                    "keywords": c.get("keywords", []),
                }
            )
    data["flat_comments"] = flat
    return data
