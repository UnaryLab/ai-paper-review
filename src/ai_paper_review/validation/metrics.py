"""Compute precision / recall / F1 (plus severity-weighted recall) from
the alignment dict produced by :mod:`ai_paper_review.validation.alignment`.

Treats every AI comment that corroborates at least one human comment as
a true positive; anything in ``false_alarms`` is a false positive. Recall
is over the human comments. Severity-weighted recall up-weights ``major``
misses 3× a ``minor`` miss.
"""
from __future__ import annotations

from typing import Any, Dict

from ai_paper_review.review.constants import SEVERITY_WEIGHT


def compute_metrics(alignment: Dict[str, Any]) -> Dict[str, Any]:
    n_actual = alignment["n_actual"]
    n_ai = alignment["n_ai"]
    n_hits = len(alignment["hits"])
    n_false = len(alignment["false_alarms"])

    recall = n_hits / n_actual if n_actual else 0.0
    # Treat every AI comment that corroborates at least one actual comment as TP
    ai_tp = n_ai - n_false
    precision = ai_tp / n_ai if n_ai else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # Severity-weighted recall (major misses hurt more)
    wt_hit = sum(SEVERITY_WEIGHT.get(h["actual"]["severity"], 1.0) for h in alignment["hits"])
    wt_all = sum(SEVERITY_WEIGHT.get(m["severity"], 1.0) for m in alignment["misses"]) + wt_hit
    weighted_recall = wt_hit / wt_all if wt_all else 0.0

    return {
        "n_actual": n_actual,
        "n_ai": n_ai,
        "n_hits": n_hits,
        "n_misses": n_actual - n_hits,
        "n_false_alarms": n_false,
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "f1": round(f1, 3),
        "severity_weighted_recall": round(weighted_recall, 3),
    }
