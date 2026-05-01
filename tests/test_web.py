"""Web UI smoke tests: routes return 200, provider picker reflects config,
and choosing an unavailable provider produces a clear error flash."""
from __future__ import annotations

import importlib
import io

import pytest


@pytest.fixture
def web_app(config_with_openai):
    """Reload ai_paper_review.web.app under a clean config.yaml pointing to openai."""
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    return ai_paper_review.web.app.app


def test_home_page_renders(web_app):
    """Home is now the About page — goal, disclaimers, three-step workflow.
    The Model page lives at /model, the review launcher at /review."""
    client = web_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode()
    assert "AI Paper Review" in body
    # About page content
    assert "Goal" in body
    assert "Disclaimer" in body
    assert "workflow-steps" in body or "How it works" in body
    # No provider grid on the About page — that lives on /model now
    assert "provider-grid" not in body


def test_review_launcher_renders(web_app):
    """The review-a-paper form now lives at /review (GET)."""
    client = web_app.test_client()
    r = client.get("/review")
    assert r.status_code == 200
    body = r.data.decode()
    assert "AI review" in body
    assert 'name="pdf"' in body
    # Provider grid moved to /model — the launcher should NOT render it.
    assert "provider-grid" not in body


def test_model_page_shows_configured_providers(web_app, monkeypatch):
    """The Model page's provider availability grid marks configured providers
    with the `provider-available` class and unconfigured ones with
    `provider-unavailable`.

    Config has openai; Gemini is set via env. The other six providers
    (anthropic, xai, github, copilot_sdk, claude_sdk, openai_compatible)
    should all be unconfigured.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    # Keep the SDK probes deterministic regardless of whether those
    # packages are installed in this environment.
    from ai_paper_review.llm import probing as _probing
    monkeypatch.setattr(_probing, "_copilot_sdk_installed", lambda: False)
    monkeypatch.setattr(_probing, "_claude_sdk_installed", lambda: False)
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    client = ai_paper_review.web.app.app.test_client()
    body = client.get("/model").data.decode()
    # Provider availability grid: 2 green (openai configured, google via env),
    # 6 red (anthropic, xai, github, copilot_sdk, claude_sdk, openai_compatible).
    assert body.count("provider-available") == 2
    assert body.count("provider-unavailable") == 6
    # The review-provider and validation-provider dropdowns each list all 8
    # providers. Configured ones are marked with ✓, unconfigured with "(no key)".
    # 2 configured providers × 2 dropdowns = 4 ✓ glyphs total.
    assert body.count("✓") == 4
    # The model input is prefilled with the configured value.
    assert 'value="gpt-4o"' in body


def test_reviewer_db_routes(web_app):
    """Legacy /reviewers routes redirect to the per-DB view under the bundled
    default; the new /database/__default__/view URLs serve directly."""
    client = web_app.test_client()
    # Legacy routes 302 to the per-DB equivalents
    assert client.get("/reviewers").status_code == 302
    assert client.get("/reviewers/R001").status_code == 302
    # The new canonical URLs serve directly
    assert client.get("/database/__default__/view").status_code == 200
    assert client.get("/database/__default__/reviewers/R001").status_code == 200
    assert client.get("/database/__default__/reviewers/R999").status_code == 404
    # Following legacy redirects still ends at 200 (two-step)
    assert client.get("/reviewers", follow_redirects=True).status_code == 200
    assert client.get("/reviewers/R001", follow_redirects=True).status_code == 200


def test_validate_form_renders(web_app):
    """Validate form should render; it no longer shows a provider grid
    (the provider is inherited from config.yaml), but it does surface the
    active provider name as read-only info."""
    client = web_app.test_client()
    r = client.get("/validation")
    assert r.status_code == 200
    # Intentional regression: provider grid was removed from /validation.
    assert b"provider-grid" not in r.data
    # The file-upload form and the submit button should still be there.
    assert b'name="actual"' in r.data
    assert b"Run validation" in r.data


def test_missing_api_key_returns_error_flash(tmp_path, monkeypatch):
    """If the configured review provider has no API key, starting a review
    flashes a clear error naming the provider, the api_keys path, and the
    env var fallback. (Previously the provider could be picked via form
    field; now it's config-driven, so this exercises the same backend path
    via a different config.)"""
    monkeypatch.delenv("PAPER_REVIEW_CONFIG", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_REVIEW_MODEL_OVERRIDE", raising=False)
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.chdir(tmp_path)
    # Configure xAI as the review provider but provide no api_keys entry.
    (tmp_path / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: xai_api\n"
        "  model: grok-4\n"
    )
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    client = ai_paper_review.web.app.app.test_client()
    r = client.post(
        "/review",
        data={"pdf": (io.BytesIO(b"%PDF-fake"), "fake.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.data.decode()
    assert "API key missing for review provider" in body
    assert "xai_api" in body
    assert "XAI_API_KEY" in body
    assert "api_keys.xai" in body


def test_unsupported_provider_in_config_rejected(tmp_path, monkeypatch):
    """A config.yaml with an unrecognized provider raises a clear error
    from the YAML loader. (POST /review no longer accepts form overrides,
    so this test exercises the loader's validator rather than a form
    rejection path.)"""
    monkeypatch.delenv("PAPER_REVIEW_CONFIG", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: not-a-real-provider\n"
        "  model: whatever\n"
    )
    from ai_paper_review.llm.config import load_config
    with pytest.raises(ValueError, match="Unsupported review_provider"):
        load_config()


def test_available_provider_starts_job(web_app):
    """Posting a PDF (with the configured provider from the fixture being
    available) kicks off a review and redirects to the status page."""
    with web_app.test_client() as client:
        r = client.post(
            "/review",
            data={
                "pdf": (io.BytesIO(b"%PDF-fake"), "fake.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/review/" in r.headers.get("Location", "")


def test_delete_review_removes_from_registry_and_disk(web_app, tmp_path):
    """POST /review/<id>/delete removes the job and its on-disk files."""
    from ai_paper_review.web import jobs as jobs_mod

    # Seed a fake "done" job whose on-disk directory actually exists
    job_dir = tmp_path / "fake_job"
    job_dir.mkdir()
    (job_dir / "review_report.md").write_text("# test\n")
    (job_dir / "review_data.md").write_text("# data\n")

    job_id = "deadbeef-dead-beef-dead-beefdeadbeef"
    with jobs_mod.JOBS_LOCK:
        jobs_mod.JOBS[job_id] = {
            "status": "done",
            "filename": "test.pdf",
            "job_dir": str(job_dir),
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }

    client = web_app.test_client()
    r = client.post(f"/review/{job_id}/delete", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/review")

    # Registry entry gone
    with jobs_mod.JOBS_LOCK:
        assert job_id not in jobs_mod.JOBS

    # On-disk directory gone
    assert not job_dir.exists()


def test_delete_nonexistent_review_is_harmless(web_app):
    """Deleting a job that doesn't exist just flashes and redirects home."""
    client = web_app.test_client()
    r = client.post("/review/not-a-real-job/delete", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/review")


def test_rehydrate_picks_up_completed_review_on_startup(tmp_path, monkeypatch):
    """Reviews with the full set of artifacts on disk are restored to JOBS
    when the web module is imported (i.e. when the server launches)."""
    # Set up a RUNS_DIR layout that mimics a real completed review
    workdir = tmp_path / "work"
    runs = workdir / "runs"
    runs.mkdir(parents=True)
    (workdir / "uploads").mkdir()

    job_id = "review_20260101_120000_aaa"
    jd = runs / job_id
    jd.mkdir()
    (jd / "review_report.md").write_text("# Review Report\n")
    (jd / "review_data.md").write_text("# Review\n")
    (jd / "_ui_state.json").write_text(
        '{"paper":{"title":"Testing Paper","abstract":""},"selected":[],"ranked_clusters":[{"x":1},{"y":2}]}'
    )

    # Also seed a validation_ dir and an incomplete review dir
    (runs / "validation_20260101_120001_bbb").mkdir()
    (runs / "validation_20260101_120001_bbb" / "validation_report.md").write_text("ignore me")
    incomplete = runs / "review_20260101_120002_ccc"
    incomplete.mkdir()
    (incomplete / "review_report.md").write_text("no ui_state alongside")

    monkeypatch.setenv("PAPER_REVIEW_WORKDIR", str(workdir))

    # Force a fresh import so module-level rehydration runs against our workdir
    import importlib
    import sys
    for mod in [m for m in list(sys.modules) if m.startswith("ai_paper_review.web")]:
        del sys.modules[mod]
    importlib.import_module("ai_paper_review.web.app")  # registers the web package
    from ai_paper_review.web import jobs as fresh_jobs

    assert job_id in fresh_jobs.JOBS
    entry = fresh_jobs.JOBS[job_id]
    assert entry["status"] == "done"
    assert entry["restored"] is True
    assert entry["n_issues"] == 2
    assert entry["paper_title"] == "Testing Paper"

    # Validation dir not loaded into review JOBS
    assert "validation_20260101_120001_bbb" not in fresh_jobs.JOBS
    # Incomplete review rehydrated as error so it can be deleted from the UI
    assert "review_20260101_120002_ccc" in fresh_jobs.JOBS
    assert fresh_jobs.JOBS["review_20260101_120002_ccc"]["status"] == "error"


def test_list_available_databases_includes_default(tmp_path, monkeypatch):
    """list_available_databases() surfaces the bundled default plus any
    user-uploaded .md databases. Seeds a user DB by copying the bundled
    comparch_reviewer_db.md (no generator involved)."""
    import shutil
    import sys
    from importlib.resources import files

    workdir = tmp_path / "work"
    (workdir / "uploads").mkdir(parents=True)
    (workdir / "runs").mkdir()
    (workdir / "databases").mkdir()

    # Seed one user-uploaded DB by copying the bundled default into the
    # databases dir under a different name. No generation code involved.
    bundled = files("ai_paper_review.database").joinpath("comparch_reviewer_db.md")
    shutil.copy(str(bundled), workdir / "databases" / "seeded.md")

    monkeypatch.setenv("PAPER_REVIEW_WORKDIR", str(workdir))
    import importlib
    for mod in [m for m in list(sys.modules) if m.startswith("ai_paper_review.web")]:
        del sys.modules[mod]
    importlib.import_module("ai_paper_review.web.app")  # registers the web package
    from ai_paper_review.web import databases as fresh_dbs

    dbs = fresh_dbs.list_available_databases()
    ids = [d["id"] for d in dbs]

    assert "__default__" in ids
    default = next(d for d in dbs if d["is_default"])
    assert default["can_delete"] is False
    assert default["n_reviewers"] > 100  # full bundled DB

    assert "seeded.md" in ids
    uploaded = next(d for d in dbs if d["id"] == "seeded.md")
    assert uploaded["can_delete"] is True
    assert uploaded["n_reviewers"] > 100  # same content as bundled


def test_n_reviewers_flows_from_form_into_state():
    """The 'Number of reviewers' picker on the Review page must reach the
    pipeline state with clamping. Covers three behaviors at once:
    (1) form field arrives at node_select_reviewers as state['n_reviewers'],
    (2) blank / malformed form values fall back to DEFAULT_N_REVIEWERS,
    (3) out-of-range values are clamped to MIN_N_REVIEWERS..MAX_N_REVIEWERS.
    Without this test, the whole wire-up is one accidental `setdefault` or
    typo away from silently reverting to the old hardcoded count."""
    from ai_paper_review.review.constants import (
        DEFAULT_N_REVIEWERS,
        MAX_N_REVIEWERS,
        MIN_N_REVIEWERS,
    )
    from ai_paper_review.review.review import ReviewState
    from ai_paper_review.review.reviewer_db import Reviewer
    from ai_paper_review.review.selection import node_select_reviewers

    # Stubbed reviewers + paper — enough for node_select_reviewers to run
    # without any LLM or real embedder.
    class _Embedder:
        def embed(self, texts):
            import numpy as np
            # Deterministic, but distinct per text so selection has a total
            # ordering it can act on.
            return np.array([
                [hash(t) % 997 / 997.0 for _ in range(3)]
                for t in texts
            ], dtype=float)

    reviewers = [
        Reviewer(
            id=f"R{i:03d}", persona=f"Persona{i}", domain="TestDomain",
            focus="f", style="s", keywords=[f"kw{i}"], system_prompt="sp",
        )
        for i in range(1, 16)  # more than MAX_N_REVIEWERS so clamp is visible
    ]
    paper = {"title": "t", "abstract": "a", "full_text": "full"}

    def _run(n_in):
        state: ReviewState = {
            "paper": paper, "reviewers": reviewers, "n_reviewers": n_in,
        }
        # node_select_reviewers is a thin wrapper that clamps then calls
        # select_reviewers (which constructs an Embedder under the hood).
        state = node_select_reviewers({**state})
        return state["n_reviewers"], len(state["selected"])

    # 1) In-range value is honored as-is
    got_n, n_selected = _run(5)
    assert got_n == 5, f"expected 5, got {got_n}"
    assert n_selected == 5

    # 2) Default when blank (simulated by leaving key absent)
    state = {"paper": paper, "reviewers": reviewers}
    state = node_select_reviewers(state)
    assert state["n_reviewers"] == DEFAULT_N_REVIEWERS

    # 3) Over-max clamps to MAX
    got_n, _ = _run(50)
    assert got_n == MAX_N_REVIEWERS

    # 4) Under-min clamps to MIN
    got_n, _ = _run(0)
    assert got_n == MIN_N_REVIEWERS
    got_n, _ = _run(-3)
    assert got_n == MIN_N_REVIEWERS


def test_aggregate_view_empty_and_with_deltas(tmp_path, monkeypatch, config_with_openai):
    """GET /aggregation — renders an empty-state page with no validation
    runs; surfaces cross-paper suggestions when two runs agree.

    The seeded deltas are minimal JSON files matching the shape
    aggregate() consumes: ``{paper_id, suggestions: [{type, target_persona,
    rationale, example_misses}]}``. The agreement pair (Methodology
    Critic / strengthen_persona_prompt) should surface as one
    recommendation with support=2; the disagreement entry (Novelty
    Hunter on paper_B only) should land below threshold.
    """
    import json
    import sys

    # Override the web module's WORKDIR so RUNS_DIR lives under tmp_path.
    workdir = tmp_path / "work"
    runs = workdir / "runs"
    runs.mkdir(parents=True)
    (workdir / "uploads").mkdir()
    (workdir / "databases").mkdir()
    monkeypatch.setenv("PAPER_REVIEW_WORKDIR", str(workdir))

    # Re-import under the new WORKDIR.
    for mod in [m for m in list(sys.modules) if m.startswith("ai_paper_review.web")]:
        del sys.modules[mod]
    import ai_paper_review.web.app
    client = ai_paper_review.web.app.app.test_client()

    # --- Empty state ---
    r = client.get("/aggregation")
    assert r.status_code == 200
    body = r.data.decode()
    assert "No validations yet" in body

    # --- Seed two validation runs with one shared suggestion ---
    def _seed(run_name, paper_id, suggestions):
        run_dir = runs / run_name
        run_dir.mkdir()
        (run_dir / "calibration_delta.json").write_text(json.dumps({
            "paper_id": paper_id,
            "suggestions": suggestions,
        }))

    shared = {
        "type": "strengthen_persona_prompt",
        "target_persona": "Methodology Critic",
        "rationale": "Missed baseline-comparison issues.",
        "example_misses": ["Paper omits comparison against Method-A in Table 3."],
    }
    _seed("validation_20260101_120000_aaa", "paper_A", [shared])
    _seed("validation_20260101_120001_bbb", "paper_B", [
        dict(shared, example_misses=["Missing ablation on component X."]),
        {
            "type": "strengthen_persona_prompt",
            "target_persona": "Novelty Hunter",
            "rationale": "Didn't flag incremental contribution.",
            "example_misses": ["No delta vs prior work quantified."],
        },
    ])

    # --- With data: default min_support=2 ---
    r = client.get("/aggregation")
    assert r.status_code == 200
    body = r.data.decode()
    # Shared suggestion should surface as a recommendation
    assert "Methodology Critic" in body
    assert "2 papers" in body
    assert "paper_A" in body and "paper_B" in body
    # Recommendation text produced by recommendation_text() for
    # strengthen_persona_prompt mentions 'priorities' list.
    assert "priorities" in body
    # Novelty Hunter is single-paper → below threshold
    assert "Below threshold" in body
    assert "Novelty Hunter" in body

    # --- min_support=1 promotes the single-paper entry too ---
    r = client.get("/aggregation?min_support=1")
    assert r.status_code == 200
    body = r.data.decode()
    # Both personas appear in the main recommendations section now
    assert body.count("Methodology Critic") >= 1
    assert body.count("Novelty Hunter") >= 1


