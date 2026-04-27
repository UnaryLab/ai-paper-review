"""
ai_paper_review.aggregation
===========================

Cross-paper aggregation of per-paper calibration deltas produced by the
validation pipeline. A post-hoc reporter, not a pipeline stage: given N
completed ``calibration_delta.json`` files, it groups suggestions by
``(type, target)``, counts how many papers back each one, and renders
recommendations for hand-editing the reviewer-database YAML.

Reporter only — nothing here mutates any config or database file.

Import the submodule explicitly::

    from ai_paper_review.aggregation.aggregation import (
        aggregate, load_deltas, recommendation_text, render_changelog,
    )
"""
