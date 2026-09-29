"""Format the human-readable validation markdown report.

Composes seven sections from the alignment + metrics + calibration dicts:

1. Header + paper metadata.
2. (Optional) LLM semantic comparison from ``llm_comparison``.
3. Summary metrics table.
4. Hits — actual comments the AI caught.
5. Misses — actual comments the AI failed to raise.
6. False alarms — AI comments not raised by any human.
7. Per-persona stats / sub-rating attributions / failure-mode breakdown
   / calibration suggestions.

The structured calibration delta JSON (consumed by the cross-paper
aggregation module) is built by the caller in
:mod:`ai_paper_review.validation.validation`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ai_paper_review.review.reviewer_db import AttributionTables

from .routing import route_category


def format_report(
    actual: Dict[str, Any],
    ai_report: Dict[str, Any],
    alignment: Dict[str, Any],
    metrics: Dict[str, Any],
    calibration: Dict[str, Any],
    llm_comparison: Optional[Dict[str, Any]] = None,
    tables: Optional[AttributionTables] = None,
) -> str:
    """Compose the human-readable validation markdown report.

    ``tables`` carries the reviewer DB's validation attribution maps so
    the Misses section can annotate each human miss with the persona
    that *should* have caught it. When ``None``, the annotation is
    skipped — the rest of the report still renders fine.

    LLM provider / model / base URL / launch / end timestamps live in
    the prepended ``<!-- provenance -->`` block the caller wraps around
    this output (see :func:`ai_paper_review.provenance.format_provenance`)
    rather than being duplicated inside the report body.
    """
    category_to_persona = tables.category_to_persona if tables else {}
    out: List[str] = []
    out.append("# Review Validation Report\n\n")
    out.append(f"**Title:** {actual.get('title') or actual.get('paper_id') or 'unknown'}\n\n")
    out.append(f"**Venue:** {actual.get('venue') or 'n/a'}\n\n")

    # --- LLM semantic comparison ---
    # Count summary of the LLM similarity matrix and the model that scored it.
    if llm_comparison and (llm_comparison.get("summary") or llm_comparison.get("raw")):
        out.append("## Semantic Comparison (LLM)\n\n")
        out.append(
            "_Count summary of the LLM similarity matrix: the number of hits, "
            "misses, and false alarms, and the number of (human, AI) comment "
            "pairs parsed from the LLM response. The LLM scores each pair by "
            "**meaning** rather than wording, so paraphrases of the same issue "
            "count as matches. The metrics below are computed from the same "
            f"matrix._ (model: `{llm_comparison.get('llm_model', '?')}`)\n\n"
        )
        if llm_comparison.get("summary"):
            out.append(f"**Summary:** {llm_comparison['summary']}\n\n")

    n_selected_ai = len(ai_report.get("selected") or [])
    out.append("## Summary Metrics\n\n")
    out.append("| Metric | Value |\n|---|---|\n")
    out.append(f"| Actual human comments | {metrics['n_actual']} |\n")
    out.append(
        f"| AI comments (all {n_selected_ai} reviewer"
        f"{'s' if n_selected_ai != 1 else ''}) | {metrics['n_ai']} |\n"
    )
    out.append(f"| Hits (actual ↔ AI match) | {metrics['n_hits']} |\n")
    out.append(f"| Misses (actual with no AI match) | {metrics['n_misses']} |\n")
    out.append(f"| False alarms (AI with no actual match) | {metrics['n_false_alarms']} |\n")
    out.append(f"| Recall | {metrics['recall']} |\n")
    out.append(f"| Precision | {metrics['precision']} |\n")
    out.append(f"| F1 | {metrics['f1']} |\n")
    out.append(f"| Severity-weighted recall | {metrics['severity_weighted_recall']} |\n\n")

    # --- Hits ---
    out.append("## Hits — Actual Comments the AI Caught\n\n")
    if not alignment["hits"]:
        out.append("_None._\n\n")
    for h in alignment["hits"]:
        a = h["actual"]
        p = h["primary_ai"]
        out.append(
            f"### ✅ {a['id']} ({a['severity']}, {a['category']}) — sim={h['primary_sim']:.2f}\n\n"
        )
        out.append(f"**Human:** {a['text']}\n\n")
        out.append(
            f"**Primary AI match** ({p['reviewer_id']} / {p['persona']}): "
            f"{p['summary']}\n\n"
        )
        # supporting_ai is sorted by sim, so the first entry per reviewer is its best.
        others: Dict[Any, float] = {}
        for s in h["supporting_ai"]:
            rid = s["ai"].get("reviewer_id")
            if rid != p.get("reviewer_id"):
                others.setdefault(rid, s["sim"])
        if others:
            out.append(f"Also raised by {len(others)} other AI reviewer(s): ")
            out.append(
                ", ".join(f"{rid} ({sim:.2f})" for rid, sim in list(others.items())[:5])
            )
            out.append("\n\n")

    # --- Misses ---
    out.append("## Misses — Actual Comments the AI Failed to Raise\n\n")
    if not alignment["misses"]:
        out.append("_None._\n\n")
    for m in alignment["misses"]:
        out.append(
            f"### ❌ {m['id']} ({m['severity']}, {m['category']}) "
            f"— best AI similarity {m.get('best_sim', 0):.2f}\n\n"
        )
        out.append(f"**Human:** {m['text']}\n\n")
        expected = route_category(m["category"], category_to_persona)
        if expected:
            out.append(f"_Expected AI persona:_ **{expected}**\n\n")

    # --- False alarms ---
    out.append("## False Alarms — AI Comments Not Raised by Any Human\n\n")
    out.append(
        "_(Note: false alarms aren't necessarily wrong, humans may have missed them.)_\n\n"
    )
    if not alignment["false_alarms"]:
        out.append("_None._\n\n")
    for fa in alignment["false_alarms"][:20]:
        out.append(
            f"- **{fa['reviewer_id']} / {fa['persona']}** ({fa['severity']}): "
            f"{fa['summary']}\n"
        )
    if len(alignment["false_alarms"]) > 20:
        out.append(f"- _...and {len(alignment['false_alarms']) - 20} more_\n")
    out.append("\n")

    # --- Per-persona stats ---
    out.append("## Per-Persona Performance (Selected Reviewers)\n\n")
    out.append(
        "| Reviewer ID | Persona | Comments emitted | Actual issues helped catch | False alarms | Noise ratio |\n"
        "|---|---|---|---|---|---|\n"
    )
    for ps in calibration["persona_stats"]:
        nr = ps["noise_ratio"]
        out.append(
            f"| {ps.get('reviewer_id') or '—'} | {ps['persona']} | "
            f"{ps['comments_emitted']} | "
            f"{ps['actual_comments_helped_catch']} | {ps['false_alarms']} | "
            f"{nr if nr is not None else '—'} |\n"
        )
    out.append("\n")

    # --- Sub-rating attributions ---
    sub_attrs = calibration.get("sub_rating_attributions", [])
    if sub_attrs:
        out.append("## Sub-Rating Signal Attribution\n\n")
        out.append(
            "_OpenReview-style sub-ratings (Soundness / Presentation / Contribution) at "
            "a low value are an orthogonal signal from the narrative comments. Each low "
            "sub-rating is attributed to the AI persona that should have caught the "
            "corresponding dimension of weakness._\n\n"
        )
        out.append("| Reviewer | Sub-rating | Value | Expected persona | Persona selected? | Caught anything? | Verdict |\n")
        out.append("|---|---|---|---|---|---|---|\n")
        for a in sub_attrs:
            verdict = a["failure_mode"].replace("_", " ")
            out.append(
                f"| {a['reviewer_label']} | {a['sub_rating']} | "
                f"{a['value']}/{a['scale']} | {a.get('expected_persona') or '—'} | "
                f"{'✓' if a.get('expected_persona_selected') else '✗' if a.get('expected_persona') else '—'} | "
                f"{'✓' if a.get('persona_caught_related_weakness') else '✗' if a.get('expected_persona') else '—'} | "
                f"**{verdict}** |\n"
            )
        out.append("\n")

    # --- Failure mode breakdown ---
    s = calibration["summary"]
    out.append("## Failure Mode Breakdown\n\n")
    out.append(f"- Selection failures (right persona not selected): **{s['selection_failures']}**\n")
    out.append(f"- Prompt failures (persona selected but missed issue): **{s['prompt_failures']}**\n")
    out.append(f"- Sub-rating signals (low human sub-rating not caught by expected persona): **{s.get('sub_rating_signals', 0)}**\n")
    if s["uncovered_categories"]:
        out.append("- Uncovered categories:\n")
        for cat, n in sorted(s["uncovered_categories"].items(), key=lambda x: -x[1]):
            out.append(f"  - `{cat}`: {n} miss(es)\n")
    out.append("\n")

    # --- Calibration suggestions ---
    out.append("## Calibration Suggestions for the Reviewer Database\n\n")
    if not calibration["suggestions"]:
        out.append("_No database changes recommended. The review profile matches ground truth._\n\n")
    for i, sug in enumerate(calibration["suggestions"], 1):
        out.append(f"### #{i} [{sug['type']}]\n\n")
        for k, v in sug.items():
            if k == "type":
                continue
            if isinstance(v, list):
                out.append(f"- **{k}:**\n")
                for item in v:
                    out.append(f"  - {item}\n")
            else:
                out.append(f"- **{k}:** {v}\n")
        out.append("\n")

    return "".join(out)
