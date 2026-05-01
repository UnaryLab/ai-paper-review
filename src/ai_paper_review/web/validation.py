"""Validation pipeline: form, submit handler, status / poll / result /
download / delete routes, the threaded worker that runs the pipeline,
and the ``Recent validations`` listing.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import traceback as _tb
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from ai_paper_review.llm.config import load_config
from ai_paper_review.llm.factory import make_client
from ai_paper_review.llm.probing import describe_config, probe_providers
from ai_paper_review.llm.utils import env_vars_for, is_local_provider
from ai_paper_review.review.parsing import review_dict_to_markdown
from ai_paper_review.validation import conversion as cr
from ai_paper_review.validation.alignment import align_comments
from ai_paper_review.validation.calibration import build_calibration
from ai_paper_review.validation.loading import load_actual, load_ai
from ai_paper_review.validation.metrics import compute_metrics
from ai_paper_review.validation.reporting import format_report

from .app import RUNS_DIR, app, logger
from .databases import DEFAULT_DB_TABLES, REVIEWERS
from .jobs import (
    JOBS,
    JOBS_LOCK,
    VALIDATE_JOBS,
    VALIDATE_JOBS_LOCK,
    _is_validation_dir_name,
    _run_name,
    _safe_upload_name,
    _set_validate_job,
    _timestamped_run_id,
)
from .run_files import list_run_files


def _run_validate_job(
    run_id: str,
    run_dir: Path,
    actual_in: Path,
    ai_job_id: str,
    ai_upload_path: Optional[Path],
    actual_filename: str,
    ai_filename: Optional[str] = None,
):
    """Worker thread — runs convert (if needed) → align → metrics →
    calibration → report, posting progress to ``VALIDATE_JOBS[run_id]``.
    """
    from ai_paper_review.provenance import format_provenance, now_iso
    launched_at = now_iso()
    try:
        is_md = actual_in.suffix.lower() == ".md"
        already_structured = False
        if is_md:
            raw_md = actual_in.read_text()
            already_structured = bool(re.search(r"^##+\s*Comment\s", raw_md,
                                                re.MULTILINE))

        # Resolve the paper title from the linked AI review job (if any) so
        # actual_converted.md gets the correct title rather than "Unknown".
        _paper_title_for_converted = actual_in.stem  # fallback: filename stem
        if ai_job_id:
            with JOBS_LOCK:
                _linked_ai_job = JOBS.get(ai_job_id)
            if _linked_ai_job:
                _paper_title_for_converted = (
                    _linked_ai_job.get("paper_title") or actual_in.stem
                )

        if is_md and already_structured:
            _set_validate_job(run_id, status="loading",
                              message="Human reviews already in AI-review format — skipping conversion.")
            actual_path = actual_in
        else:
            _set_validate_job(run_id, status="converting",
                              message="Converting human review to structured markdown via LLM…")
            cv_cfg = load_config()
            val_provider = cv_cfg.resolve_provider("validation")
            val_has_key = cv_cfg.resolve_api_key(val_provider)
            if not val_has_key and not is_local_provider(cv_cfg, val_provider):
                envs = ", ".join(env_vars_for(val_provider)) or "(none)"
                raise RuntimeError(
                    f"API key missing for validation provider '{val_provider}'. "
                    f"Add it under api_keys.{val_provider} in config.yaml, or "
                    f"export one of these env vars: {envs}."
                )
            raw = actual_in.read_text()
            extracted = cr.llm_extract(
                raw, DEFAULT_DB_TABLES.category_vocab,
                run_dir=run_dir,
            )
            extracted = cr.normalize_extracted(
                extracted, DEFAULT_DB_TABLES.category_vocab,
            )

            md_lines = [
                "# Converted Reviews", "",
                f"**Paper ID:** {actual_in.stem}",
                f"**Title:** {_paper_title_for_converted}",
                f"**Notes:** Converted from {actual_in.name} via web UI "
                f"(LLM, input was {'markdown' if is_md else 'text'}).",
                "",
            ]
            for i, rv in enumerate(extracted["actual_reviews"], start=1):
                md_lines.append("---")
                md_lines.append("")
                if "overall_recommendation" not in rv and "recommendation" in rv:
                    rv["overall_recommendation"] = rv["recommendation"]
                rv_md = review_dict_to_markdown(rv)
                rv_md = rv_md.replace("# Review\n", f"# Review {i}\n", 1)
                md_lines.append(rv_md)
            actual_path = run_dir / "actual_converted.md"
            actual_path.write_text("\n".join(md_lines))
            logger.info("Wrote AI-review-format conversion to %s (%d reviews)",
                        actual_path, len(extracted["actual_reviews"]))

        _set_validate_job(run_id, status="loading",
                          message="Loading review files…")
        if ai_job_id:
            with JOBS_LOCK:
                ai_job = JOBS.get(ai_job_id)
            if ai_job is None or ai_job.get("status") != "done":
                raise RuntimeError("Selected AI review job is not available.")
            ai_path = Path(ai_job["review_data_md"])
        else:
            ai_path = ai_upload_path

        actual = load_actual(str(actual_path))
        ai_report = load_ai(str(ai_path))
        n_human = len(actual["flat_comments"])
        n_ai = len(ai_report["flat_comments"])
        total_pairs = n_human * n_ai
        import math as _math
        n_chunks_est = max(1, _math.ceil(n_human / 10))
        # Pre-build chunk label list: [{label, row_start, row_end}] for UI.
        _CHUNK_SIZE = 10
        chunk_labels = [
            {
                "label": f"Chunk {ci + 1}",
                "row_start": ci * _CHUNK_SIZE + 1,
                "row_end": min((ci + 1) * _CHUNK_SIZE, n_human),
            }
            for ci in range(n_chunks_est)
        ]
        _set_validate_job(
            run_id, status="aligning",
            message=(f"Aligning {n_human} human × {n_ai} AI = {total_pairs} pairs "
                     f"across {n_chunks_est} parallel chunk{'s' if n_chunks_est != 1 else ''}…"),
            n_human=n_human, n_ai=n_ai, total_pairs=total_pairs,
            n_chunks=n_chunks_est, chunks_done=0,
            chunk_labels=chunk_labels, completed_chunk_indices=[],
        )

        cfg = load_config()
        llm_client = make_client(cfg, use_case="validation")
        val_provider = cfg.resolve_provider("validation")
        val_model = cfg.resolve_model("validation")
        val_base_url = cfg.resolve_base_url_for_stage("validation")
        logger.info("Validation using LLM: provider=%s model=%s",
                    val_provider, val_model)

        _completed_indices: list = []

        def _chunk_progress(done: int, total: int, ci: int) -> None:
            _completed_indices.append(ci)
            _set_validate_job(
                run_id,
                chunks_done=done, n_chunks=total,
                completed_chunk_indices=list(_completed_indices),
                message=(f"Aligning {n_human} human × {n_ai} AI — "
                         f"chunk {done}/{total} done…"),
            )

        alignment = align_comments(
            actual["flat_comments"], ai_report["flat_comments"],
            llm_client,
            run_dir=run_dir,
            on_chunk_done=_chunk_progress,
        )

        _set_validate_job(run_id, status="computing",
                          message="Computing metrics and calibration deltas")
        metrics = compute_metrics(alignment)
        calibration = build_calibration(
            alignment, ai_report, REVIEWERS, DEFAULT_DB_TABLES,
            actual_reviews=actual.get("actual_reviews", []),
        )
        llm_comparison = alignment.get("llm_comparison")

        _set_validate_job(run_id, status="writing",
                          message="Writing report and calibration delta")
        ended_at = now_iso()
        report_md = format_report(
            actual, ai_report, alignment, metrics, calibration,
            llm_comparison=llm_comparison,
            tables=DEFAULT_DB_TABLES,
        )
        provenance = format_provenance(
            provider=val_provider,
            model=val_model,
            base_url=val_base_url,
            launched_at=launched_at,
            ended_at=ended_at,
        )
        (run_dir / "validation_report.md").write_text(provenance + report_md)
        (run_dir / "calibration_delta.json").write_text(json.dumps({
            "paper_id": actual.get("paper_id"),
            "metrics": metrics,
            "summary": calibration["summary"],
            "persona_stats": calibration["persona_stats"],
            "miss_attributions": calibration["miss_attributions"],
            "sub_rating_attributions": calibration.get("sub_rating_attributions", []),
            "suggestions": calibration["suggestions"],
            "llm_comparison": (
                {
                    "summary": llm_comparison.get("summary", ""),
                    "matches": llm_comparison.get("matches", []),
                    "missed": llm_comparison.get("missed", []),
                    "extras": llm_comparison.get("extras", []),
                    "llm_model": llm_comparison.get("llm_model", ""),
                } if llm_comparison else None
            ),
        }, indent=2, default=str))

        validation_name = _run_name(
            actual_filename, val_provider, val_model, launched_at,
        )
        # Prefer the AI review's title (set by the LLM extraction pipeline);
        # fall back to whatever the human-review conversion wrote.
        paper_title = ai_report.get("title") or actual.get("title") or ""
        ui_state = {
            "validation_name": validation_name,
            "paper_title": paper_title,
            "metrics": metrics,
            "alignment": alignment,
            "calibration": calibration,
            "actual_meta": {
                "paper_id": actual.get("paper_id"),
                "paper_title": actual.get("title"),
                "n_reviews": actual.get("n_reviews"),
                "flat_comments_count": len(actual.get("flat_comments", [])),
                "flat_strengths_count": len(actual.get("flat_strengths", [])),
            },
            "actual_filename": actual_filename,
            "ai_filename": ai_filename or "(upload)",
            # Absolute paths so the result page can show the AI-review
            # source even when it lives outside run_dir (the "prior AI
            # review" dropdown points at another review's run_dir).
            "ai_review_source_path": str(Path(ai_path).resolve()),
            "actual_source_path": str(Path(actual_path).resolve()),
            "run_dir_name": run_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            # LLM + timing provenance — shown on the result page so a
            # reader can tell which model produced the alignment and
            # how long the run took without opening the markdown.
            "llm_provider": val_provider,
            "llm_model": val_model,
            "llm_base_url": val_base_url or "",
            "launched_at": launched_at,
            "ended_at": ended_at,
        }
        (run_dir / "_ui_state.json").write_text(
            json.dumps(ui_state, indent=2, default=str)
        )
        _set_validate_job(run_id, status="done",
                          message="Validation complete",
                          n_hits=len(alignment.get("hits", [])),
                          n_misses=len(alignment.get("misses", [])),
                          n_false_alarms=len(alignment.get("false_alarms", [])))
        logger.info("Validation job %s complete", run_id)
    except Exception as e:
        logger.exception("Validation job %s failed", run_id)
        _set_validate_job(
            run_id, status="error",
            message=f"{type(e).__name__}: {e}",
            traceback=_tb.format_exc(),
        )
        # Write a minimal _ui_state.json even on error so the run directory
        # is discoverable after a server restart and can be deleted from the UI.
        try:
            _err_state = {
                "validation_name": locals().get("validation_name") or run_id,
                "paper_title": locals().get("paper_title") or "",
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "metrics": {},
                "alignment": {},
                "calibration": {},
                "actual_meta": {},
                "actual_filename": actual_filename,
                "ai_filename": ai_filename or "",
                "ai_review_source_path": "",
                "actual_source_path": str(actual_in) if actual_in else "",
                "run_dir_name": run_dir.name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "llm_provider": locals().get("val_provider") or "",
                "llm_model": locals().get("val_model") or "",
                "llm_base_url": locals().get("val_base_url") or "",
                "launched_at": launched_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
            }
            (run_dir / "_ui_state.json").write_text(
                json.dumps(_err_state, indent=2, default=str)
            )
        except Exception as write_err:
            logger.warning("Could not write error _ui_state.json for %s: %s",
                           run_id, write_err)


@app.get("/validation")
def validate_form():
    past_reviews = []
    with JOBS_LOCK:
        for jid, job in JOBS.items():
            if job.get("status") == "done" and job.get("review_data_md"):
                filename = job.get("filename") or ""
                pdf_stem = Path(filename).stem if filename else jid
                provider = job.get("provider") or job.get("llm_provider") or ""
                model    = job.get("model") or job.get("llm_model") or ""
                parts = [pdf_stem]
                if provider:
                    parts.append(provider)
                if model:
                    parts.append(model)
                past_reviews.append({
                    "job_id": jid,
                    "label": "-".join(parts),
                    "started_at": job.get("started_at") or job.get("created_at", ""),
                })
    past_reviews.sort(key=lambda p: p.get("started_at", ""), reverse=True)
    try:
        llm_status = describe_config()
        providers = probe_providers()
    except Exception as e:
        logger.warning("Could not probe LLM config for /validate: %s", e)
        llm_status = {}
        providers = []
    return render_template(
        "validation.html",
        past_reviews=past_reviews,
        llm_status=llm_status,
        any_provider_configured=any(p["configured"] for p in providers),
        recent_validations=list_validations(),
    )


@app.post("/validation")
def validate_run():
    actual_file = request.files.get("actual")
    ai_job_id = request.form.get("ai_job_id") or ""
    ai_file = request.files.get("ai_review")

    if not actual_file or not actual_file.filename:
        flash("Please upload the actual human reviews.")
        return redirect(url_for("validate_form"))

    if not ai_job_id and (not ai_file or not ai_file.filename):
        flash("Please either upload an AI review file or pick a prior review.")
        return redirect(url_for("validate_form"))

    run_id = _timestamped_run_id("validation")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    safe_actual = _safe_upload_name(actual_file.filename, default_ext=".md")
    actual_in = run_dir / safe_actual
    actual_file.save(str(actual_in))
    if not actual_in.exists():
        flash(f"Upload failed: human-review file was not saved to {actual_in}.")
        return redirect(url_for("validate_form"))

    ai_upload_path: Optional[Path] = None
    if not ai_job_id:
        safe_ai = _safe_upload_name(ai_file.filename, default_ext=".md")
        ai_upload_path = run_dir / safe_ai
        ai_file.save(str(ai_upload_path))
        if not ai_upload_path.exists():
            flash(f"Upload failed: AI-review file was not saved to {ai_upload_path}.")
            return redirect(url_for("validate_form"))

    # ai_label is shown in the Recent-validations table.
    if ai_job_id:
        with JOBS_LOCK:
            prior = JOBS.get(ai_job_id) or {}
        prior_name = prior.get("filename") or ai_job_id
        ai_label = f"prior: {prior_name}"
    elif ai_file and ai_file.filename:
        ai_label = ai_file.filename
    else:
        ai_label = "(upload)"

    # Resolve the validation-stage LLM identity at submit time so the
    # status page can show it from the first poll, before the worker
    # has progressed past the early conversion stage.
    try:
        cfg = load_config()
        val_provider = cfg.resolve_provider("validation")
        val_model = cfg.resolve_model("validation")
        val_base_url = cfg.resolve_base_url_for_stage("validation") or ""
    except Exception:
        val_provider = val_model = val_base_url = ""

    with VALIDATE_JOBS_LOCK:
        VALIDATE_JOBS[run_id] = {
            "status": "queued",
            "message": "Waiting to start",
            "run_dir": str(run_dir),
            "run_id": run_id,
            "actual_filename": actual_file.filename,
            "ai_job_id": ai_job_id or None,
            "ai_filename": ai_label,
            "llm_provider": val_provider,
            "llm_model": val_model,
            "llm_base_url": val_base_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    t = threading.Thread(
        target=_run_validate_job,
        args=(run_id, run_dir, actual_in, ai_job_id, ai_upload_path,
              actual_file.filename, ai_label),
        daemon=True,
    )
    t.start()
    logger.info("Started validation job %s (actual=%s, ai=%s)",
                run_id, actual_file.filename, ai_label)
    return redirect(url_for("validate_status", run_id=run_id))


@app.get("/validation/<run_id>/status")
def validate_status(run_id: str):
    """Status page for a running validation job; redirects to the result
    page when the worker finishes.
    """
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        abort(404)
    with VALIDATE_JOBS_LOCK:
        job = VALIDATE_JOBS.get(run_id)
    if job is None:
        # Recover from server-restart mid-run (or stale URL) by jumping
        # straight to the result page when the run dir exists.
        if (RUNS_DIR / run_id / "_ui_state.json").exists():
            return redirect(url_for("validate_result_view", run_dir=run_id))
        abort(404)
    if job["status"] == "done":
        return redirect(url_for("validate_result_view", run_dir=run_id))
    return render_template("validation_status.html", job=job, run_id=run_id)


@app.get("/validation/<run_id>/poll")
def validate_poll(run_id: str):
    """JSON polling endpoint for the validation status page."""
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        abort(404)
    with VALIDATE_JOBS_LOCK:
        job = VALIDATE_JOBS.get(run_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.get("/validation/<run_dir>/download/<fname>")
def validate_download(run_dir: str, fname: str):
    # Download whitelist — primary outputs plus the intermediate
    # artifacts useful for debugging a run's verdicts.
    if fname not in (
        "validation_report.md",
        "calibration_delta.json",
        "actual_converted.md",
        "actual_raw_llm.md",
        "alignment_llm_analysis.md",
        "alignment_similarities.md",
        "alignment_ranking.md",
    ):
        abort(404)
    p = RUNS_DIR / run_dir / fname
    if not p.exists():
        abort(404)
    return send_file(str(p), as_attachment=True)


@app.get("/validation/<run_dir>/result")
def validate_result_view(run_dir: str):
    """Re-render the validation result page from the persisted UI state.
    No LLM calls are re-issued — the data comes from ``_ui_state.json``
    written at the end of a successful run.
    """
    if "/" in run_dir or "\\" in run_dir or ".." in run_dir:
        abort(404)
    d = RUNS_DIR / run_dir
    state_path = d / "_ui_state.json"
    if not state_path.exists():
        flash(f"Validation {run_dir!r} has no renderable state "
              "(may pre-date the UI state feature). You can still download "
              "the validation report if it exists.")
        return redirect(url_for("about"))
    try:
        state = json.loads(state_path.read_text())
    except Exception as e:
        flash(f"Couldn't load validation state: {e}")
        return redirect(url_for("about"))
    # Fold the AI-review source into the Inputs catalog. For uploads,
    # list_run_files will see and dedup it; for "prior AI review" runs
    # it lives in a different run_dir so this is how it shows up.
    extra_inputs: List[Dict[str, str]] = []
    ai_src = state.get("ai_review_source_path")
    if ai_src:
        extra_inputs.append({
            "path": ai_src,
            "description": "AI review source data consumed by this validation run.",
        })

    return render_template(
        "validation_result.html",
        validation_name=state.get("validation_name", "Validation report"),
        paper_title=state.get("paper_title", ""),
        metrics=state.get("metrics", {}),
        alignment=state.get("alignment", {}),
        calibration=state.get("calibration", {}),
        actual_meta=state.get("actual_meta", {}),
        run_dir_name=state.get("run_dir_name", run_dir),
        run_files=list_run_files(d, extra_inputs=extra_inputs),
        llm_provider=state.get("llm_provider"),
        llm_model=state.get("llm_model"),
        llm_base_url=state.get("llm_base_url") or "(default)",
        launched_at=state.get("launched_at"),
        ended_at=state.get("ended_at"),
    )


@app.post("/validation/<run_dir>/delete")
def validate_delete(run_dir: str):
    """Delete a past validation run and all its files from disk."""
    if "/" in run_dir or "\\" in run_dir or ".." in run_dir:
        abort(404)
    if not _is_validation_dir_name(run_dir):
        abort(404)
    d = RUNS_DIR / run_dir
    # Drop the in-memory entry first so the Recent-validations table
    # doesn't keep showing a stale row even if a worker thread is still
    # writing into the unlinked directory (file handles survive unlink
    # on POSIX).
    with VALIDATE_JOBS_LOCK:
        VALIDATE_JOBS.pop(run_dir, None)
    if not d.exists() or not d.is_dir():
        flash(f"Validation {run_dir!r} not found (already deleted?).")
        return redirect(url_for("validate_form"))
    try:
        shutil.rmtree(d)
        flash(f"Deleted validation '{run_dir}'.")
    except Exception as e:
        logger.warning("Failed to delete %s: %s", d, e)
        flash(f"Delete failed: {e}")
    return redirect(url_for("validate_form"))


def list_validations() -> List[Dict[str, Any]]:
    """List validation runs for the Recent validations table.

    Unions in-memory ``VALIDATE_JOBS`` (running runs) with on-disk
    ``validation_*`` directories (completed runs that survive restart).
    On overlap, the in-memory entry wins because it carries fresher
    status. Sorted newest-first by ``updated_at``.
    """
    # Files the validation worker writes into the run_dir; used to tell
    # the uploaded AI-review file (saved under its original name) apart
    # from internal artifacts when backfilling pre-ai_filename runs.
    _INTERNAL_RUN_FILES = {
        "_ui_state.json",
        "actual_converted.md",
        "actual_raw_llm.md",
        "validation_report.md",
        "calibration_delta.json",
        "alignment_llm_analysis.md",
        "alignment_similarities.md",
        "alignment_ranking.md",
    }

    def _backfill_ai_filename(run_dir: Path) -> str:
        """Recover the AI-review filename for runs whose _ui_state.json
        predates the ai_filename field. Prior-review runs (where the AI
        side was a reference to another job, not an upload) still show
        '?' since no file was copied in.
        """
        try:
            candidates = [
                f for f in run_dir.iterdir()
                if f.is_file()
                and f.suffix == ".md"
                and f.name not in _INTERNAL_RUN_FILES
            ]
        except OSError:
            return "?"
        if len(candidates) == 1:
            return candidates[0].name
        if len(candidates) > 1:
            # Heuristic for legacy rows: AI review data files tend to be
            # smaller than the full report. Not authoritative but beats '?'.
            return min(candidates, key=lambda p: p.stat().st_size).name
        return "?"

    rows: Dict[str, Dict[str, Any]] = {}

    with VALIDATE_JOBS_LOCK:
        jobs_snapshot = {k: dict(v) for k, v in VALIDATE_JOBS.items()}
    for run_id, job in jobs_snapshot.items():
        rows[run_id] = {
            "run_id": run_id,
            "actual_filename": job.get("actual_filename") or "?",
            "ai_filename": job.get("ai_filename") or "?",
            "status": job.get("status", "queued"),
            "started_at": job.get("created_at") or "",
            "updated_at": job.get("updated_at") or job.get("created_at") or "",
        }

    if RUNS_DIR.exists():
        for entry in RUNS_DIR.iterdir():
            if not entry.is_dir() or not _is_validation_dir_name(entry.name):
                continue
            if entry.name in rows:
                continue
            state_path = entry / "_ui_state.json"
            if not state_path.exists():
                # Errored run — no _ui_state.json written. Show it with
                # status="error" so the user can delete the directory.
                mtime_err = datetime.fromtimestamp(
                    entry.stat().st_mtime, tz=timezone.utc
                ).isoformat()
                rows[entry.name] = {
                    "run_id": entry.name,
                    "actual_filename": "?",
                    "ai_filename": "?",
                    "status": "error",
                    "started_at": mtime_err,
                    "updated_at": mtime_err,
                }
                continue
            try:
                state = json.loads(state_path.read_text())
            except Exception:
                continue
            ai_filename = state.get("ai_filename")
            if not ai_filename:
                ai_filename = _backfill_ai_filename(entry)
            rows[entry.name] = {
                "run_id": entry.name,
                "actual_filename": state.get("actual_filename") or "?",
                "ai_filename": ai_filename,
                "status": state.get("status") or "done",
                "started_at": state.get("launched_at") or state.get("created_at") or "",
                "updated_at": state.get("created_at") or "",
            }

    out = list(rows.values())
    out.sort(key=lambda d: d.get("started_at", ""), reverse=True)
    return out
