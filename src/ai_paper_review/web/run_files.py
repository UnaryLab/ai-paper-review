"""Catalog files in a review or validation run directory for the result
page's "Source files on disk" panel. Shared by both pipelines.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional


_KNOWN_FILES: Dict[str, Dict[str, str]] = {
    "review_report.md": {
        "category": "outputs",
        "description": "Final human-readable review report (ranked clusters, ready to share).",
    },
    "review_data.md": {
        "category": "outputs",
        "description": "Per-reviewer structured comments (the canonical input to Validation).",
    },
    "writing_clarity_review.md": {
        "category": "outputs",
        "description": "Always-on writing-clarity reviewer output; not clustered, not compared to human reviews.",
    },
    "selection_similarities.md": {
        "category": "outputs",
        "description": "Full reviewer-vs-paper cosine similarity table; top-N after persona-diversifying selection are marked.",
    },
    "clustering_similarities.md": {
        "category": "outputs",
        "description": "Pairwise comment-to-comment similarity; sorted near-threshold pair list + full upper-triangle matrix.",
    },
    "validation_report.md": {
        "category": "outputs",
        "description": "Final validation report with hits / misses / false alarms and calibration suggestions.",
    },
    "calibration_delta.json": {
        "category": "outputs",
        "description": "Structured calibration delta for programmatic consumption (auto-patcher input).",
    },
    "actual_converted.md": {
        "category": "outputs",
        "description": "Human review after LLM conversion into the AI-review markdown format.",
    },
    "actual_raw_llm.md": {
        "category": "outputs",
        "description": "Verbatim LLM output from the human-review conversion step (diagnostic).",
    },
    "alignment_llm_analysis.md": {
        "category": "outputs",
        "description": "Raw LLM response + prompt for the batch similarity analysis (diagnostic).",
    },
    "alignment_similarities.md": {
        "category": "outputs",
        "description": "N × M similarity matrix between human and AI comments, with verdicts.",
    },
    "alignment_ranking.md": {
        "category": "outputs",
        "description": "Human comments sorted by their best-match AI similarity.",
    },
    "_ui_state.json": {
        "category": "internal",
        "description": "Snapshot of rendered state for the web UI. Not meant for direct use.",
    },
}


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def list_run_files(
    run_dir: Path,
    extra_inputs: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Catalog files in a run directory for the result page's "Source
    files on disk" panel.

    ``extra_inputs`` surfaces files that aren't inside ``run_dir`` but
    are still inputs — the canonical case is a validation run pointing
    at a "prior AI review" file in another review's run_dir. Entries
    already in run_dir are deduplicated by resolved path.

    Returns ``{inputs, outputs, internal, run_dir}``; each file entry
    has ``name``, ``abs_path``, ``size``, ``description``.
    """
    outputs: List[Dict[str, Any]] = []
    internal: List[Dict[str, Any]] = []
    inputs: List[Dict[str, Any]] = []

    if not run_dir.exists() or not run_dir.is_dir():
        return {"inputs": [], "outputs": [], "internal": [],
                "run_dir": str(run_dir)}

    for f in sorted(run_dir.iterdir()):
        if not f.is_file():
            continue
        try:
            size = _fmt_size(f.stat().st_size)
        except OSError:
            size = "?"
        meta = _KNOWN_FILES.get(f.name)
        entry = {
            "name": f.name,
            "abs_path": str(f.resolve()),
            "size": size,
            "description": meta["description"] if meta else
                           "User-uploaded source file." if f.suffix.lower()
                           in {".pdf", ".md", ".txt"} else
                           "Unclassified file in this run directory.",
        }
        if meta:
            if meta["category"] == "internal":
                internal.append(entry)
            else:
                outputs.append(entry)
        else:
            # Unknown files in the run dir are typically user uploads
            # (PDF for reviews, .md for validation) or stray artifacts.
            inputs.append(entry)

    if extra_inputs:
        seen_paths = {entry["abs_path"] for entry in inputs}
        for ex in extra_inputs:
            p_str = ex.get("path")
            if not p_str:
                continue
            try:
                p = Path(p_str).resolve()
            except OSError:
                continue
            if not p.exists() or not p.is_file():
                continue
            abs_path = str(p)
            if abs_path in seen_paths:
                continue
            try:
                size = _fmt_size(p.stat().st_size)
            except OSError:
                size = "?"
            inputs.append({
                "name": p.name,
                "abs_path": abs_path,
                "size": size,
                "description": ex.get("description") or "External input referenced by this run.",
            })
            seen_paths.add(abs_path)

    return {
        "inputs": inputs,
        "outputs": outputs,
        "internal": internal,
        "run_dir": str(run_dir.resolve()),
    }
