"""Web UI smoke tests: routes return 200, provider picker reflects config,
and choosing an unavailable provider produces a clear error flash."""
from __future__ import annotations

import importlib
import io
import json

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

    Config has openai; Gemini is set via env. The other five providers
    (anthropic, xai, copilot_sdk, claude_sdk, openai_compatible)
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
    # 5 red (anthropic, xai, copilot_sdk, claude_sdk, openai_compatible).
    assert body.count("provider-available") == 2
    assert body.count("provider-unavailable") == 5
    # The review-provider and validation-provider dropdowns each list all 7
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


def test_n_reviewers_flows_from_form_into_state(monkeypatch):
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
    from ai_paper_review.review import selection
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

    monkeypatch.setattr(selection, "Embedder", _Embedder)

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




def test_secret_key_is_random_without_flask_secret(config_with_openai, monkeypatch):
    monkeypatch.delenv("FLASK_SECRET", raising=False)
    import ai_paper_review.web.app
    first = importlib.reload(ai_paper_review.web.app).app.secret_key
    second = importlib.reload(ai_paper_review.web.app).app.secret_key
    assert first != second


def test_invalid_config_shows_its_error_not_missing_banner(web_app, isolated_config):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n  provider: github_api\n  model: m\n")
    body = web_app.test_client().get("/").data.decode()
    assert "github_api was removed" in body
    assert "No <code>config.yaml</code> found" not in body


def _seed_done_review(tmp_path, ui_state, job_id="review_20260101_000000_zzz"):
    """Register a finished review in JOBS backed by files in tmp_path."""
    import json
    from ai_paper_review.web import jobs as jobs_mod

    job_dir = tmp_path / job_id
    job_dir.mkdir()
    ui = job_dir / "_ui_state.json"
    ui.write_text(json.dumps({
        "review_name": "r", "paper": {"title": "t", "abstract": ""},
        "selected": [], "ranked_clusters": [], "clarity_review": {},
        **ui_state,
    }))
    with jobs_mod.JOBS_LOCK:
        jobs_mod.JOBS[job_id] = {
            "status": "done", "filename": "p.pdf", "job_dir": str(job_dir),
            "ui_state_json": str(ui),
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
        }
    return job_id, job_dir


def test_result_reviewer_links_use_run_database(web_app, tmp_path, monkeypatch):
    """The worker stores the database id in _ui_state.json and the result
    page links reviewers under that DB; old runs fall back to __default__."""
    import json
    import ai_paper_review.web.review as web_review
    from ai_paper_review.review.reviewer_db import Reviewer
    from ai_paper_review.web import jobs as jobs_mod

    rv = Reviewer(id="R001", persona="P", domain="D", focus="", style="",
                  keywords=[], system_prompt="")

    def _set(**kw):
        def node(state, **_):
            state.update(kw)
            return state
        return node

    monkeypatch.setattr(web_review, "node_ingest_pdf",
                        _set(paper={"title": "T", "abstract": "a", "full_text": "b"}))
    monkeypatch.setattr(web_review, "node_load_db", _set(reviewers=[rv]))
    monkeypatch.setattr(web_review, "node_select_reviewers",
                        _set(selected=[(rv, 0.9)], selection_similarities=[]))
    monkeypatch.setattr(web_review, "node_run_clarity_review", _set(clarity_review={}))
    monkeypatch.setattr(web_review, "node_run_reviewers",
                        _set(raw_reviews=[], all_comments=[]))
    monkeypatch.setattr(web_review, "node_cluster_comments", _set(clustering_similarities={}))
    monkeypatch.setattr(web_review, "node_rank_clusters", _set(ranked=[]))
    monkeypatch.setattr(web_review, "node_format_report", _set(report_md="# R\n"))

    job_id = "review_20260101_000000_db"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    with jobs_mod.JOBS_LOCK:
        jobs_mod.JOBS[job_id] = {"status": "queued", "filename": "p.pdf",
                                 "job_dir": str(job_dir),
                                 "created_at": "x", "updated_at": "x"}
    web_review._run_review_job(job_id, tmp_path / "p.pdf", job_dir,
                               database_id="__bundled__:mlai_reviewer_db.md")
    assert jobs_mod.JOBS[job_id]["status"] == "done", jobs_mod.JOBS[job_id]
    ui = json.loads((job_dir / "_ui_state.json").read_text())
    assert ui["database_id"] == "__bundled__:mlai_reviewer_db.md"

    client = web_app.test_client()
    body = client.get(f"/review/{job_id}/result").data.decode()
    assert "/database/__bundled__:mlai_reviewer_db.md/reviewers/R001" in body

    old_id, _ = _seed_done_review(
        tmp_path, {"selected": [{"id": "R002", "domain": "D", "persona": "P", "score": 0.5}]})
    body = client.get(f"/review/{old_id}/result").data.decode()
    assert "/database/__default__/reviewers/R002" in body


def test_n_reviewers_capped_by_distinct_personas(web_app, monkeypatch):
    """Selection picks one reviewer per persona, so both the form cap and
    the submit check use the distinct-persona count, not the row count."""
    import ai_paper_review.web.review as web_review
    from ai_paper_review.review.reviewer_db import Reviewer

    client = web_app.test_client()
    body = client.get("/review").data.decode()
    assert 'data-n-reviewers="20"' in body  # 200 reviewers, 20 personas
    assert 'data-n-reviewers="200"' not in body

    fake = [Reviewer(id=f"R{i}", persona=f"P{i % 2}", domain="D", focus="",
                     style="", keywords=[], system_prompt="") for i in range(5)]
    monkeypatch.setattr(web_review, "parse_reviewer_db", lambda _p: fake)
    r = client.post(
        "/review",
        data={"pdf": (io.BytesIO(b"%PDF-fake"), "fake.pdf"), "n_reviewers": "3"},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "contains only 2 distinct reviewer personas" in r.data.decode()


def test_result_alternatives_exclude_representative(web_app, tmp_path):
    """After json.loads the representative is an equal copy, not the same
    object, so it must be excluded from the alternatives by value."""
    rep = {"_reviewer_id": "R001", "_persona": "P", "summary": "s1",
           "description": "d1", "severity": "major"}
    other = {"_reviewer_id": "R002", "_persona": "Q", "summary": "s2",
             "description": "d2", "severity": "minor"}
    job_id, _ = _seed_done_review(tmp_path, {"ranked_clusters": [{
        "rank": 1, "score": 3, "size": 2, "num_distinct_reviewers": 2,
        "representative": rep, "members": [rep, other],
    }]})
    body = web_app.test_client().get(f"/review/{job_id}/result").data.decode()
    assert body.count('class="alt-phrasing-head"') == 1
    assert "d2" in body


def test_bundled_database_view_renders(web_app):
    """__bundled__:<file> ids resolve for the view and reviewer pages."""
    client = web_app.test_client()
    r = client.get("/database/__bundled__:mlai_reviewer_db.md/view")
    assert r.status_code == 200
    from ai_paper_review import bundled_db_dir
    from ai_paper_review.review.reviewer_db import parse_reviewer_db
    rid = parse_reviewer_db(str(bundled_db_dir() / "mlai_reviewer_db.md"))[0].id
    r = client.get(f"/database/__bundled__:mlai_reviewer_db.md/reviewers/{rid}")
    assert r.status_code == 200


def test_delete_refuses_running_review(web_app, tmp_path):
    """A review that is still running is neither unregistered nor removed."""
    from ai_paper_review.web import jobs as jobs_mod

    job_id, job_dir = _seed_done_review(tmp_path, {})
    with jobs_mod.JOBS_LOCK:
        jobs_mod.JOBS[job_id]["status"] = "reviewing"
    r = web_app.test_client().post(f"/review/{job_id}/delete",
                                   follow_redirects=True)
    assert "still running" in r.data.decode()
    with jobs_mod.JOBS_LOCK:
        assert job_id in jobs_mod.JOBS
    assert job_dir.exists()


def test_rehydrate_corrupt_ui_state_is_error(tmp_path, monkeypatch, config_with_openai):
    """A run dir whose _ui_state.json doesn't parse is restored as an error,
    so its result page redirects instead of returning 500."""
    import sys
    workdir = tmp_path / "work"
    jd = workdir / "runs" / "review_20260101_120000_bad"
    jd.mkdir(parents=True)
    (jd / "review_report.md").write_text("# Review Report\n")
    (jd / "review_data.md").write_text("# Review\n")
    (jd / "_ui_state.json").write_text("{not json")
    monkeypatch.setenv("PAPER_REVIEW_WORKDIR", str(workdir))
    for mod in [m for m in list(sys.modules) if m.startswith("ai_paper_review.web")]:
        del sys.modules[mod]
    fresh_app = importlib.import_module("ai_paper_review.web.app")
    from ai_paper_review.web import jobs as fresh_jobs

    assert fresh_jobs.JOBS[jd.name]["status"] == "error"
    r = fresh_app.app.test_client().get(f"/review/{jd.name}/result")
    assert r.status_code == 302


def test_validation_uploads_same_name_do_not_collide(web_app, monkeypatch):
    """Human and AI uploads with the same filename are saved side by side
    (``human__`` / ``ai__`` prefixes), so the AI file can't overwrite the
    human one."""
    from ai_paper_review.web import validation as v

    class _NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(v.threading, "Thread", _NoThread)
    client = web_app.test_client()
    r = client.post("/validation", data={
        "actual": (io.BytesIO(b"HUMAN"), "reviews.md"),
        "ai_review": (io.BytesIO(b"AI"), "reviews.md"),
    }, content_type="multipart/form-data")
    assert r.status_code == 302
    run_id = r.headers["Location"].split("/validation/")[1].split("/")[0]
    run_dir = v.RUNS_DIR / run_id
    assert (run_dir / "human__reviews.md").read_text() == "HUMAN"
    assert (run_dir / "ai__reviews.md").read_text() == "AI"
    files = v.list_run_files(run_dir)
    assert {f["name"] for f in files["inputs"]} == {
        "human__reviews.md", "ai__reviews.md",
    }


def test_model_apply_drops_stale_base_url_on_provider_switch(
        isolated_config, monkeypatch):
    """The prefilled base URL of the old provider is not carried over as
    an explicit override when the provider changes."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: llama3\n"
        "  base_url: http://localhost:11434/v1\n"
        "llm_validation:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://localhost:1234/v1\n"
        "api_keys:\n"
        "  anthropic_api: sk-ant-test\n"
    )
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    client = ai_paper_review.web.app.app.test_client()
    r = client.post("/model", data={
        "action": "apply",
        "review_provider": "anthropic_api",
        "review_model": "claude-x",
        "review_base_url": "http://localhost:11434/v1",
        "validation_provider": "anthropic_api",
        "validation_base_url": "http://localhost:1234/v1",
    })
    assert r.status_code == 302
    from ai_paper_review.llm.config import load_config
    cfg = load_config()
    assert cfg.review_provider == "anthropic_api"
    assert cfg.resolve_base_url_for_stage("review") is None
    assert cfg.resolve_base_url_for_stage("validation") is None


def test_model_page_usable_when_config_fails_to_load(isolated_config):
    """A config load_config rejects still yields provider options and a
    Reset button that bypasses browser validation."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: github_api\n"
        "  model: gpt-4o\n"
    )
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    client = ai_paper_review.web.app.app.test_client()
    r = client.get("/model")
    assert r.status_code == 200
    body = r.data.decode()
    assert '<option value="anthropic_api"' in body
    assert "formnovalidate" in body


def test_model_rejects_malformed_base_url(web_app, monkeypatch):
    """A malformed base URL is refused on POST, and one that slips in via
    env still produces a visible flash on GET."""
    import os
    client = web_app.test_client()
    r = client.post("/model", data={
        "action": "apply",
        "review_provider": "openai_compatible_api",
        "review_model": "llama3",
        "review_base_url": "http://[bad/v1",
    }, follow_redirects=True)
    assert "Invalid review base url" in r.data.decode()
    assert "PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE" not in os.environ

    monkeypatch.setenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", "openai_compatible_api")
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE", "http://[bad/v1")
    r = client.get("/model")
    assert "Could not probe providers" in r.data.decode()


def test_validation_download_rejects_non_validation_dir(web_app):
    from ai_paper_review.web import validation as v
    d = v.RUNS_DIR / "review_20260101_000000_abc"
    d.mkdir(parents=True)
    (d / "validation_report.md").write_text("x")
    r = web_app.test_client().get(
        "/validation/review_20260101_000000_abc/download/validation_report.md")
    assert r.status_code == 404


def test_validation_delete_refuses_running_job(web_app):
    from ai_paper_review.web import validation as v
    run_id = "validation_20260101_000000_run"
    d = v.RUNS_DIR / run_id
    d.mkdir(parents=True)
    with v.VALIDATE_JOBS_LOCK:
        v.VALIDATE_JOBS[run_id] = {"status": "aligning"}
    try:
        r = web_app.test_client().post(f"/validation/{run_id}/delete",
                                       follow_redirects=True)
        assert d.exists()
        assert "still running" in r.data.decode()
    finally:
        with v.VALIDATE_JOBS_LOCK:
            v.VALIDATE_JOBS.pop(run_id, None)

    # On-disk only: stored status decides.
    (d / "_ui_state.json").write_text(json.dumps({"status": "error"}))
    web_app.test_client().post(f"/validation/{run_id}/delete")
    assert not d.exists()


def test_validation_result_redirects_on_error_state(web_app):
    from ai_paper_review.web import validation as v
    run_id = "validation_20260101_000000_err"
    d = v.RUNS_DIR / run_id
    d.mkdir(parents=True)
    (d / "_ui_state.json").write_text(json.dumps({
        "status": "error", "error": "RuntimeError: boom",
    }))
    client = web_app.test_client()
    r = client.get(f"/validation/{run_id}/result")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/validation")
    r = client.get("/validation")
    assert "RuntimeError: boom" in r.data.decode()


def test_validation_status_poll_handles_404(web_app):
    """The status page's poll loop keeps going on non-OK responses and
    leaves the page on 404 instead of stopping silently."""
    from ai_paper_review.web import validation as v
    run_id = "validation_20260101_000000_pol"
    with v.VALIDATE_JOBS_LOCK:
        v.VALIDATE_JOBS[run_id] = {"status": "aligning", "message": "",
                                  "created_at": "2026-01-01T00:00:00",
                                  "updated_at": "2026-01-01T00:00:00"}
    try:
        body = web_app.test_client().get(
            f"/validation/{run_id}/status").data.decode()
    finally:
        with v.VALIDATE_JOBS_LOCK:
            v.VALIDATE_JOBS.pop(run_id, None)
    assert "if (!r.ok) return;" not in body
    assert "r.status === 404" in body


class _NoThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


def _stub_validate_pipeline(monkeypatch, v):
    """Stub the LLM and scoring steps of the validation worker; record
    what they were called with."""
    seen = {}

    def _extract(raw, vocab, provider_override=None, model_override=None,
                 run_dir=None, llm_config=None):
        seen["extract"] = (provider_override, model_override)
        seen["extract_cfg"] = llm_config
        return {"actual_reviews": []}

    def _make_client(cfg, use_case="default"):
        seen["client_cfg"] = cfg
        return object()

    def _calibration(alignment, ai_report, reviewers, tables, **_):
        seen["reviewers"], seen["tables"] = reviewers, tables
        return {"summary": {}, "persona_stats": {}, "miss_attributions": [],
                "suggestions": []}

    monkeypatch.setattr(v.cr, "llm_extract", _extract)
    monkeypatch.setattr(v.cr, "normalize_extracted", lambda e, _vocab: e)
    monkeypatch.setattr(v, "make_client", _make_client)
    monkeypatch.setattr(v, "load_actual", lambda _p: {"flat_comments": []})
    monkeypatch.setattr(v, "load_ai", lambda _p: {"flat_comments": [], "title": "T"})
    monkeypatch.setattr(v, "align_comments", lambda *a, **k: {})
    monkeypatch.setattr(v, "compute_metrics", lambda _a: {})
    monkeypatch.setattr(v, "build_calibration", _calibration)
    monkeypatch.setattr(v, "format_report", lambda *a, **k: "# R\n")
    return seen


def test_key_precheck_resolves_stage_base_url(isolated_config, monkeypatch, tmp_path):
    """The missing-key pre-check judges locality by the stage's own base
    URL, the one make_client will use."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "  base_url: http://api.example.com/v1\n"
        "llm_validation:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://localhost:1234/v1\n"
    )
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    from ai_paper_review.web import validation as v

    def _reached(*a, **k):
        raise RuntimeError("reached llm_extract")

    monkeypatch.setattr(v.cr, "llm_extract", _reached)
    run_id = "validation_20260101_000000_w1"
    v.VALIDATE_JOBS[run_id] = {}
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    human = run_dir / "human__r.txt"
    human.write_text("plain review text")
    v._run_validate_job(run_id, run_dir, human, "", None, "r.txt")
    assert "reached llm_extract" in v.VALIDATE_JOBS[run_id]["message"]

    # Review stage has no base URL of its own: not local, so the key is required.
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "llm_validation:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://localhost:1234/v1\n"
    )
    import ai_paper_review.web.review as web_review
    monkeypatch.setattr(web_review.threading, "Thread", _NoThread)
    r = ai_paper_review.web.app.app.test_client().post(
        "/review", data={"pdf": (io.BytesIO(b"%PDF-fake"), "fake.pdf")},
        content_type="multipart/form-data", follow_redirects=True)
    assert "API key missing for review provider" in r.data.decode()


def test_review_key_precheck_resolves_stage_base_url(isolated_config, monkeypatch):
    """The review missing-key pre-check looks up the key for the review
    stage's own base URL, not the validation stage's."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "llm_validation:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://api.example.com/v1\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    # The unknown database stops the request right after the pre-check.
    r = ai_paper_review.web.app.app.test_client().post(
        "/review",
        data={"pdf": (io.BytesIO(b"%PDF-fake"), "fake.pdf"), "database": "missing.md"},
        content_type="multipart/form-data", follow_redirects=True)
    body = r.data.decode()
    assert "API key missing" not in body
    assert "Database selection invalid" in body


def test_review_run_uses_submit_time_config(isolated_config, monkeypatch, tmp_path):
    """The review worker runs on the config loaded at submit; a Model-page
    change mid-run does not alter the recorded base URL."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "  base_url: http://localhost:1111/v1\n"
    )
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    import ai_paper_review.web.review as web_review
    from ai_paper_review.llm.config import load_config
    from ai_paper_review.web import jobs as jobs_mod

    seen = {}

    def _set(**kw):
        def node(state, **_):
            seen["llm_config"] = state.get("llm_config")
            state.update(kw)
            return state
        return node

    for name, kw in [
        ("node_ingest_pdf", {"paper": {"title": "T", "abstract": "a"}}),
        ("node_load_db", {"reviewers": []}),
        ("node_select_reviewers", {"selected": [], "selection_similarities": []}),
        ("node_run_clarity_review", {"clarity_review": {}}),
        ("node_run_reviewers", {"raw_reviews": [], "all_comments": []}),
        ("node_cluster_comments", {"clustering_similarities": {}}),
        ("node_rank_clusters", {"ranked": []}),
        ("node_format_report", {"report_md": "# R\n"}),
    ]:
        monkeypatch.setattr(web_review, name, _set(**kw))

    cfg = load_config()
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE", "http://localhost:2222/v1")
    job_id = "review_20260101_000000_w2"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    with jobs_mod.JOBS_LOCK:
        jobs_mod.JOBS[job_id] = {"status": "queued", "job_dir": str(job_dir),
                                 "created_at": "x", "updated_at": "x"}
    web_review._run_review_job(job_id, tmp_path / "p.pdf", job_dir,
                               "openai_compatible_api", "m", llm_config=cfg)
    assert jobs_mod.JOBS[job_id]["status"] == "done", jobs_mod.JOBS[job_id]
    ui = json.loads((job_dir / "_ui_state.json").read_text())
    assert ui["llm_base_url"] == "http://localhost:1111/v1"
    assert seen["llm_config"] is cfg


def test_validation_run_uses_one_config(web_app, monkeypatch, tmp_path):
    """Conversion, alignment and provenance all use the config passed in
    at submit, even if the Model page changes it mid-run."""
    from ai_paper_review.web import validation as v
    from ai_paper_review.llm.config import load_config

    seen = _stub_validate_pipeline(monkeypatch, v)
    cfg = load_config()
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_MODEL_OVERRIDE", "gpt-drift")
    run_id = "validation_20260101_000000_w2v"
    v.VALIDATE_JOBS[run_id] = {}
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    human = run_dir / "human__r.txt"
    human.write_text("plain review text")
    ai = run_dir / "ai__a.md"
    ai.write_text("# AI\n")
    v._run_validate_job(run_id, run_dir, human, "", ai, "r.txt",
                        llm_config=cfg)
    assert v.VALIDATE_JOBS[run_id]["status"] == "done", v.VALIDATE_JOBS[run_id]
    assert seen["extract"] == ("openai_api", "gpt-4o")
    assert seen["extract_cfg"] is cfg
    assert seen["client_cfg"] is cfg
    ui = json.loads((run_dir / "_ui_state.json").read_text())
    assert ui["llm_model"] == "gpt-4o"


def test_validation_uses_review_job_database(web_app, monkeypatch, tmp_path):
    """Validating a prior review job calibrates against that job's reviewer
    database; an uploaded AI review keeps the default one."""
    from ai_paper_review import bundled_db_dir
    from ai_paper_review.review.reviewer_db import parse_reviewer_database
    from ai_paper_review.web import jobs as jobs_mod
    from ai_paper_review.web import validation as v

    seen = _stub_validate_pipeline(monkeypatch, v)
    job_id, job_dir = _seed_done_review(
        tmp_path, {"database_id": "__bundled__:mlai_reviewer_db.md"})
    (job_dir / "review_data.md").write_text("# AI\n")
    with jobs_mod.JOBS_LOCK:
        jobs_mod.JOBS[job_id]["review_data_md"] = str(job_dir / "review_data.md")

    run_id = "validation_20260101_000000_w3"
    v.VALIDATE_JOBS[run_id] = {}
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    human = run_dir / "human__r.md"
    human.write_text("## Comment 1\nx\n")
    v._run_validate_job(run_id, run_dir, human, job_id, None, "r.md")
    assert v.VALIDATE_JOBS[run_id]["status"] == "done", v.VALIDATE_JOBS[run_id]
    mlai = parse_reviewer_database(str(bundled_db_dir() / "mlai_reviewer_db.md"))
    assert [r.id for r in seen["reviewers"]] == [r.id for r in mlai.reviewers]
    assert seen["tables"] == mlai.tables
    assert seen["tables"] != v.DEFAULT_DB_TABLES

    ai = run_dir / "ai__a.md"
    ai.write_text("# AI\n")
    v._run_validate_job(run_id, run_dir, human, "", ai, "r.md")
    assert seen["tables"] is v.DEFAULT_DB_TABLES


def test_no_key_flash_omits_env_clause_without_env_vars(isolated_config, monkeypatch,
                                                        tmp_path):
    """openai_compatible_api on a non-OpenAI host has no key env var, so the
    flash points at config.yaml only instead of listing "(none)"."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "  base_url: http://api.example.com/v1\n"
    )
    import ai_paper_review.web.app
    importlib.reload(ai_paper_review.web.app)
    r = ai_paper_review.web.app.app.test_client().post(
        "/review", data={"pdf": (io.BytesIO(b"%PDF-fake"), "fake.pdf")},
        content_type="multipart/form-data", follow_redirects=True)
    body = r.data.decode()
    assert "API key missing for review provider" in body
    assert "api_keys.openai_compatible_api" in body
    assert "(none)" not in body
    assert "env vars" not in body

    # Validation stage: its own non-OpenAI URL decides, not review's
    # OpenAI URL (whose OPENAI_API_KEY must not be picked up).
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "  base_url: https://api.openai.com/v1\n"
        "llm_validation:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://api.example.com/v1\n"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    from ai_paper_review.web import validation as v
    monkeypatch.setattr(v.cr, "llm_extract", lambda *a, **k: 1 / 0)
    run_id = "validation_20260101_000000_w5"
    v.VALIDATE_JOBS[run_id] = {}
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    human = run_dir / "human__r.txt"
    human.write_text("plain review text")
    v._run_validate_job(run_id, run_dir, human, "", None, "r.txt")
    msg = v.VALIDATE_JOBS[run_id]["message"]
    assert "API key missing for validation provider" in msg
    assert "env vars" not in msg


def test_launcher_db_limit_error_says_personas(web_app):
    body = web_app.test_client().get("/review").data.decode()
    assert "' distinct reviewer persona'" in body


def test_review_submit_with_bad_config_redirects_with_message(web_app, monkeypatch):
    import ai_paper_review.web.app as web_app_mod
    import ai_paper_review.web.review as web_review

    def bad_config():
        raise ValueError("bad value")

    monkeypatch.setattr(web_review, "load_config", bad_config)
    monkeypatch.setattr(web_app_mod, "load_config", bad_config)
    client = web_app.test_client()
    r = client.post(
        "/review", data={"pdf": (io.BytesIO(b"%PDF-fake"), "fake.pdf")},
        content_type="multipart/form-data")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/review")
    assert client.get(r.headers["Location"]).data.decode().count("bad value") == 1


def test_validation_submit_with_bad_config_redirects_with_message(
        web_app, monkeypatch, tmp_path):
    import ai_paper_review.web.app as web_app_mod
    from ai_paper_review.web import validation as v

    def bad_config():
        raise ValueError("bad value")

    monkeypatch.setattr(v, "load_config", bad_config)
    monkeypatch.setattr(web_app_mod, "load_config", bad_config)
    monkeypatch.setattr(v, "RUNS_DIR", tmp_path / "runs")
    jobs_before = set(v.VALIDATE_JOBS)
    client = web_app.test_client()
    r = client.post("/validation", data={
        "actual": (io.BytesIO(b"HUMAN"), "human.md"),
        "ai_review": (io.BytesIO(b"AI"), "ai.md"),
    }, content_type="multipart/form-data")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/validation")
    assert set(v.VALIDATE_JOBS) == jobs_before
    assert not (tmp_path / "runs").exists()
    assert client.get(r.headers["Location"]).data.decode().count("bad value") == 1
