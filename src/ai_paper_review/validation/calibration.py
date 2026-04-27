"""Per-paper calibration delta — actionable suggestions for the reviewer DB.

Walks the alignment dict + AI report + reviewer DB and produces:

* ``persona_stats``           — per-selected-persona hit / miss / noise.
* ``miss_attributions``       — for each missed comment, which persona
  *should* have caught it and whether that persona was selected.
* ``sub_rating_attributions`` — same idea for OpenReview-style low
  sub-ratings (Soundness / Presentation / Contribution).
* ``suggestions``             — concrete change recommendations:
  ``strengthen_persona_prompt``, ``selection_policy_adjustment``,
  ``reduce_persona_noise``, ``sub_rating_signal``, ``topical_gap``.

Cross-paper aggregation (turning N per-paper deltas into a recommendations
report) lives in :mod:`ai_paper_review.aggregation` — a separate package,
since it's a post-hoc reporter over the pipeline's outputs rather than
a pipeline stage itself. This file produces the per-paper input it
consumes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from ai_paper_review.review.reviewer_db import AttributionTables, Reviewer

from .routing import is_low_sub_rating, route_category


def build_calibration(
    alignment: Dict[str, Any],
    ai_report: Dict[str, Any],
    reviewers: List[Reviewer],
    tables: AttributionTables,
    actual_reviews: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Produce concrete, actionable calibration suggestions for the DB.

    Per-persona noise ratio = false_alarms / comments_emitted. Low
    sub-ratings (OpenReview Soundness/Presentation/Contribution) are
    attributed to the expected persona via
    ``tables.sub_rating_to_persona``; category → persona routing goes
    through ``tables.category_to_persona``. Both maps are loaded from
    the reviewer DB's ``Validation Attribution Tables`` YAML block,
    so the personas they name always match the personas actually in
    the DB.
    """
    actual_reviews = actual_reviews or []

    # Backfill missing personas from the reviewer DB. When the AI-review
    # markdown lacks the ``**Persona:** ...`` header (external pipeline,
    # older prompt, LLM drift), ``load_ai`` leaves ``_persona`` empty,
    # which collapses the per-persona table into one blank-key row that's
    # useless for calibration.
    rid_to_persona: Dict[str, str] = {
        r.id: r.persona for r in reviewers if getattr(r, "id", None)
    }

    def _resolve_persona(rid: str, current: str) -> str:
        """Returns ``"?"`` when unknown so the key is a stable sentinel
        rather than an empty string that would collapse multiple rows."""
        if current:
            return current
        if rid and rid in rid_to_persona:
            return rid_to_persona[rid]
        return "?"

    for s in ai_report.get("selected", []):
        s["persona"] = _resolve_persona(s.get("id", ""), s.get("persona", ""))
    for rv in ai_report.get("raw_reviews", []):
        rv["_persona"] = _resolve_persona(
            rv.get("_reviewer_id", ""), rv.get("_persona", "")
        )
    # The alignment dicts hold their own copies of AI comment dicts with
    # `persona` fields that won't auto-refresh from ai_report — walk them too.
    for hit in alignment.get("hits", []):
        primary = hit.get("primary_ai", {})
        primary["persona"] = _resolve_persona(
            primary.get("reviewer_id", ""), primary.get("persona", "")
        )
        for sup in hit.get("supporting_ai", []):
            a = sup.get("ai", {})
            a["persona"] = _resolve_persona(
                a.get("reviewer_id", ""), a.get("persona", "")
            )
    for fa in alignment.get("false_alarms", []):
        fa["persona"] = _resolve_persona(
            fa.get("reviewer_id", ""), fa.get("persona", "")
        )

    # Compute AFTER backfill so persona_stats picks up the looked-up values.
    selected_personas = [s.get("persona", "?") for s in ai_report.get("selected", [])]

    persona_hits: Dict[str, int] = defaultdict(int)
    persona_total: Dict[str, int] = defaultdict(int)
    persona_false: Dict[str, int] = defaultdict(int)

    for c in ai_report.get("raw_reviews", []):
        p = c.get("_persona", "?")
        persona_total[p] += len(c.get("comments", []))

    matched_ai_comment_ids: set = set()
    for hit in alignment["hits"]:
        matched_ai_comment_ids.add(hit["primary_ai"]["id"])
        for s in hit["supporting_ai"]:
            matched_ai_comment_ids.add(s["ai"]["id"])

    for hit in alignment["hits"]:
        personas_seen: set = set()
        personas_seen.add(hit["primary_ai"]["persona"])
        for s in hit["supporting_ai"]:
            personas_seen.add(s["ai"]["persona"])
        for p in personas_seen:
            persona_hits[p] += 1

    for fa in alignment["false_alarms"]:
        persona_false[fa["persona"]] += 1

    persona_stats = []
    for sel in ai_report.get("selected", []):
        p = sel.get("persona", "?") or "?"
        rid = sel.get("id", "") or ""
        total = persona_total.get(p, 0)
        false = persona_false.get(p, 0)
        contrib = persona_hits.get(p, 0)
        persona_stats.append(
            {
                "reviewer_id": rid,
                "persona": p,
                "comments_emitted": total,
                "actual_comments_helped_catch": contrib,
                "false_alarms": false,
                "noise_ratio": round(false / total, 3) if total else None,
            }
        )

    miss_attributions: List[Dict[str, Any]] = []
    selection_failures = 0
    prompt_failures = 0
    uncovered_categories: Dict[str, int] = defaultdict(int)

    for m in alignment["misses"]:
        expected_persona = route_category(m["category"], tables.category_to_persona)
        if expected_persona is None:
            miss_attributions.append(
                {
                    "actual_comment": m["text"][:140],
                    "category": m["category"],
                    "expected_persona": None,
                    "failure_mode": "uncategorized",
                    "note": "Actual comment has no category or category not in routing map.",
                }
            )
            continue

        was_selected = expected_persona in selected_personas
        uncovered_categories[m["category"]] += 1
        if not was_selected:
            selection_failures += 1
            failure_mode = "selection_failure"
            note = (
                f"Persona '{expected_persona}' was NOT among the "
                f"{len(selected_personas)} selected reviewers, "
                "so no AI agent was watching for this issue."
            )
        else:
            prompt_failures += 1
            failure_mode = "prompt_failure"
            note = (
                f"Persona '{expected_persona}' WAS selected but still missed this issue. "
                "Consider strengthening its system prompt with the pattern below."
            )
        miss_attributions.append(
            {
                "actual_comment": m["text"][:160],
                "severity": m["severity"],
                "category": m["category"],
                "expected_persona": expected_persona,
                "expected_persona_selected": was_selected,
                "failure_mode": failure_mode,
                "note": note,
            }
        )

    # Concrete DB-level suggestions:
    #   (a) prompt_failure  → strengthen_persona_prompt
    #   (b) selection_failure → selection_policy_adjustment
    #   (c) high noise_ratio → reduce_persona_noise
    #   (d) low sub-rating  → sub_rating_signal
    #   (e) repeat misses in one category → topical_gap
    suggestions: List[Dict[str, Any]] = []

    miss_by_persona: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ma in miss_attributions:
        if ma.get("failure_mode") == "prompt_failure" and ma.get("expected_persona"):
            miss_by_persona[ma["expected_persona"]].append(ma)

    for persona, misses in miss_by_persona.items():
        examples = [m["actual_comment"] for m in misses[:3]]
        suggestions.append(
            {
                "type": "strengthen_persona_prompt",
                "target_persona": persona,
                "scope": "apply across all 10 domains",
                "rationale": (
                    f"This persona was selected but missed {len(misses)} issue(s) that a "
                    "human reviewer raised. The prompt likely doesn't cue it to look for the patterns below."
                ),
                "example_misses": examples,
                "prompt_patch_hint": (
                    f"Append to '{persona}' section of system prompts: "
                    "'Also explicitly check for: ' + <pattern extracted from examples>."
                ),
            }
        )

    missed_personas_needed = {
        ma["expected_persona"]
        for ma in miss_attributions
        if ma.get("failure_mode") == "selection_failure"
    }
    if missed_personas_needed:
        suggestions.append(
            {
                "type": "selection_policy_adjustment",
                "missing_personas_in_selection": sorted(missed_personas_needed),
                "rationale": (
                    "Human reviewers raised issues in these aspect areas, but the corresponding "
                    "AI personas were not selected for this paper."
                ),
                "fix_hint": (
                    "Consider raising `domain_bleed`, enforcing a persona floor, or adding these "
                    "personas' keywords to the paper-side query."
                ),
            }
        )

    for ps in persona_stats:
        nr = ps["noise_ratio"]
        if nr is None:
            continue
        if nr >= 0.75 and ps["comments_emitted"] >= 4:
            suggestions.append(
                {
                    "type": "reduce_persona_noise",
                    "target_persona": ps["persona"],
                    "noise_ratio": nr,
                    "comments_emitted": ps["comments_emitted"],
                    "rationale": (
                        f"{nr*100:.0f}% of this persona's comments did not "
                        "align with any human reviewer comment."
                    ),
                    "prompt_patch_hint": (
                        "Tighten scope in the prompt; require at least one paper-specific anchor "
                        "(quoted phrase, figure/table number) per comment."
                    ),
                }
            )

    sub_rating_attributions: List[Dict[str, Any]] = []
    for rv in actual_reviews:
        scale = rv.get("sub_rating_scale")
        for sr_name, sr_value in (rv.get("sub_ratings") or {}).items():
            if not isinstance(sr_value, (int, float)):
                continue
            if not is_low_sub_rating(int(sr_value), scale or 4):
                continue
            expected_persona = tables.sub_rating_to_persona.get(sr_name.lower())
            if expected_persona is None:
                sub_rating_attributions.append({
                    "reviewer_label": rv.get("reviewer_label"),
                    "sub_rating": sr_name, "value": sr_value, "scale": scale,
                    "expected_persona": None,
                    "failure_mode": "unmapped_sub_rating",
                    "note": (f"Sub-rating '{sr_name}' has no persona mapping in "
                             "the reviewer DB's 'Validation Attribution Tables' "
                             "block. Add a row under `sub_rating_to_persona:` if "
                             "this keeps appearing."),
                })
                continue
            selected = expected_persona in selected_personas
            persona_caught = persona_hits.get(expected_persona, 0) > 0
            if not selected:
                failure_mode = "selection_failure"
            elif not persona_caught:
                failure_mode = "prompt_failure"
            else:
                failure_mode = "caught"
            sub_rating_attributions.append({
                "reviewer_label": rv.get("reviewer_label"),
                "sub_rating": sr_name, "value": sr_value, "scale": scale,
                "expected_persona": expected_persona,
                "expected_persona_selected": selected,
                "persona_caught_related_weakness": persona_caught,
                "failure_mode": failure_mode,
            })

    sub_rating_misses: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for attr in sub_rating_attributions:
        if attr["failure_mode"] in ("selection_failure", "prompt_failure"):
            sub_rating_misses[(attr["sub_rating"], attr["expected_persona"])].append(attr)

    for (sr_name, persona), attrs in sub_rating_misses.items():
        failure_modes = {a["failure_mode"] for a in attrs}
        suggestions.append({
            "type": "sub_rating_signal",
            "target_persona": persona,
            "sub_rating": sr_name,
            "support": len(attrs),
            "reviewers": sorted({a["reviewer_label"] for a in attrs}),
            "failure_modes": sorted(failure_modes),
            "rationale": (
                f"{len(attrs)} human reviewer(s) gave a low '{sr_name}' sub-rating, "
                f"which maps to the '{persona}' AI persona. That persona either wasn't "
                f"selected or didn't produce a matching comment on this paper."
            ),
            "fix_hint": (
                "If selection_failure: ensure this persona is selected whenever keywords "
                "relevant to it appear in the paper. If prompt_failure: strengthen the "
                f"'{persona}' prompt so it probes for '{sr_name}'-type weaknesses more aggressively."
            ),
        })

    uncovered_sorted = sorted(uncovered_categories.items(), key=lambda x: -x[1])
    for cat, n in uncovered_sorted:
        expected = route_category(cat, tables.category_to_persona)
        if expected and expected not in miss_by_persona and expected not in missed_personas_needed:
            continue
        suggestions.append(
            {
                "type": "topical_gap",
                "category": cat,
                "miss_count": n,
                "expected_persona": expected,
                "rationale": f"Category '{cat}' accounted for {n} missed human comment(s).",
                "fix_hint": (
                    "If no persona handles this category, consider adding or renaming a persona. "
                    "If a persona exists but is under-prompted, strengthen its scope."
                ),
            }
        )

    return {
        "persona_stats": persona_stats,
        "miss_attributions": miss_attributions,
        "sub_rating_attributions": sub_rating_attributions,
        "summary": {
            "selection_failures": selection_failures,
            "prompt_failures": prompt_failures,
            "uncovered_categories": dict(uncovered_categories),
            "sub_rating_signals": len([a for a in sub_rating_attributions
                                        if a["failure_mode"] in ("selection_failure", "prompt_failure")]),
        },
        "suggestions": suggestions,
    }
