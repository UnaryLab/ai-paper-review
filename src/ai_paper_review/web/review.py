"""Paper-review pipeline: launcher form, submit handler, status / poll
/ result / download / delete routes, and the threaded worker that
runs the pipeline.
"""
from __future__ import annotations

import json
import shutil
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from ai_paper_review.llm.config import load_config
from ai_paper_review.llm.utils import env_vars_for, is_local_provider
from ai_paper_review.review.clarity import node_run_clarity_review
from ai_paper_review.review.clustering import node_cluster_comments
from ai_paper_review.review.constants import (
    DEFAULT_N_REVIEWERS,
    MAX_N_REVIEWERS,
    MIN_N_REVIEWERS,
    RECOMMENDED_MAX_N_REVIEWERS,
    RECOMMENDED_MIN_N_REVIEWERS,
)
from ai_paper_review.review.parsing import review_dict_to_markdown
from ai_paper_review.review.ranking import node_format_report, node_rank_clusters
from ai_paper_review.review.reviewer_db import parse_reviewer_db
from ai_paper_review.review.reviewer_dispatching import node_run_reviewers
from ai_paper_review.review.review import ReviewState, node_ingest_pdf
from ai_paper_review.review.selection import node_load_db, node_select_reviewers
from ai_paper_review.llm.probing import describe_config, probe_providers

from .app import DEFAULT_DB_PATH, RUNS_DIR, app, logger
from .databases import REVIEWERS, list_available_databases, resolve_database_path
from .jobs import JOBS, JOBS_LOCK, _run_name, _safe_upload_name, _set_job, _timestamped_run_id
from .run_files import list_run_files


def _run_review_job(
    job_id: str, pdf_path: Path, job_dir: Path,
    provider: str | None = None, model: str | None = None,
    db_path: str | None = None,
    n_reviewers: int = DEFAULT_N_REVIEWERS,
):
    """Blocking worker run in a daemon thread."""
    from ai_paper_review.provenance import now_iso
    launched_at = now_iso()
    try:
        _set_job(job_id, status="ingesting", message="Extracting PDF text")
        state: ReviewState = {
            "pdf_path": str(pdf_path),
            "db_path": db_path or str(DEFAULT_DB_PATH),
            "n_reviewers": int(n_reviewers),
            "launched_at": launched_at,
        }
        if provider:
            state["llm_provider"] = provider
        if model:
            state["llm_model"] = model

        state = node_ingest_pdf(state)
        _set_job(job_id, message=f"Parsed: {state['paper']['title'][:90]}",
                 paper_title=state["paper"]["title"])

        _set_job(job_id, status="selecting",
                 message=f"Matching top-{n_reviewers} reviewers by topic")
        state = node_load_db(state)
        state = node_select_reviewers(state)

        completed_reviewers: list = []
        dispatched_reviewers: list = []

        def _on_progress(info: dict):
            if info["event"] == "dispatched":
                dispatched_reviewers.append({
                    "reviewer_id": info["reviewer_id"],
                    "persona": info["persona"],
                })
                _set_job(
                    job_id,
                    status="reviewing",
                    message=f"Dispatched {info['current']}/{info['total']}: "
                            f"{info['persona']} ({info['reviewer_id']})",
                    review_progress={
                        "current": len(completed_reviewers),
                        "total": info["total"],
                        "dispatched": len(dispatched_reviewers),
                        "event": "dispatched",
                    },
                    completed_reviewers=list(completed_reviewers),
                    dispatched_reviewers=list(dispatched_reviewers),
                )
            elif info["event"] == "done":
                completed_reviewers.append({
                    "reviewer_id": info["reviewer_id"],
                    "persona": info["persona"],
                    "n_comments": info["n_comments"],
                })
                _set_job(
                    job_id,
                    message=f"Completed {len(completed_reviewers)}/{info['total']}: "
                            f"{info['persona']} — {info['n_comments']} comments",
                    review_progress={
                        "current": len(completed_reviewers),
                        "total": info["total"],
                        "dispatched": len(dispatched_reviewers),
                        "event": "done",
                        "n_comments": info["n_comments"],
                    },
                    completed_reviewers=list(completed_reviewers),
                    dispatched_reviewers=list(dispatched_reviewers),
                )

        # Clarity runs FIRST, before the parallel persona reviewers —
        # its single sequential LLM call seeds the provider's prompt
        # cache with the shared (system + PDF) prefix so the N persona
        # reviewers that dispatch next all hit the warm cache.
        # Mirrors :func:`ai_paper_review.review.review.build_graph`'s
        # ordering; the web runner wires nodes manually rather than
        # invoking the graph, so the order has to be maintained in
        # both places.
        _set_job(job_id, status="clarity",
                 message="Running always-on writing clarity reviewer")
        state = node_run_clarity_review(state)

        state = node_run_reviewers(state, on_progress=_on_progress)

        _set_job(job_id, status="clustering", message="Clustering comments")
        state = node_cluster_comments(state)
        state = node_rank_clusters(state)
        state = node_format_report(state)

        # Build a provenance block once and prepend it to every file.
        # Covers LLM provider/model/base_url, launch/end timestamps, and
        # the format-fix-retries tally so each artifact is
        # self-describing when downloaded or inspected later.
        # ``ended_at`` / ``n_format_repairs`` / ``n_reviewers_total``
        # were captured inside ``node_format_report`` and stashed in
        # state — reusing them here keeps the prepended block in sync
        # with whatever the writer sees.
        from ai_paper_review.provenance import format_provenance
        _cfg = load_config()
        _active_provider = state.get("llm_provider") or _cfg.review_provider
        _active_model = state.get("llm_model") or _cfg.review_model
        _active_base_url = _cfg.resolve_base_url(_active_provider)
        review_name = _run_name(
            pdf_path.name, _active_provider, _active_model, launched_at,
        )
        provenance = format_provenance(
            provider=_active_provider,
            model=_active_model,
            base_url=_active_base_url,
            launched_at=launched_at,
            ended_at=state.get("ended_at"),
            format_fix_retries=state.get("n_format_repairs"),
            n_reviewers_total=state.get("n_reviewers_total"),
        )

        # Provenance is prepended ONLY to the main review_report.md —
        # the similarities tables and per-reviewer data don't need the
        # same metadata banner repeated on every file.
        report_md = job_dir / "review_report.md"
        report_md.write_text(provenance + state["report_md"])

        from ai_paper_review.review.selection import format_selection_similarities_md
        selection_similarities_md = job_dir / "selection_similarities.md"
        selection_similarities_md.write_text(
            format_selection_similarities_md(
                state["paper"],
                state.get("selection_similarities", []),
                state["selected"],
                n_requested=state.get("n_reviewers", len(state["selected"])),
            )
        )

        from ai_paper_review.review.clustering import format_clustering_similarities_md
        clustering_similarities_md = job_dir / "clustering_similarities.md"
        clustering_similarities_md.write_text(
            format_clustering_similarities_md(
                state["paper"],
                state.get("all_comments", []),
                state.get("clustering_similarities", {}),
            )
        )

        # Per-reviewer markdown — input format the validator consumes.
        raw_md_lines = [
            "# AI Review Output",
            "",
            f"**Paper ID:** {review_name}",
            f"**Title:** {state['paper']['title']}",
            "",
        ]
        for rv in state["raw_reviews"]:
            raw_md_lines.append("---")
            raw_md_lines.append("")
            # Use the authoritative ``_*`` fields (from the reviewer DB)
            # rather than whatever the LLM emitted. Avoid ``setdefault``
            # here — the LLM output usually has an empty ``reviewer_id``
            # already, which would block the DB-sourced value and make
            # the serialized markdown look unowned (breaking per-persona
            # stats downstream).
            rv_copy = dict(rv)
            rv_copy["reviewer_id"] = (
                rv.get("_reviewer_id") or rv.get("reviewer_id") or ""
            )
            rv_copy["persona"] = rv.get("_persona") or rv.get("persona") or ""
            rv_copy["domain"]  = rv.get("_domain")  or rv.get("domain")  or ""
            raw_md_lines.append(review_dict_to_markdown(rv_copy))
        review_data_md = job_dir / "review_data.md"
        review_data_md.write_text("\n".join(raw_md_lines))

        # Writing-clarity reviewer output — its own file, intentionally
        # not merged into review_data.md so the validation flow ignores it.
        clarity_review = state.get("clarity_review") or {}
        clarity_md_path = job_dir / "writing_clarity_review.md"
        clarity_copy = dict(clarity_review)
        clarity_copy["reviewer_id"] = clarity_review.get("_reviewer_id", "")
        clarity_copy["persona"] = clarity_review.get("_persona", "")
        clarity_copy["domain"] = clarity_review.get("_domain", "")
        clarity_md_path.write_text(review_dict_to_markdown(clarity_copy))

        # ``node_format_report`` already tallied the reviewers (incl.
        # the clarity reviewer) whose final output had to be rescued by
        # a markdown-repair pass and stashed the counts in state.
        _n_format_repairs = state.get("n_format_repairs", 0)
        _n_reviewers_total = state.get("n_reviewers_total", 0)

        _ui_state = {
            "review_name": review_name,
            "paper": {"title": state["paper"]["title"], "abstract": state["paper"]["abstract"]},
            "selected": [
                {"id": r.id, "domain": r.domain, "persona": r.persona, "score": float(s)}
                for r, s in state["selected"]
            ],
            "ranked_clusters": state["ranked"],
            "clarity_review": clarity_review,
            "n_format_repairs": _n_format_repairs,
            "n_reviewers_total": _n_reviewers_total,
            # LLM + timing provenance — shown on the result page so a
            # reader can tell which model produced the review and how
            # long the run took without opening the markdown. Mirrors
            # the validation result page.
            "llm_provider": _active_provider,
            "llm_model": _active_model,
            "llm_base_url": str(_active_base_url or ""),
            "launched_at": launched_at,
            "ended_at": state.get("ended_at"),
        }
        (job_dir / "_ui_state.json").write_text(json.dumps(_ui_state, indent=2, default=str))

        _set_job(job_id, status="done",
                 message=f"Review complete: {len(state['ranked'])} ranked issues",
                 report_md=str(report_md),
                 review_data_md=str(review_data_md),
                 ui_state_json=str(job_dir / "_ui_state.json"),
                 n_issues=len(state["ranked"]))
    except Exception as e:
        logger.exception("Review job %s failed", job_id)
        _set_job(job_id, status="error", message=f"{type(e).__name__}: {e}",
                 traceback=traceback.format_exc())
        # Write a minimal _ui_state.json even on error so the run directory
        # is discoverable after a server restart and can be deleted from the UI.
        try:
            _locs = locals()
            _err_state = {
                "review_name": _locs.get("review_name") or job_id,
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "paper": {"title": "", "abstract": ""},
                "selected": [],
                "ranked_clusters": [],
                "clarity_review": {},
                "n_format_repairs": 0,
                "n_reviewers_total": 0,
                "llm_provider": _locs.get("_active_provider") or provider or "",
                "llm_model": _locs.get("_active_model") or model or "",
                "llm_base_url": str(_locs.get("_active_base_url") or ""),
                "launched_at": launched_at,
                "ended_at": "",
            }
            (job_dir / "_ui_state.json").write_text(
                json.dumps(_err_state, indent=2, default=str)
            )
        except Exception as write_err:
            logger.warning("Could not write error _ui_state.json for %s: %s",
                           job_id, write_err)


@app.route("/")
@app.route("/about")
def about():
    """Landing page: goal, disclaimers, three-step workflow."""
    return render_template("about.html")


@app.get("/review")
def review_launcher():
    """Review-a-paper form."""
    domains = sorted({r.domain for r in REVIEWERS})
    personas = sorted({r.persona for r in REVIEWERS})
    with JOBS_LOCK:
        recent = sorted(
            ({"id": k, **v, "id_short": k} for k, v in JOBS.items()),
            key=lambda j: j.get("started_at", ""),
            reverse=True,
        )
    try:
        llm_status = describe_config()
        providers = probe_providers()
    except Exception as e:
        llm_status = {"provider": "?", "model": "?",
                      "providers_configured": {}, "active_key_source": "none",
                      "error": str(e)}
        providers = []
    return render_template(
        "index.html",
        n_reviewers=len(REVIEWERS),
        n_domains=len(domains),
        n_personas=len(personas),
        recent_jobs=recent,
        llm_status=llm_status,
        providers=providers,
        any_provider_configured=any(p["configured"] for p in providers),
        available_databases=list_available_databases(),
        default_n_reviewers=DEFAULT_N_REVIEWERS,
        min_n_reviewers=MIN_N_REVIEWERS,
        max_n_reviewers=MAX_N_REVIEWERS,
        recommended_min_n_reviewers=RECOMMENDED_MIN_N_REVIEWERS,
        recommended_max_n_reviewers=RECOMMENDED_MAX_N_REVIEWERS,
    )


@app.post("/review")
def start_review():
    file = request.files.get("pdf")
    if not file or not file.filename:
        flash("Please choose a PDF to upload.")
        return redirect(url_for("about"))
    if not file.filename.lower().endswith(".pdf"):
        flash("Only .pdf files are accepted.")
        return redirect(url_for("about"))

    # Provider and model come entirely from config.yaml's llm_review section
    # (set via the Model page). No form overrides — keeps the submit flow
    # a single click when the config is already right.
    cfg = load_config()
    chosen_provider = cfg.resolve_provider("review")
    chosen_model = cfg.resolve_model("review")
    chosen_base_url = cfg.resolve_base_url_for_stage("review") or ""

    has_key = cfg.resolve_api_key(chosen_provider)
    if not has_key and not is_local_provider(cfg, chosen_provider):
        envs = ", ".join(env_vars_for(chosen_provider)) or "(none)"
        flash(
            f"API key missing for review provider '{chosen_provider}'. "
            f"Add it under api_keys.{chosen_provider} in config.yaml, or export "
            f"one of these env vars: {envs}. Then restart the server. "
            f"(Set review provider/model on the Model page.)"
        )
        return redirect(url_for("about"))

    chosen_database_id = request.form.get("database") or "__default__"
    try:
        db_path = resolve_database_path(chosen_database_id)
    except (ValueError, FileNotFoundError) as e:
        flash(f"Database selection invalid: {e}")
        return redirect(url_for("about"))

    raw_n = (request.form.get("n_reviewers") or "").strip()
    try:
        n_reviewers = int(raw_n) if raw_n else DEFAULT_N_REVIEWERS
    except ValueError:
        n_reviewers = DEFAULT_N_REVIEWERS
    n_reviewers = max(MIN_N_REVIEWERS, min(MAX_N_REVIEWERS, n_reviewers))

    # Reject at form submission so a small uploaded DB + a large N
    # doesn't surface deep inside node_select_reviewers as IndexError.
    try:
        db_reviewers_count = len(parse_reviewer_db(str(db_path)))
    except Exception as e:
        flash(f"Could not read the selected database: {e}")
        return redirect(url_for("review_launcher"))
    if n_reviewers > db_reviewers_count:
        flash(
            f"The selected database contains only {db_reviewers_count} "
            f"reviewer{'s' if db_reviewers_count != 1 else ''}, but you "
            f"asked for {n_reviewers}. Please lower the number of "
            f"reviewers (maximum for this database: {db_reviewers_count}) "
            f"or pick a larger database, then resubmit."
        )
        return redirect(url_for("review_launcher"))

    job_id = _timestamped_run_id("review")
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_upload_name(file.filename, default_ext=".pdf")
    pdf_path = job_dir / safe_name
    file.save(str(pdf_path))
    if not pdf_path.exists():
        # Catches the case where Werkzeug silently routes the save
        # elsewhere — fail loudly here rather than as FileNotFoundError
        # in the worker thread.
        flash(f"Upload failed: file was not saved to {pdf_path}.")
        return redirect(url_for("review_launcher"))

    _now = datetime.now(timezone.utc).isoformat()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "message": "Waiting to start",
            "filename": file.filename,
            "job_dir": str(job_dir),
            "provider": chosen_provider,
            "model": chosen_model,
            "base_url": chosen_base_url,
            "database": chosen_database_id,
            "database_path": str(db_path),
            "n_reviewers": n_reviewers,
            "started_at": _now,
            "created_at": _now,
            "updated_at": _now,
        }

    t = threading.Thread(
        target=_run_review_job,
        args=(job_id, pdf_path, job_dir, chosen_provider, chosen_model,
              str(db_path), n_reviewers),
        daemon=True,
    )
    t.start()
    logger.info("Started review job %s for %s (provider=%s model=%s "
                "reviewers=%d)", job_id, file.filename,
                chosen_provider, chosen_model, n_reviewers)
    return redirect(url_for("review_status", job_id=job_id))


@app.get("/review/<job_id>")
def review_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    if job["status"] == "done":
        return redirect(url_for("review_result", job_id=job_id))
    return render_template("review_status.html", job=job, job_id=job_id)


@app.get("/review/<job_id>/poll")
def review_poll(job_id: str):
    """JSON polling endpoint for the status page."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.get("/review/<job_id>/result")
def review_result(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    if job["status"] != "done":
        return redirect(url_for("review_status", job_id=job_id))
    report_data = json.loads(Path(job["ui_state_json"]).read_text())
    run_files = list_run_files(Path(job["job_dir"]))
    return render_template(
        "review_result.html", job=job, job_id=job_id,
        data=report_data, run_files=run_files,
    )


@app.get("/review/<job_id>/download/<kind>")
def review_download(job_id: str, kind: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or job["status"] != "done":
        abort(404)
    if kind == "md":
        return send_file(job["report_md"], as_attachment=True)
    if kind == "data":
        return send_file(job["review_data_md"], as_attachment=True)
    if kind == "clarity":
        p = Path(job["job_dir"]) / "writing_clarity_review.md"
        if not p.exists():
            abort(404)
        return send_file(str(p), as_attachment=True)
    abort(404)


@app.post("/review/<job_id>/delete")
def review_delete(job_id: str):
    """Remove a review from the registry and delete its files. Safe on any
    status (queued/running/done/errored). Redirects to the Review launcher.
    """
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if job is None:
        flash(f"Review {job_id} not found (already deleted?).")
        return redirect(url_for("review_launcher"))

    # Best-effort cleanup — at worst we leak a few files.
    job_dir = job.get("job_dir")
    if job_dir:
        try:
            p = Path(job_dir)
            if p.exists():
                shutil.rmtree(p)
                logger.info("Deleted review %s and its job dir %s", job_id, job_dir)
            else:
                logger.info("Deleted review %s (job dir %s already gone)", job_id, job_dir)
        except Exception as e:
            logger.warning("Deleted review %s from registry but failed to remove "
                           "%s on disk: %s", job_id, job_dir, e)
            flash(f"Review {job_id} removed, but files at {job_dir} could not "
                  f"be deleted: {e}")
            return redirect(url_for("review_launcher"))

    flash(f"Review {job_id} deleted.")
    return redirect(url_for("review_launcher"))
