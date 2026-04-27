"""Web surface for cross-paper aggregation — ``GET /aggregation``.

Reads completed validation runs from the workdir, groups their
calibration-delta suggestions across papers, and renders the
recommendations template. A separate module from ``web/validation.py``
because aggregation runs on the pipeline's *outputs*, not inside the
pipeline itself.
"""
from __future__ import annotations

from flask import render_template, request

from ai_paper_review.aggregation.aggregation import (
    aggregate, load_deltas, recommendation_text,
)

from .app import RUNS_DIR, app


@app.get("/aggregation")
def aggregate_view():
    """Cross-paper aggregation: surfaces suggestions repeating across
    ≥ ``min_support`` validation runs. Reporter-only; the user hand-edits
    the reviewer-database YAML based on what shows up here."""
    pattern = str(RUNS_DIR / "validation_*" / "calibration_delta.json")
    deltas = load_deltas([pattern])
    n_papers = len(deltas)

    # Upper-bound the filter at n_papers so a too-large min_support
    # can't silently hide everything when the user has few runs.
    raw_min_support = (request.args.get("min_support") or "2").strip()
    try:
        min_support = int(raw_min_support)
    except ValueError:
        min_support = 2
    min_support = max(1, min(min_support, max(1, n_papers)))

    all_suggestions = aggregate(deltas)

    def _row(s):
        return {
            "type": s.type,
            "target": s.target,
            "support": s.support,
            "paper_ids": s.paper_ids,
            "example_misses": s.example_misses[:5],
            "recommendation": recommendation_text(s),
        }

    recommendations = [_row(s) for s in all_suggestions if s.support >= min_support]
    below_threshold = [_row(s) for s in all_suggestions if s.support < min_support]
    source_paths = sorted(d.get("_path", "?") for d in deltas)

    return render_template(
        "aggregation.html",
        n_papers=n_papers,
        min_support=min_support,
        recommendations=recommendations,
        below_threshold=below_threshold,
        source_paths=source_paths,
    )
