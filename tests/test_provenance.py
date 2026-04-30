"""Provenance header — format + coverage across every .md writer.

The core `format_provenance()` helper is covered first (unit), then each
pipeline's output path gets a tiny end-to-end check confirming the
generated markdown carries the `<!-- provenance -->` marker plus the
expected LLM / timing lines. Anything that slips past these tests is a
silent regression: reports would look fine but lose their audit trail.
"""
from __future__ import annotations

from ai_paper_review.provenance import format_provenance, now_iso


def test_format_provenance_llm_stage():
    block = format_provenance(
        provider="anthropic_api",
        model="claude-sonnet-4-5-20250929",
        base_url=None,
        launched_at="2026-04-23T02:35:00+00:00",
        ended_at="2026-04-23T02:42:17+00:00",
    )
    assert "<!-- provenance -->" in block
    assert "`anthropic_api` / `claude-sonnet-4-5-20250929`" in block
    assert "(default)" in block  # base_url=None rendered
    assert "2026-04-23T02:35:00+00:00" in block
    assert "2026-04-23T02:42:17+00:00" in block
    assert "duration: 7m 17s" in block
    # Must end with a section separator so downstream content reads cleanly.
    assert block.rstrip().endswith("---")


def test_format_provenance_reporter_stage():
    """Aggregation doesn't call an LLM; render the block with
    provider=None and confirm the LLM line flips to "not applicable"."""
    block = format_provenance(
        provider=None, model=None, base_url=None,
        launched_at="2026-04-23T02:35:00+00:00",
        ended_at="2026-04-23T02:35:05+00:00",
    )
    assert "not applicable (reporter stage)" in block
    assert "duration: 5.0s" in block


def test_format_provenance_includes_format_fix_retries():
    """When the caller passes ``format_fix_retries`` + ``n_reviewers_total``
    (review stage), the provenance block appends a trailing **Format-fix
    retries:** line so the tally lives with the rest of the run metadata
    rather than inline in the report body."""
    block = format_provenance(
        provider="anthropic_api",
        model="claude-sonnet-4-5-20250929",
        base_url=None,
        launched_at="2026-04-23T02:35:00+00:00",
        ended_at="2026-04-23T02:37:30+00:00",
        format_fix_retries=2,
        n_reviewers_total=11,
    )
    assert "**Format-fix retries:** 2 of 11 reviewer(s)" in block
    # The line must come after Ended (so timing reads naturally first).
    assert block.index("**Ended:**") < block.index("**Format-fix retries:**")


def test_format_provenance_omits_retries_line_when_not_given():
    """Validation / aggregation don't have a reviewer-dispatch retry
    concept, so the line must not render when the kwargs are absent."""
    block = format_provenance(
        provider="anthropic_api",
        model="claude-sonnet-4-5-20250929",
        base_url=None,
        launched_at="2026-04-23T02:35:00+00:00",
        ended_at="2026-04-23T02:37:30+00:00",
    )
    assert "Format-fix retries:" not in block


def test_format_provenance_custom_base_url():
    block = format_provenance(
        provider="openai_compatible_api",
        model="llama3.1:8b",
        base_url="http://localhost:11434/v1",
        launched_at="2026-04-23T02:35:00+00:00",
        ended_at="2026-04-23T02:35:01+00:00",
    )
    assert "http://localhost:11434/v1" in block


def test_now_iso_round_trips():
    from datetime import datetime
    iso = now_iso()
    # Must be ISO8601 parseable in UTC.
    parsed = datetime.fromisoformat(iso)
    assert parsed.tzinfo is not None


def test_aggregation_report_carries_provenance():
    """Aggregation's render_changelog prepends the provenance block and
    declares itself a reporter stage (no LLM). Also confirms the old
    `_Generated ...` line is gone so the header is single-sourced."""
    from ai_paper_review.aggregation.aggregation import (
        SuggestionAgg, render_changelog,
    )
    suggestions = [
        SuggestionAgg(
            type="strengthen_persona_prompt", target="Methodology Critic",
            support=2, paper_ids=["paper_A", "paper_B"],
            example_misses=["Missed baseline."], extra={},
        ),
    ]
    recs = [{
        "status": "recommended",
        "type": "strengthen_persona_prompt",
        "target": "Methodology Critic",
        "support": 2, "paper_ids": ["paper_A", "paper_B"],
        "example_misses": ["Missed baseline."],
        "recommendation": "Consider appending a bullet.",
    }]
    md = render_changelog(recs, suggestions, min_support=2,
                          launched_at="2026-04-23T02:35:00+00:00")
    assert "<!-- provenance -->" in md
    assert "not applicable (reporter stage)" in md
    assert "2026-04-23T02:35:00+00:00" in md
    # Old free-form generated-on line should no longer appear.
    assert "_Generated " not in md
    # Body of the actual report is still there.
    assert "# Calibration Recommendations Report" in md
    assert "Methodology Critic" in md


def test_validation_report_body_omits_provenance_fields():
    """The validation report body carries only Paper / Venue + the
    analysis sections. LLM provider / model / launch / end / duration
    live in the prepended ``<!-- provenance -->`` block the caller
    wraps around this output — never duplicated inline."""
    from ai_paper_review.validation.reporting import format_report

    actual = {"title": "Test Paper", "venue": "TestConf", "flat_comments": []}
    ai_report = {"flat_comments": [], "selected": []}
    alignment = {"hits": [], "misses": [], "false_alarms": [],
                 "n_actual": 0, "n_ai": 0, "n_strengths": 0}
    metrics = {
        "n_actual": 0, "n_ai": 0, "n_hits": 0, "n_misses": 0,
        "n_false_alarms": 0, "recall": 0.0, "precision": 0.0, "f1": 0.0,
        "severity_weighted_recall": 0.0, "strength_contradictions": [],
    }
    calibration = {
        "summary": {"selection_failures": 0, "prompt_failures": 0,
                    "sub_rating_signals": 0, "uncovered_categories": {}},
        "persona_stats": {}, "miss_attributions": [], "suggestions": [],
    }
    md = format_report(actual, ai_report, alignment, metrics, calibration)
    assert "**LLM:**" not in md
    assert "**Base URL:**" not in md
    assert "**Launched:**" not in md
    assert "**Ended:**" not in md
    # Original header fields still there.
    assert "**Title:** Test Paper" in md
    assert "**Venue:** TestConf" in md


def test_alignment_artifacts_do_not_carry_provenance(tmp_path):
    """Validation's debugging artifacts (the three alignment_*.md
    files) must NOT carry a provenance block — the run metadata banner
    lives on validation_report.md alone. Prepending it on every
    supporting artifact adds noise without helping anyone."""
    import numpy as np
    from ai_paper_review.validation.alignment import _write_batch_artifacts

    actual = [{"id": "H1", "summary": "h"}]
    ai = [{"id": "A1", "summary": "a"}]
    sims = np.array([[0.9]])
    _write_batch_artifacts(
        tmp_path, actual, ai, sims,
        raw_response="0.9", n_parsed=1,
        llm_model="claude-sonnet-4-5-20250929",
    )
    for name in ("alignment_llm_analysis.md",
                 "alignment_similarities.md",
                 "alignment_ranking.md"):
        body = (tmp_path / name).read_text()
        assert "<!-- provenance -->" not in body, (
            f"{name} leaks a provenance block"
        )
        # The artifacts still render their expected content: the
        # analysis file starts with its top-level heading, similarities
        # starts with its, etc.
        assert body.startswith("# "), (
            f"{name} missing top-level heading"
        )


def test_review_outputs_carry_provenance(config_with_openai, tmp_path, monkeypatch):
    """Every .md file the web review runner writes should prepend the
    provenance block. Stubs the pipeline nodes at the web-module level
    so no LLM call or PDF extraction actually runs — we care only that
    the writer prepends provenance to each output file."""
    # config_with_openai already chdir'd into a config-present tmpdir.
    # Import web.app FIRST — its module-level loop re-registers sub-module
    # routes if they're already partially loaded, so if we import
    # web.review directly the /about route ends up registered twice.
    import importlib
    importlib.import_module("ai_paper_review.web.app")
    import ai_paper_review.web.review as web_review

    from ai_paper_review.review.reviewer_db import Reviewer

    def _fake_ingest(state):
        state["paper"] = {"title": "Testing Paper", "abstract": "abs",
                          "full_text": "body"}
        return state

    def _fake_load_db(state):
        state["reviewers"] = [
            Reviewer(id="R001", persona="P", domain="D", focus="", style="",
                     keywords=[], system_prompt=""),
        ]
        return state

    def _fake_select(state):
        state["selected"] = [(state["reviewers"][0], 0.9)]
        state["selection_similarities"] = []
        state.setdefault("n_reviewers", 1)
        return state

    def _fake_run(state, on_progress=None):
        state["raw_reviews"] = [{
            "_reviewer_id": "R001", "_persona": "P", "_domain": "D",
            "comments": [{"summary": "s", "description": "d",
                          "severity": "minor"}],
        }]
        state["all_comments"] = state["raw_reviews"][0]["comments"]
        return state

    def _fake_clarity(state):
        state["clarity_review"] = {
            "_reviewer_id": "G001", "_persona": "Writing Clarity Reviewer",
            "_domain": "Writing",
            "comments": [{"summary": "s", "description": "d"}],
        }
        return state

    def _fake_cluster(state):
        state["clusters"] = []
        state["clustering_similarities"] = {}
        return state

    def _fake_rank(state):
        state["ranked"] = []
        return state

    def _fake_format(state):
        state["report_md"] = "# Review Report\n\nBody.\n"
        return state

    monkeypatch.setattr(web_review, "node_ingest_pdf", _fake_ingest)
    monkeypatch.setattr(web_review, "node_load_db", _fake_load_db)
    monkeypatch.setattr(web_review, "node_select_reviewers", _fake_select)
    monkeypatch.setattr(web_review, "node_run_reviewers", _fake_run)
    monkeypatch.setattr(web_review, "node_run_clarity_review", _fake_clarity)
    monkeypatch.setattr(web_review, "node_cluster_comments", _fake_cluster)
    monkeypatch.setattr(web_review, "node_rank_clusters", _fake_rank)
    monkeypatch.setattr(web_review, "node_format_report", _fake_format)

    # Call _run_review_job directly; no PDF actually touched since ingest
    # is stubbed. Seed the JOBS entry first because _set_job is a no-op
    # when the id isn't already registered.
    from ai_paper_review.web import jobs as jobs_mod
    job_id = "test-job-1"
    with jobs_mod.JOBS_LOCK:
        jobs_mod.JOBS[job_id] = {
            "status": "pending", "filename": "fake.pdf",
            "created_at": "2026-04-23T00:00:00+00:00",
            "updated_at": "2026-04-23T00:00:00+00:00",
            "job_dir": str(tmp_path / "run"),
        }
    job_dir = tmp_path / "run"
    job_dir.mkdir()
    web_review._run_review_job(
        job_id=job_id,
        pdf_path=tmp_path / "fake.pdf",
        job_dir=job_dir,
        provider=None, model=None, db_path=None, n_reviewers=1,
    )
    with jobs_mod.JOBS_LOCK:
        job = jobs_mod.JOBS[job_id]
    assert job["status"] == "done", f"job failed: {job}"

    # Provenance is prepended ONLY to review_report.md. The other
    # artifacts — per-reviewer data + similarity tables + clarity
    # review — don't need the metadata banner repeated on every file.
    report_body = (job_dir / "review_report.md").read_text()
    assert report_body.startswith("<!-- provenance -->"), (
        f"review_report.md missing provenance header. "
        f"First 200 chars: {report_body[:200]!r}"
    )
    assert "openai" in report_body  # config_with_openai fixture sets this
    assert "**Launched:**" in report_body
    assert "**Ended:**" in report_body

    for name in ("review_data.md",
                 "selection_similarities.md", "clustering_similarities.md",
                 "writing_clarity_review.md"):
        body = (job_dir / name).read_text()
        assert "<!-- provenance -->" not in body, (
            f"{name} leaks a provenance block. First 200 chars: {body[:200]!r}"
        )
