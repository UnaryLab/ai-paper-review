"""Aggregation: distinct-paper support, sub_rating_signal text, Skipped section."""
from __future__ import annotations

import json
from pathlib import Path

from ai_paper_review.aggregation.aggregation import (
    aggregate, load_deltas, recommendation_text, render_changelog,
)


def _write_delta(run_dir: Path, suggestions, paper_id=None) -> str:
    run_dir.mkdir(parents=True)
    p = run_dir / "calibration_delta.json"
    d = {"suggestions": suggestions}
    if paper_id is not None:
        d["paper_id"] = paper_id
    p.write_text(json.dumps(d))
    return str(p)


def _sub_rating(sub_rating: str) -> dict:
    return {
        "type": "sub_rating_signal",
        "target_persona": "Novelty Hunter",
        "sub_rating": sub_rating,
        "support": 1,
        "reviewers": ["R1"],
        "failure_modes": ["prompt_failure"],
        "rationale": f"low {sub_rating}",
        "fix_hint": "...",
    }


def test_support_counts_distinct_papers(tmp_path):
    """Two suggestions from one paper, or the same paper validated twice,
    count as support 1; unnamed deltas fall back to the run directory."""
    both = [_sub_rating("contribution"), _sub_rating("significance")]
    paths = [
        _write_delta(tmp_path / "validation_a", both),
        _write_delta(tmp_path / "validation_a2", both, paper_id="paper_X"),
        _write_delta(tmp_path / "validation_a3", both, paper_id="paper_X"),
    ]
    aggs = aggregate(load_deltas([paths[0]]))
    assert len(aggs) == 1
    assert aggs[0].support == 1
    assert aggs[0].paper_ids == ["validation_a"]

    aggs = aggregate(load_deltas(paths[1:]))
    assert aggs[0].support == 1
    assert aggs[0].paper_ids == ["paper_X"]

    aggs = aggregate(load_deltas(paths))
    assert aggs[0].support == 2
    assert aggs[0].paper_ids == ["validation_a", "paper_X"]


def test_sub_rating_signal_text_and_skipped_section(tmp_path):
    """sub_rating_signal gets a real recommendation; below-threshold
    suggestions are listed under Skipped even with no changelog entries."""
    p = _write_delta(tmp_path / "validation_a", [_sub_rating("soundness")],
                     paper_id="paper_A")
    aggs = aggregate(load_deltas([p]))
    text = recommendation_text(aggs[0])
    assert "Unknown suggestion type" not in text
    assert "Novelty Hunter" in text and "soundness" in text

    md = render_changelog([], aggs, min_support=2)
    assert "## Skipped (below threshold or already present)" in md
    assert "- [sub_rating_signal] → Novelty Hunter (support=1)" in md
    assert "- Skipped / no-op: **1**" in md


def test_cli_deltas_in_one_dir_are_distinct_papers(tmp_path):
    """CLI deltas (<stem>_calibration.json) sharing a directory fall back to
    the file stem, not the directory name."""
    paths = []
    for stem in ("paperA", "paperB"):
        p = tmp_path / f"{stem}_calibration.json"
        p.write_text(json.dumps({"paper_id": "", "suggestions": [_sub_rating("contribution")]}))
        paths.append(str(p))
    aggs = aggregate(load_deltas(paths))
    assert aggs[0].support == 2
    assert aggs[0].paper_ids == ["paperA_calibration", "paperB_calibration"]
