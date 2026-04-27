"""Cross-paper aggregation of per-paper calibration deltas.

The validation pipeline writes ONE ``calibration_delta.json`` per paper.
This module loads N such deltas, groups their suggestions by
``(type, target)``, and produces a recommendations report that surfaces
only suggestions repeating across papers — a single paper's suggestion
is easily driven by one human reviewer's idiosyncrasies, so robust
database tuning wants cross-paper support.

Strictly a reporter: nothing here mutates any database or config file.
Acting on the recommendations is up to the user.

Recommendation → reviewer-config edit:

  * ``strengthen_persona_prompt``  → add a new bullet to the persona's
                                     ``priorities`` list.
  * ``reduce_persona_noise``       → tighten the persona's
                                     ``common_concerns`` field.
  * ``selection_policy_adjustment``→ raise ``domain_bleed`` in
                                     :mod:`ai_paper_review.review.selection`,
                                     or add a persona-floor rule.
  * ``topical_gap``                → add a new persona to the config
                                     YAML (see ``docs/database_format.md``).

Usable three ways:

* **Web UI** — the ``/aggregate`` page globs the workdir's completed
  validation runs and renders the recommendations in-browser.
* **CLI** — ``ai-paper-review-aggregate`` runs the same pipeline from
  the shell against a path glob, writing a markdown report to disk or
  stdout.
* **Python library** — import from here for scripted / notebook use.
"""
from __future__ import annotations

import glob
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("calibration_aggregation")


@dataclass
class SuggestionAgg:
    """A suggestion aggregated across multiple calibration deltas."""
    type: str
    target: str                           # persona name or category
    support: int                          # number of papers that produced this
    paper_ids: List[str] = field(default_factory=list)
    example_misses: List[str] = field(default_factory=list)
    rationales: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


def load_deltas(paths: List[str]) -> List[Dict[str, Any]]:
    """Load and sanity-check calibration delta JSON files."""
    out = []
    for pat in paths:
        for p in sorted(glob.glob(pat)) if any(ch in pat for ch in "*?[") else [pat]:
            try:
                d = json.loads(Path(p).read_text())
                d["_path"] = p
                out.append(d)
            except Exception as e:  # pragma: no cover
                logger.warning("Skipping %s: %s", p, e)
    logger.info("Loaded %d calibration delta file(s)", len(out))
    return out


def aggregate(deltas: List[Dict[str, Any]]) -> List[SuggestionAgg]:
    """Group suggestions by (type, target) across papers and count support."""
    bucket: Dict[Tuple[str, str], SuggestionAgg] = {}
    for d in deltas:
        paper_id = d.get("paper_id") or Path(d.get("_path", "?")).stem
        for s in d.get("suggestions", []):
            stype = s.get("type")
            # Determine the target — different types key on different fields.
            if stype in ("strengthen_persona_prompt", "reduce_persona_noise"):
                target = s.get("target_persona", "?")
            elif stype == "topical_gap":
                target = s.get("category", "?")
            elif stype == "selection_policy_adjustment":
                # One bucket per distinct missing-persona set
                target = ",".join(sorted(s.get("missing_personas_in_selection", [])))
            else:
                target = s.get("target_persona") or s.get("category") or "?"

            key = (stype, target)
            if key not in bucket:
                bucket[key] = SuggestionAgg(type=stype, target=target, support=0)
            agg = bucket[key]
            agg.support += 1
            agg.paper_ids.append(paper_id)
            agg.rationales.append(s.get("rationale", ""))
            agg.example_misses.extend(s.get("example_misses", []))
            for k, v in s.items():
                if k not in {"type", "target_persona", "category", "rationale",
                             "example_misses", "missing_personas_in_selection"}:
                    agg.extra.setdefault(k, []).append(v)

    # Stable order: by support desc, then by target
    return sorted(bucket.values(), key=lambda a: (-a.support, a.type, a.target))


def render_changelog(
    changelog: List[Dict[str, Any]],
    suggestions: List[SuggestionAgg],
    min_support: int,
    launched_at: Optional[str] = None,
) -> str:
    """Render the recommendations report as markdown.

    The provenance block at the top records ``launched_at`` (defaults to
    "now" if the caller didn't capture it earlier) and ``ended_at=now``.
    The LLM fields render "not applicable (reporter stage)" — aggregation
    summarises prior validation runs, it doesn't call an LLM itself.
    """
    from ai_paper_review.provenance import format_provenance, now_iso

    lines: List[str] = []
    lines.append(format_provenance(
        provider=None, model=None, base_url=None,
        launched_at=launched_at or now_iso(),
    ))
    lines.append("# Calibration Recommendations Report\n\n")
    lines.append(f"**Minimum support:** {min_support} paper(s)\n\n")

    applied = [c for c in changelog if c.get("status") == "recommended"]
    manual = [c for c in changelog if c.get("status") == "manual_review_required"]
    skipped = [c for c in changelog if c.get("status", "").startswith(("noop", "skipped", "unknown"))]

    lines.append("## Summary\n\n")
    lines.append(f"- Recommended actions: **{len(applied)}**\n")
    lines.append(f"- Needs manual review: **{len(manual)}**\n")
    lines.append(f"- Skipped / no-op: **{len(skipped)}**\n\n")

    if applied:
        lines.append("## Recommendations\n\n")
        for c in applied:
            lines.append(f"### [{c['type']}] → {c['target']}\n\n")
            lines.append(f"- **Support:** {c['support']} paper(s): {', '.join(c['paper_ids'])}\n")
            if "recommendation" in c:
                lines.append(f"- **Recommendation:** {c['recommendation']}\n")
            if c.get("example_misses"):
                lines.append("- **Example misses:**\n")
                for m in c["example_misses"]:
                    lines.append(f"  - {m[:180]}\n")
            lines.append("\n")

    if manual:
        lines.append("## Manual Review Required\n\n")
        for c in manual:
            lines.append(f"### [{c['type']}] → {c['target']}\n\n")
            lines.append(f"- **Support:** {c['support']} paper(s): {', '.join(c['paper_ids'])}\n")
            lines.append(f"- **Recommendation:** {c['recommendation']}\n\n")

    if skipped:
        lines.append("## Skipped (below threshold or already present)\n\n")
        under_threshold = [
            s for s in suggestions if s.support < min_support
        ]
        for s in under_threshold[:20]:
            lines.append(f"- [{s.type}] → {s.target} (support={s.support})\n")
        lines.append("\n")

    return "".join(lines)


def recommendation_text(agg: SuggestionAgg) -> str:
    """Produce a one-line recommendation for a single suggestion bucket.

    Stays conservative — points the user at the config file and names
    the persona/category; doesn't synthesize new prompt text.
    """
    if agg.type == "strengthen_persona_prompt":
        return (
            f"Consider appending a new bullet to persona '{agg.target}''s "
            f"`priorities` list in your reviewer-config YAML that covers the "
            f"repeated miss pattern. Example misses: "
            f"{'; '.join(agg.example_misses[:2])[:200]}"
        )
    if agg.type == "reduce_persona_noise":
        return (
            f"Consider tightening persona '{agg.target}''s `common_concerns` "
            "in your reviewer-config YAML — the persona is flagging issues "
            "not anchored to specific paper content."
        )
    if agg.type == "selection_policy_adjustment":
        return (
            f"Consider raising `domain_bleed` above 0.15 in "
            "ai_paper_review.review.selection, or adding a persona-floor rule so "
            f"'{agg.target}' is selected when its keywords appear in the paper."
        )
    if agg.type == "topical_gap":
        return (
            f"Category '{agg.target}' has no persona catching it. Consider "
            "adding a new persona to the `personas:` list in your config "
            "YAML, then rebuild the reviewer database (see "
            "docs/database_format.md)."
        )
    return f"Unknown suggestion type: {agg.type}"


def main() -> None:
    """``ai-paper-review-aggregate`` — aggregate calibration deltas into
    cross-paper recommendations.

    Reads N ``calibration_delta.json`` files (paths or glob patterns),
    groups suggestions by ``(type, target)``, filters by ``min_support``,
    and writes a markdown recommendations report to a file or stdout.
    Reporter only — does not mutate any config or database.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="ai-paper-review-aggregate",
        description=(
            "Cross-paper aggregation of per-paper calibration deltas from "
            "completed validation runs into reviewer-database tuning "
            "recommendations. Reporter only — never mutates config or "
            "database files."
        ),
    )
    ap.add_argument(
        "deltas",
        nargs="+",
        help=(
            "Paths or glob patterns to calibration_delta.json files. "
            "Example: "
            "'ai-paper-review-data/runs/validation_*/calibration_delta.json'"
        ),
    )
    ap.add_argument(
        "--min-support",
        type=int,
        default=2,
        help=(
            "Minimum number of papers a suggestion must recur in before "
            "it counts as a recommendation (default: 2; lower to 1 for "
            "early-corpus exploration)."
        ),
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Write the markdown report to this path (default: stdout).",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from ai_paper_review.provenance import now_iso
    launched_at = now_iso()

    deltas = load_deltas(args.deltas)
    if not deltas:
        raise SystemExit(
            f"Loaded 0 calibration_delta.json files from: {args.deltas}. "
            "Run `ai-paper-review-validate` on some papers first."
        )

    suggestions = aggregate(deltas)
    recs = [
        {
            "status": "recommended",
            "type": s.type,
            "target": s.target,
            "support": s.support,
            "paper_ids": s.paper_ids,
            "example_misses": s.example_misses[:3],
            "recommendation": recommendation_text(s),
        }
        for s in suggestions
        if s.support >= args.min_support
    ]
    report = render_changelog(recs, suggestions, args.min_support,
                              launched_at=launched_at)

    if args.out:
        Path(args.out).write_text(report)
        print(f"Wrote {args.out}  "
              f"({len(recs)} recommendation(s) from {len(deltas)} paper(s), "
              f"min_support={args.min_support})")
    else:
        print(report)


if __name__ == "__main__":
    main()
