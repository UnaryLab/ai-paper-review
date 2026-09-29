"""Validator: loading, chunked LLM alignment, calibration, sub-rating attribution, report."""
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
    rv = loaded["actual_reviews"][0]
    assert rv["recommendation_raw"] is None
    assert rv["recommendation_scale"] is None
    assert rv["confidence_raw"] is None
    assert rv["sub_ratings"] == {}
    assert rv["sub_rating_scale"] is None
    assert rv["paper_summary"] is None
    assert rv["strengths"] == []
    assert loaded["flat_strengths"] == []
    [c] = loaded["flat_comments"]
    assert (c["severity"], c["category"], c["text"]) == ("major", "novelty", "foo")


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
    parses into empty maps, and calibration against such a DB leaves
    misses and low sub-ratings unattributed instead of crashing."""
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

    alignment = {
        "hits": [], "false_alarms": [], "n_ai": 0,
        "misses": [{"text": "not novel", "severity": "major",
                    "category": "novelty"}],
    }
    ai_report = {"selected": [], "raw_reviews": []}
    reviews = [{"reviewer_label": "Rev1", "sub_rating_scale": 4,
                "sub_ratings": {"soundness": 1}}]
    cal = validate.build_calibration(alignment, ai_report, _db.reviewers,
                                     _db.tables, actual_reviews=reviews)
    assert [m["failure_mode"] for m in cal["miss_attributions"]] == ["uncategorized"]
    assert [a["failure_mode"] for a in cal["sub_rating_attributions"]] \
        == ["unmapped_sub_rating"]
    assert [s["type"] for s in cal["suggestions"]] == ["topical_gap"]
    assert cal["summary"]["sub_rating_signals"] == 0


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
    # The fixture's only calibration signal: a low soundness sub-rating
    # whose persona (Methodology Critic) was not selected.
    [sug] = cal["suggestions"]
    assert sug["type"] == "sub_rating_signal"
    assert sug["target_persona"] == "Methodology Critic"
    assert sug["sub_rating"] == "soundness"
    assert sug["failure_modes"] == ["selection_failure"]

    summary = cal["summary"]
    assert summary["sub_rating_signals"] == 1


def test_batch_llm_alignment_builds_matches_from_similarity_matrix():
    """align_comments sends each chunk of human comments (with all AI
    comments) in one LLM call, parses the similarity matrix from the
    responses, and builds hits/misses/false_alarms.
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

    # 3 human comments fit in one chunk (≤5), so exactly 1 LLM call.
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



def test_alignment_parallel_calls_capped_by_max_concurrent():
    """Chunks run in parallel, but never more at once than max_concurrent."""
    import re
    import threading
    import time
    from ai_paper_review.validation.alignment import align_comments

    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    class SlowClient:
        model = "fake"
        def complete(self, system, user, max_tokens=4000):
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            time.sleep(0.1)
            with lock:
                state["running"] -= 1
            hids = re.findall(r"- \*\*(H\d+)\*\*:", user)
            return "\n".join(f"{h} | A1 | 0.10" for h in hids)

    actual = [{"id": f"H{i}", "text": f"comment {i}"} for i in range(1, 16)]
    ai = [{"id": "A1", "summary": "s", "description": "d"}]
    align_comments(actual, ai, SlowClient(), max_concurrent=2)
    assert state["peak"] == 2


def _scripted_client(lines_for, model="fake"):
    """Fake LLM client: ``lines_for(hids, aids)`` returns the response
    lines for the human/AI ids found in the prompt; records max_tokens."""
    import re

    class _Client:
        calls = []

        def complete(self, system, user, max_tokens=4000):
            _Client.calls.append(max_tokens)
            hids = re.findall(r"- \*\*(H\d+)\*\*:", user)
            aids = re.findall(r"- \*\*(A\d+)\*\*:", user)
            return "\n".join(lines_for(hids, aids))

    _Client.model = model
    return _Client()


def test_v1_alignment_budget_and_incomplete_chunk_marked(tmp_path, caplog):
    """Budget is 25/pair + 40/human row; a chunk that parses fewer lines
    than its pair count is warned about and marked in the analysis file."""
    import logging
    from ai_paper_review.validation.alignment import align_comments

    actual = [{"id": f"H{i}", "text": f"h{i}"} for i in range(1, 6)]
    ai = [{"id": f"A{j}", "summary": f"a{j}", "description": "d"}
          for j in range(1, 101)]
    # Drop one pair out of 500 (99.8% parsed, above the old 50% check).
    client = _scripted_client(
        lambda hids, aids: [f"{h} | {a} | 0.10" for h in hids for a in aids][:-1])
    with caplog.at_level(logging.WARNING, logger="validator"):
        align_comments(actual, ai, client, run_dir=tmp_path)
    assert client.calls == [5 * 100 * 25 + 40 * 5]
    assert any("incomplete" in r.getMessage() for r in caplog.records)
    analysis = (tmp_path / "alignment_llm_analysis.md").read_text()
    assert "INCOMPLETE, parsed 499 / 500" in analysis


def test_v2_topical_gap_reports_unmapped_categories_only():
    from ai_paper_review.review.reviewer_db import AttributionTables

    tables = AttributionTables(category_to_persona={"novelty": "Novelty Hunter"})
    alignment = {
        "hits": [], "false_alarms": [], "n_ai": 0,
        "misses": [
            {"text": "not novel", "severity": "major", "category": "novelty"},
            {"text": "ethics issue", "severity": "major", "category": "ethics"},
        ],
    }
    ai_report = {"selected": [{"id": "R1", "persona": "Theorist"}],
                 "raw_reviews": []}
    cal = validate.build_calibration(alignment, ai_report, [], tables)
    gaps = [s for s in cal["suggestions"] if s["type"] == "topical_gap"]
    assert [g["category"] for g in gaps] == ["ethics"]
    assert gaps[0]["expected_persona"] is None
    assert cal["summary"]["uncovered_categories"] == {"novelty": 1, "ethics": 1}


def test_v5_loaders_raise_on_empty_input(tmp_path):
    import pytest

    no_comments = tmp_path / "a.md"
    no_comments.write_text(
        "# Converted Reviews\n\n**Paper ID:** x\n\n---\n\n# Review 1\n\n"
        "**Recommendation:** accept\n")
    with pytest.raises(ValueError):
        validate.load_actual(str(no_comments))
    with pytest.raises(ValueError):
        validate.load_ai(str(no_comments))

    no_reviews = tmp_path / "b.md"
    no_reviews.write_text("# Converted Reviews\n\n**Paper ID:** x\n")
    with pytest.raises(ValueError):
        validate.load_actual(str(no_reviews))
    with pytest.raises(ValueError):
        validate.load_ai(str(no_reviews))


def test_v6_cli_default_db_is_bundled_db(monkeypatch):
    import sys
    import pytest
    from ai_paper_review.validation import validation as cli

    seen = {}

    class _Stop(Exception):
        pass

    def _fake_parse(path):
        seen["db"] = path
        raise _Stop

    monkeypatch.setattr(cli, "load_actual", lambda p: {})
    monkeypatch.setattr(cli, "load_ai", lambda p: {})
    monkeypatch.setattr(cli, "parse_reviewer_database", _fake_parse)
    monkeypatch.setattr(sys, "argv", ["validate", "--actual", "a.md",
                                      "--ai-review", "b.md"])
    with pytest.raises(_Stop):
        cli.main()
    assert seen["db"] == str(default_db_path())
    assert Path(seen["db"]).exists()


def test_v7_empty_or_repeated_reviewer_ids_get_unique_comment_ids(tmp_path):
    from ai_paper_review.validation.conversion import parse_llm_markdown

    block = ("## Comment 1\n- **Severity:** major\n- **Category:** novelty\n"
             "- **Summary:** s\n- **Description:** d\n")
    md = ("# Converted Reviews\n\n**Paper ID:** x\n\n---\n\n"
          f"# Review 1\n\n{block}\n---\n\n# Review 2\n\n{block}\n---\n\n"
          f"# Review 3\n\n**Reviewer ID:** R1\n\n{block}\n---\n\n"
          f"# Review 4\n\n**Reviewer ID:** R1\n\n{block}")
    p = tmp_path / "a.md"
    p.write_text(md)
    for loaded in (validate.load_actual(str(p)), validate.load_ai(str(p))):
        ids = [c["id"] for c in loaded["flat_comments"]]
        assert len(ids) == 4 and len(set(ids)) == 4, ids

    conv = parse_llm_markdown(
        f"# Review 1\n\n**Reviewer ID:** Reviewer #1\n\n{block}\n---\n\n"
        f"# Review 2\n\n**Reviewer ID:** Reviewer 1\n\n{block}")
    cids = [c["comment_id"] for rv in conv["actual_reviews"]
            for c in rv["comments"]]
    assert len(set(cids)) == 2, cids


def test_v8_persona_stats_count_only_aligned_comments(caplog):
    import logging
    from ai_paper_review.review.reviewer_db import AttributionTables
    from ai_paper_review.validation.alignment import align_comments

    ai = [{"id": f"A{j}", "summary": "s", "description": "d",
           "reviewer_id": "R1" if j <= 2 else "R2",
           "persona": "P1" if j <= 2 else "P2", "severity": "minor"}
          for j in range(1, 6)]
    actual = [{"id": "H1", "text": "h", "severity": "major", "category": ""}]
    client = _scripted_client(
        lambda hids, aids: [f"{h} | {a} | 0.10" for h in hids for a in aids])
    with caplog.at_level(logging.WARNING, logger="validator"):
        alignment = align_comments(actual, ai, client, max_comments_per_side=2)
    assert any("3 dropped" in r.getMessage() for r in caplog.records)

    ai_report = {
        "selected": [{"id": "R1", "persona": "P1"},
                     {"id": "R2", "persona": "P2"}],
        "raw_reviews": [
            {"_reviewer_id": "R1", "_persona": "P1", "comments": ai[:2]},
            {"_reviewer_id": "R2", "_persona": "P2", "comments": ai[2:]},
        ],
    }
    cal = validate.build_calibration(alignment, ai_report, [], AttributionTables())
    by_p = {ps["persona"]: ps for ps in cal["persona_stats"]}
    assert by_p["P1"]["comments_emitted"] == 2
    assert by_p["P1"]["noise_ratio"] == 1.0
    assert by_p["P2"]["comments_emitted"] == 0
    assert by_p["P2"]["noise_ratio"] is None


def test_v9_staggered_submission_stops_after_failure():
    import pytest
    from ai_paper_review.validation.alignment import align_comments

    calls = []

    class FailingClient:
        model = "fake"

        def complete(self, system, user, max_tokens=4000):
            calls.append(1)
            raise RuntimeError("quota exceeded")

    actual = [{"id": f"H{i}", "text": f"h{i}"} for i in range(1, 16)]
    ai = [{"id": "A1", "summary": "s", "description": "d"}]
    with pytest.raises(RuntimeError, match="Batch alignment LLM call failed"):
        align_comments(actual, ai, FailingClient(),
                       chunk_stagger_s=0.2, max_concurrent=3)
    assert len(calls) == 1


def test_v11_report_labels_counts_and_sub_ratings(tmp_path, actual_review):
    from ai_paper_review.review.reviewer_db import AttributionTables
    from ai_paper_review.validation.alignment import align_comments
    from ai_paper_review.validation.metrics import compute_metrics
    from ai_paper_review.validation.reporting import format_report

    # (c) reviewer_label is stored on each review.
    p = tmp_path / "a.md"
    _write_reviews_md(p, actual_review)
    loaded = validate.load_actual(str(p))
    assert loaded["actual_reviews"][0]["reviewer_label"] == "Reviewer_qFvT"

    # (a) distinct supporting reviewers, excluding the primary's.
    ai = [
        {"id": "A1", "summary": "s1", "description": "d", "severity": "major",
         "reviewer_id": "R1", "persona": "P1"},
        {"id": "A2", "summary": "s2", "description": "d", "severity": "major",
         "reviewer_id": "R1", "persona": "P1"},
        {"id": "A3", "summary": "s3", "description": "d", "severity": "major",
         "reviewer_id": "R2", "persona": "P2"},
    ]
    actual = [{"id": "H1", "text": "h", "severity": "major",
               "category": "novelty"}]
    sims = {"A1": 0.9, "A2": 0.8, "A3": 0.7}
    client = _scripted_client(
        lambda hids, aids: [f"{h} | {a} | {sims[a]}" for h in hids for a in aids])
    alignment = align_comments(actual, ai, client)

    # (d) "2 fair"-style sub-rating values are parsed.
    tables = AttributionTables(sub_rating_to_persona={"soundness": "P3"})
    ai_report = {"selected": [{"id": "R1", "persona": "P1"},
                              {"id": "R2", "persona": "P2"}],
                 "raw_reviews": []}
    reviews = [{"reviewer_label": "Rev1", "sub_rating_scale": 4,
                "sub_ratings": {"soundness": "2 fair"}}]
    cal = validate.build_calibration(alignment, ai_report, [], tables,
                                     actual_reviews=reviews)
    attrs = cal["sub_rating_attributions"]
    assert [a["sub_rating"] for a in attrs] == ["soundness"]

    # (b) title / venue fall back when the parsed value is "".
    report = format_report(
        {"title": "", "venue": "", "paper_id": "pid-1"}, ai_report,
        alignment, compute_metrics(alignment), cal)
    assert "**Title:** pid-1" in report
    assert "**Venue:** n/a" in report
    # (a) in the report: one other reviewer (R2), R1's own A2 excluded.
    assert "Also raised by 1 other AI reviewer(s)" in report


def test_v12_parser_accepts_decorated_rows_and_raises_on_zero_rows():
    import numpy as np
    import pytest
    from ai_paper_review.validation.alignment import (
        _parse_batch_similarity_matrix, align_comments,
    )

    raw = ("| H1 | A1 | 0.90 |\n"
           "- H2 | A1 | 0.80\n"
           "3. H3 | A1 | 0.70\n"
           "`H4` | `A1` | 0.60\n")
    sims, n = _parse_batch_similarity_matrix(raw, ["H1", "H2", "H3", "H4"], ["A1"])
    assert n == 4
    assert np.allclose(sims[:, 0], [0.9, 0.8, 0.7, 0.6])

    client = _scripted_client(lambda hids, aids: ["I cannot score these."])
    with pytest.raises(RuntimeError, match="parsed 0"):
        align_comments([{"id": "H1", "text": "h"}],
                       [{"id": "A1", "summary": "s", "description": "d"}],
                       client)


def test_v11a_supporting_list_matches_distinct_reviewer_count():
    from ai_paper_review.validation.alignment import align_comments
    from ai_paper_review.validation.metrics import compute_metrics
    from ai_paper_review.validation.reporting import format_report

    ai = [
        {"id": "A1", "summary": "s1", "description": "d", "severity": "major",
         "reviewer_id": "R1", "persona": "P1"},
        {"id": "A2", "summary": "s2", "description": "d", "severity": "major",
         "reviewer_id": "R1", "persona": "P1"},
        {"id": "A3", "summary": "s3", "description": "d", "severity": "major",
         "reviewer_id": "R2", "persona": "P2"},
    ]
    actual = [{"id": "H1", "text": "h", "severity": "major",
               "category": "novelty"}]
    sims = {"A1": 0.9, "A2": 0.8, "A3": 0.7}
    client = _scripted_client(
        lambda hids, aids: [f"{h} | {a} | {sims[a]}" for h in hids for a in aids])
    alignment = align_comments(actual, ai, client)
    ai_report = {"selected": [{"id": "R1", "persona": "P1"},
                              {"id": "R2", "persona": "P2"}],
                 "raw_reviews": []}
    cal = validate.build_calibration(alignment, ai_report, [], {})
    report = format_report({"title": "t", "venue": "v", "paper_id": "p"},
                           ai_report, alignment, compute_metrics(alignment), cal)
    assert "Also raised by 1 other AI reviewer(s): R2 (0.70)\n" in report


def test_report_semantic_comparison_is_count_summary():
    from ai_paper_review.validation.alignment import align_comments
    from ai_paper_review.validation.metrics import compute_metrics
    from ai_paper_review.validation.reporting import format_report

    ai = [{"id": "A1", "summary": "s1", "description": "d", "severity": "major",
           "reviewer_id": "R1", "persona": "P1"}]
    actual = [{"id": "H1", "text": "h", "severity": "major",
               "category": "novelty"}]
    client = _scripted_client(
        lambda hids, aids: [f"{h} | {a} | 0.9" for h in hids for a in aids])
    alignment = align_comments(actual, ai, client)
    llm_comparison = alignment["llm_comparison"]
    ai_report = {"selected": [{"id": "R1", "persona": "P1"}], "raw_reviews": []}
    cal = validate.build_calibration(alignment, ai_report, [], {})
    report = format_report({"title": "t", "venue": "v", "paper_id": "p"},
                           ai_report, alignment, compute_metrics(alignment), cal,
                           llm_comparison=llm_comparison)
    assert "## Semantic Comparison (LLM)\n" in report
    assert f"**Summary:** {llm_comparison['summary']}\n" in report
    assert "1 hits, 0 misses, 0 false alarms" in report
    assert "adds interpretation" not in report
    assert "natural-language" not in report


def test_batch_artifacts_use_positional_ids_for_comments_without_id(tmp_path):
    import numpy as np
    from ai_paper_review.validation.alignment import _write_batch_artifacts

    _write_batch_artifacts(
        tmp_path, [{"text": "h1"}, {"text": "h2"}],
        [{"summary": "a1"}, {"summary": "a2"}],
        np.array([[0.9, 0.1], [0.2, 0.8]], dtype=np.float32),
        "raw", 4, "m")
    sims = (tmp_path / "alignment_similarities.md").read_text()
    assert "| human \\\\ AI | A1 | A2 | best |" in sims
    assert "| H2 | A2 | 0.80 | same |" in sims
    ranking = (tmp_path / "alignment_ranking.md").read_text()
    assert "| 1 | H1 | A1 | 0.90 | same |" in ranking


def test_extraction_prompt_lists_only_db_category_vocab():
    from ai_paper_review.validation.conversion import _build_system_prompt

    vocab = ["novelty", "evaluation", "ethics review"]
    prompt = _build_system_prompt(vocab)
    assert "practical" not in prompt
    assert "industry" not in prompt
    assert prompt.count(", ".join(vocab)) >= 1
    category_lines = [ln for ln in prompt.splitlines() if "ategory" in ln]
    assert all("scalability" not in ln for ln in category_lines)


def test_llm_extract_uses_given_config(monkeypatch):
    """With ``llm_config`` given, llm_extract builds its client from that
    config, does not read config.yaml, and leaves the caller's object
    unchanged when overrides are passed."""
    from ai_paper_review.llm import config as _config, factory as _factory
    from ai_paper_review.llm.config import LLMConfig
    from ai_paper_review.validation.conversion import llm_extract

    class _Client:
        model = "m-override"

        def complete(self, system, user, max_tokens):
            return ("# Review 1\n\n## Comment 1\n- **Severity:** major\n"
                    "- **Category:** novelty\n- **Summary:** s\n"
                    "- **Description:** d\n")

    seen = {}

    def no_load():
        raise AssertionError("load_config called despite llm_config")

    def make(cfg, use_case="default"):
        seen["provider"] = cfg.resolve_provider(use_case)
        seen["model"] = cfg.resolve_model(use_case)
        seen["base_url"] = cfg.resolve_base_url_for_stage(use_case)
        return _Client()

    monkeypatch.setattr(_config, "load_config", no_load)
    monkeypatch.setattr(_factory, "make_client", make)
    cfg = LLMConfig(review_provider="openai_api", review_model="gpt-4o",
                    validation_base_url="http://submit.example/v1")
    out = llm_extract("review text", ["novelty"],
                      provider_override="openai_compatible_api",
                      model_override="m-override", llm_config=cfg)
    assert len(out["actual_reviews"]) == 1
    assert seen == {"provider": "openai_compatible_api", "model": "m-override",
                    "base_url": "http://submit.example/v1"}
    assert cfg.validation_provider is None
    assert cfg.validation_model is None
