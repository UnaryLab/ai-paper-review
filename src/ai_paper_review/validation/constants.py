"""Shared constants for the validation pipeline.

* ``RECOMMENDATION_VOCAB`` / ``SEVERITY_VOCAB`` — the canonical strings
  the LLM-extraction prompt asks the model to emit. Every downstream
  consumer expects these literals.
* ``BATCH_SAME_THR`` / ``BATCH_PARTIAL_THR`` — verdict thresholds on
  the LLM's per-pair similarity score. Tuned so a paraphrase clears
  ``same``, a different-angle-on-same-issue pair clears ``partial``,
  and unrelated pairs fall through to ``missed``.

Three other tables that used to live here — ``CATEGORY_VOCAB``,
``CATEGORY_TO_PERSONA``, ``SUB_RATING_TO_PERSONA`` — moved into the
reviewer DB markdown itself (section 7, "Validation Attribution
Tables"). They're parsed into
:class:`ai_paper_review.review.reviewer_db.AttributionTables` and
passed through as data, so a user-uploaded DB with different personas
brings its own attribution maps instead of silently mismatching
against hard-coded persona names.
"""
from __future__ import annotations


RECOMMENDATION_VOCAB = [
    "strong_accept", "accept", "weak_accept", "borderline",
    "weak_reject", "reject", "strong_reject",
]

SEVERITY_VOCAB = ["major", "moderate", "minor"]


BATCH_SAME_THR = 0.65
BATCH_PARTIAL_THR = 0.35
