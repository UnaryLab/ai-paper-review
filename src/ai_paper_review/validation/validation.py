"""``ai-paper-review-validate`` — align an AI review against a real human
review and emit metrics + markdown report + per-paper calibration delta.

This file is the CLI veneer. The actual logic lives in sibling
submodules (``alignment``, ``calibration``, ``loading``, ``metrics``,
``reporting``).

The ``conversion`` sibling (reshapes raw human-review text into the
AI-review markdown schema this CLI consumes as ``--actual``) has no CLI
of its own — the web UI's validation flow runs it automatically when
the upload isn't already in that schema.

Rolling up calibration deltas across many papers into recommendations
is a separate concern — see :mod:`ai_paper_review.aggregation`.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from ai_paper_review.review.reviewer_db import parse_reviewer_database

from .alignment import align_comments
from .calibration import build_calibration
from .loading import load_actual, load_ai
from .metrics import compute_metrics
from .reporting import format_report

logger = logging.getLogger("validator")


def main() -> None:
    """Align + metrics + per-paper calibration + markdown report."""
    script_dir = Path(__file__).resolve().parent

    ap = argparse.ArgumentParser(
        prog="ai-paper-review-validate",
        description=(
            "Validate an AI review against real human reviews and emit "
            "DB calibration suggestions. The human review must already be "
            "in AI-review-format markdown — convert raw human-review text "
            "via the web UI's validation flow (or call "
            "`ai_paper_review.validation.conversion.llm_extract` from "
            "Python) before running this CLI."
        ),
    )
    ap.add_argument("--actual", required=True,
                    help="Actual (human) reviews file in AI-review-format markdown.")
    ap.add_argument("--ai-review", required=True,
                    help="AI review markdown produced by `ai-paper-review-review`.")
    ap.add_argument("--db", default=None,
                    help="Reviewer database markdown (default: bundled).")
    ap.add_argument("--out", default=None,
                    help="Validation report markdown (default: <actual>_validation.md).")
    ap.add_argument("--calibration-out", default=None,
                    help="Per-paper calibration delta JSON (default: <actual>_calibration.json).")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Enable DEBUG logging.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from ai_paper_review.provenance import format_provenance, now_iso
    launched_at = now_iso()

    actual_path = Path(args.actual).resolve()
    out = args.out or str(actual_path.with_name(f"{actual_path.stem}_validation.md"))
    calibration_out = (
        args.calibration_out
        or str(actual_path.with_name(f"{actual_path.stem}_calibration.json"))
    )
    db = args.db or str(script_dir / "comparch_reviewer_db.md")

    actual = load_actual(args.actual)
    ai_report = load_ai(args.ai_review)
    _db_obj = parse_reviewer_database(db)
    reviewers = _db_obj.reviewers
    tables = _db_obj.tables

    logger.info(
        "Loaded %d actual comments, %d AI comments, %d reviewers in DB",
        len(actual["flat_comments"]),
        len(ai_report["flat_comments"]),
        len(reviewers),
    )

    # No embedding fallback — without a working LLM there's nothing
    # meaningful to produce, so a failure here propagates.
    from ai_paper_review.llm.config import load_config
    from ai_paper_review.llm.factory import make_client
    cfg = load_config()
    llm_client = make_client(cfg, use_case="validation")
    val_provider = cfg.resolve_provider("validation")
    val_model = cfg.resolve_model("validation")
    val_base_url = cfg.resolve_base_url_for_stage("validation")
    logger.info("Using LLM for alignment: provider=%s model=%s",
                val_provider, val_model)

    out_dir = Path(out).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    alignment = align_comments(
        actual["flat_comments"],
        ai_report["flat_comments"],
        llm_client,
        run_dir=out_dir,
    )

    metrics = compute_metrics(alignment)
    calibration = build_calibration(
        alignment, ai_report, reviewers, tables,
        actual_reviews=actual.get("actual_reviews", []),
    )
    llm_comparison = alignment.get("llm_comparison")

    ended_at = now_iso()
    report_md = format_report(
        actual, ai_report, alignment, metrics, calibration,
        llm_comparison=llm_comparison,
        tables=tables,
    )
    provenance = format_provenance(
        provider=val_provider,
        model=val_model,
        base_url=val_base_url,
        launched_at=launched_at,
        ended_at=ended_at,
    )
    Path(out).write_text(provenance + report_md)

    delta = {
        "paper_id": actual.get("paper_id"),
        "metrics": metrics,
        "summary": calibration["summary"],
        "persona_stats": calibration["persona_stats"],
        "miss_attributions": calibration["miss_attributions"],
        "sub_rating_attributions": calibration.get("sub_rating_attributions", []),
        "suggestions": calibration["suggestions"],
    }
    if llm_comparison:
        delta["llm_comparison"] = {
            "summary": llm_comparison.get("summary", ""),
            "matches": llm_comparison.get("matches", []),
            "missed": llm_comparison.get("missed", []),
            "extras": llm_comparison.get("extras", []),
            "llm_model": llm_comparison.get("llm_model", ""),
        }
    Path(calibration_out).write_text(json.dumps(delta, indent=2, default=str))

    print(f"Wrote validation report: {out}")
    print(f"Wrote calibration delta: {calibration_out}")
    print(
        f"Recall={metrics['recall']}  Precision={metrics['precision']}  "
        f"F1={metrics['f1']}  Sev-weighted recall={metrics['severity_weighted_recall']}"
    )
    n_sug = len(calibration["suggestions"])
    if n_sug:
        print(f"→ {n_sug} calibration suggestion(s) produced for the reviewer DB.")
    if llm_comparison and llm_comparison.get("summary"):
        print(f"LLM comparison ({llm_comparison.get('llm_model', '?')}): "
              f"{llm_comparison['summary'][:200]}")


if __name__ == "__main__":
    main()
