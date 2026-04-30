"""Validator: alignment, strength contradictions, sub-rating attribution."""
from __future__ import annotations

from pathlib import Path


from ai_paper_review import default_db_path
from ai_paper_review.review.parsing import review_dict_to_markdown
from ai_paper_review.review.reviewer_db import parse_reviewer_database
from ai_paper_review.validation import (
    alignment as _alignment,
    calibration as _calibration,
    loading as _loading,
)


# Convenience namespace so test bodies stay terse and readable.
class validate:  # noqa: N801 — mimic the old `import as` shape, scoped to tests
    load_actual = staticmethod(_loading.load_actual)
    load_ai = staticmethod(_loading.load_ai)
    align_comments = staticmethod(_alignment.align_comments)
    build_calibration = staticmethod(_calibration.build_calibration)


def _write_reviews_md(path: Path, data: dict):
    """Serialize a reviews dict to markdown for test fixtures."""
    md_lines = ["# Converted Reviews", ""]
    md_lines.append(f"**Paper ID:** {data.get('paper_id', 'test')}")
    md_lines.append(f"**Title:** {data.get('title', 'Test')}")
    md_lines.append("")
    reviews_key = "actual_reviews" if "actual_reviews" in data else "raw_reviews"
    for i, rv in enumerate(data.get(reviews_key, []), start=1):
        md_lines.append("---")
        md_lines.append("")
        rv_copy = dict(rv)
        rv_copy.setdefault("overall_recommendation", rv.get("recommendation", ""))
        rv_copy.setdefault("reviewer_id", rv.get("_reviewer_id", f"R{i:03d}"))
        rv_copy.setdefault("domain", rv.get("_domain", ""))
        rv_copy.setdefault("persona", rv.get("_persona", ""))
        rv_md = review_dict_to_markdown(rv_copy)
        rv_md = rv_md.replace("# Review\n", f"# Review {i}\n", 1)
        md_lines.append(rv_md)
    path.write_text("\n".join(md_lines))


def test_load_actual_fills_defaults(tmp_path, actual_review):
    p = tmp_path / "a.md"
    _write_reviews_md(p, actual_review)
    loaded = validate.load_actual(str(p))
    assert len(loaded["flat_comments"]) == 2
    assert len(loaded["flat_strengths"]) == 2


def test_load_actual_minimal_input(tmp_path):
    """Hand-written minimal markdown still validates — optional fields default."""
    md = """# Converted Reviews

**Paper ID:** min

---

# Review 1

**Recommendation:** weak_reject

## Comment 1
- **Severity:** major
- **Category:** novelty
- **Summary:** foo
- **Description:** foo
"""
    p = tmp_path / "a.md"
    p.write_text(md)
    loaded = validate.load_actual(str(p))
    assert loaded["flat_strengths"] == []


def test_sub_rating_attribution_routes_correctly(tmp_path, actual_review, ai_review, mock_llm_for_fixtures):
    """Low soundness should route to Methodology Critic."""
    ap = tmp_path / "actual.md"; _write_reviews_md(ap, actual_review)
    ip = tmp_path / "ai.md";     _write_reviews_md(ip, ai_review)

    actual = validate.load_actual(str(ap))
    ai = validate.load_ai(str(ip))
    alignment = validate.align_comments(
        actual["flat_comments"], ai["flat_comments"], mock_llm_for_fixtures,
    )
    _db = parse_reviewer_database(str(default_db_path()))
    cal = validate.build_calibration(
        alignment, ai, _db.reviewers, _db.tables,
        actual_reviews=actual["actual_reviews"],
    )
    attrs = cal["sub_rating_attributions"]
    soundness = [a for a in attrs if a["sub_rating"] == "soundness"]
    assert soundness and soundness[0]["expected_persona"] == "Methodology Critic"
    assert soundness[0]["failure_mode"] == "selection_failure"


def test_attribution_tables_loaded_from_bundled_db():
    """The validation attribution tables now live inside the reviewer
    DB markdown. Parsing the bundled DB must yield the three maps with
    persona names that exist as `#### R### — Persona` entries in the
    same file — otherwise calibration would attribute misses to phantom
    personas."""
    _db = parse_reviewer_database(str(default_db_path()))
    tables = _db.tables
    # Vocab isn't empty and contains at least the core categories.
    assert "novelty" in tables.category_vocab
    assert "methodology" in tables.category_vocab
    assert "evaluation" in tables.category_vocab
    # Category map uses lowercase keys and names real personas from the DB.
    persona_names = {r.persona for r in _db.reviewers}
    assert tables.category_to_persona["novelty"] == "Novelty Hunter"
    assert tables.category_to_persona["methodology"] == "Methodology Critic"
    # Every persona on the RHS must exist in the DB (sanity — no
    # phantoms) so calibration never attributes to a reviewer that
    # isn't in the selection pool.
    for p in tables.category_to_persona.values():
        assert p in persona_names, f"phantom persona in attribution map: {p!r}"
    for p in tables.sub_rating_to_persona.values():
        assert p in persona_names, f"phantom persona in sub-rating map: {p!r}"
    # Sub-rating map covers the OpenReview-standard sub-ratings.
    assert tables.sub_rating_to_persona["soundness"] == "Methodology Critic"
    assert tables.sub_rating_to_persona["presentation"] == "Clarity & Presentation Editor"


def test_attribution_tables_missing_block_returns_empty(tmp_path):
    """A reviewer DB markdown file without the attribution-tables block
    parses into empty maps — calibration against such a DB just skips
    miss attribution, never crashes."""
    src = str(default_db_path())
    stripped = tmp_path / "stripped.md"
    # Copy the bundled DB but truncate everything from section 7 onward.
    content = Path(src).read_text()
    marker = "## 7. Validation Attribution Tables"
    cut = content.split(marker, 1)[0]
    stripped.write_text(cut)
    _db = parse_reviewer_database(str(stripped))
    assert _db.reviewers  # reviewers still parse fine
    assert _db.tables.category_vocab == []
    assert _db.tables.category_to_persona == {}
    assert _db.tables.sub_rating_to_persona == {}


def test_calibration_emits_expected_suggestions(tmp_path, actual_review, ai_review, mock_llm_for_fixtures):
    ap = tmp_path / "actual.md"; _write_reviews_md(ap, actual_review)
    ip = tmp_path / "ai.md";     _write_reviews_md(ip, ai_review)

    actual = validate.load_actual(str(ap))
    ai = validate.load_ai(str(ip))
    alignment = validate.align_comments(
        actual["flat_comments"], ai["flat_comments"], mock_llm_for_fixtures,
    )
    _db = parse_reviewer_database(str(default_db_path()))
    cal = validate.build_calibration(
        alignment, ai, _db.reviewers, _db.tables,
        actual_reviews=actual["actual_reviews"],
    )
    types = {s["type"] for s in cal["suggestions"]}
    # `sub_rating_signal` is the remaining calibration signal exposed by
    # this fixture. `reduce_persona_noise` used to fire too, but only
    # because the removed strength-contradiction feature halved the
    # comments-emitted threshold from 4 → 2.
    assert "sub_rating_signal" in types

    summary = cal["summary"]
    assert summary["sub_rating_signals"] >= 1


def test_batch_llm_alignment_builds_matches_from_similarity_matrix():
    """align_comments issues ONE LLM call with all comments, parses a
    similarity matrix from the response, and builds hits/misses/false_alarms.
    Thresholds: same ≥ 0.65, partial ≥ 0.35."""
    from ai_paper_review.validation.alignment import align_comments

    # Scripted similarity matrix: H1↔A3 strong, H2↔A1 partial + A5 partial,
    # H3 unmatched, A7 / A9 false alarms.
    scripted = {
        ("H1", "A1"): 0.10, ("H1", "A3"): 0.91, ("H1", "A5"): 0.15,
        ("H1", "A7"): 0.02, ("H1", "A9"): 0.03,
        ("H2", "A1"): 0.55, ("H2", "A3"): 0.22, ("H2", "A5"): 0.48,
        ("H2", "A7"): 0.01, ("H2", "A9"): 0.02,
        ("H3", "A1"): 0.08, ("H3", "A3"): 0.05, ("H3", "A5"): 0.04,
        ("H3", "A7"): 0.11, ("H3", "A9"): 0.22,
    }

    class FakeClient:
        model = "fake-batch-1.0"
        calls = []

        def complete(self, system, user, max_tokens=4000):
            FakeClient.calls.append((system[:40], user[:40], max_tokens))
            # Emit all 15 pairs as `H_id | A_id | score` lines
            lines = ["### Similarity scores", ""]
            for (hid, aid), sim in scripted.items():
                lines.append(f"{hid} | {aid} | {sim:.2f}")
            lines += ["", "### Ranked human comments", ""]
            # Sort H by best sim
            best = {}
            for (hid, aid), sim in scripted.items():
                if sim > best.get(hid, (0, ""))[0]:
                    best[hid] = (sim, aid)
            for rank, (hid, (sim, aid)) in enumerate(
                    sorted(best.items(), key=lambda t: -t[1][0]), start=1):
                lines.append(f"{rank}. {hid} — best_match={aid} sim={sim:.2f}")
            return "\n".join(lines)

    actual = [
        {"id": "H1", "text": "Missing SOTA baseline",
         "severity": "major", "category": "evaluation"},
        {"id": "H2", "text": "Evaluation is weak",
         "severity": "major", "category": "evaluation"},
        {"id": "H3", "text": "Section 4 is ambiguous about model sizes",
         "severity": "minor", "category": "presentation"},
    ]
    ai = [
        {"id": "A1", "summary": "Weak baselines", "description": "x",
         "severity": "major", "reviewer_id": "R1"},
        {"id": "A3", "summary": "No SOTA comparison", "description": "x",
         "severity": "major", "reviewer_id": "R2"},
        {"id": "A5", "summary": "Not enough experiments", "description": "x",
         "severity": "major", "reviewer_id": "R3"},
        {"id": "A7", "summary": "Typos", "description": "x",
         "severity": "minor", "reviewer_id": "R4"},
        {"id": "A9", "summary": "Figure 2 color scheme", "description": "x",
         "severity": "minor", "reviewer_id": "R5"},
    ]

    alignment = align_comments(actual, ai, FakeClient())

    # 3 human comments fit in one chunk (≤10), so exactly 1 LLM call.
    assert len(FakeClient.calls) == 1, \
        f"expected 1 chunk call, got {len(FakeClient.calls)}"

    hit_by_h = {h["actual"]["id"]: h for h in alignment["hits"]}
    assert "H1" in hit_by_h and "H2" in hit_by_h
    assert hit_by_h["H1"]["primary_ai"]["id"] == "A3"
    assert hit_by_h["H1"]["llm_verdict"] == "same"
    assert hit_by_h["H1"]["primary_sim"] >= 0.65
    assert hit_by_h["H2"]["primary_ai"]["id"] == "A1"
    assert hit_by_h["H2"]["llm_verdict"] == "partial"
    sup_ids = {s["ai"]["id"] for s in hit_by_h["H2"]["supporting_ai"]}
    assert "A5" in sup_ids

    miss_ids = {m["id"] for m in alignment["misses"]}
    assert miss_ids == {"H3"}

    fa_ids = {fa["id"] for fa in alignment["false_alarms"]}
    assert fa_ids == {"A7", "A9"}

    assert alignment.get("aligner") == "batch-llm"


def test_align_raises_on_llm_error():
    """When a chunked LLM call fails, align_comments raises — there is no
    embedding fallback, and callers need to see the underlying error."""
    import pytest
    from ai_paper_review.validation.alignment import align_comments

    class FailingClient:
        model = "fake-broken"
        def complete(self, system, user, max_tokens=20):
            raise RuntimeError("quota exceeded")

    actual = [{"id": "H1",
               "text": "The evaluation is missing a comparison against PriorWork.",
               "severity": "major", "category": "evaluation"}]
    ai = [{"id": "A1", "summary": "Missing baseline comparison",
           "description": "No comparison to PriorWork in the evaluation.",
           "severity": "major", "reviewer_id": "R1"}]

    with pytest.raises(RuntimeError, match="Batch alignment LLM call failed"):
        align_comments(actual, ai, FailingClient())

