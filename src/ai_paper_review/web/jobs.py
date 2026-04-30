"""In-memory job state for review and validation runs.

Two parallel registries because the two flows have different value
shapes: ``JOBS`` for paper-review jobs, ``VALIDATE_JOBS`` for
human-vs-AI validation jobs. Each is keyed by the run id, which is
also the run directory name.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from flask import request
from werkzeug.utils import secure_filename

from .app import RUNS_DIR, app, logger


JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

VALIDATE_JOBS: Dict[str, Dict[str, Any]] = {}
VALIDATE_JOBS_LOCK = threading.Lock()


_REVIEW_DIR_PREFIX = "review_"
_VALIDATION_DIR_PREFIX = "validation_"


def _timestamped_run_id(kind: str) -> str:
    """``kind`` is ``"review"`` or ``"validation"``; returns e.g.
    ``review_20260419_092345_a1b``.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{kind}_{ts}_{uuid.uuid4().hex[:3]}"


def _is_review_dir_name(name: str) -> bool:
    return name.startswith(_REVIEW_DIR_PREFIX)


def _is_validation_dir_name(name: str) -> bool:
    return name.startswith(_VALIDATION_DIR_PREFIX)


def _run_name(primary_filename: str, provider: str, model: str, launched_at: str) -> str:
    """Build a human-readable run identifier.

    Format: ``<stem>-<provider>-<model>-<YYYYMMDD-HHMMSS>``
    """
    stem = Path(primary_filename or "unknown").stem
    ts = (launched_at or "")[:19]          # '2026-04-30T10:30:00'
    ts = ts.replace("-", "").replace("T", "-").replace(":", "")  # '20260430-103000'
    safe_model = (model or "unknown").replace("/", "-")
    return f"{stem}-{provider}-{safe_model}-{ts}"


def _safe_upload_name(raw_name: Optional[str], default_ext: str = "") -> str:
    """Sanitize browser-supplied filenames to ASCII / no path components.

    Without this, names with spaces, parens, non-ASCII, or accidental
    macOS drag-and-drop path prefixes can route ``file.save()`` somewhere
    other than ``Path(job_dir) / raw_name``, which then surfaces as a
    cryptic ``FileNotFoundError`` in the worker. Falls back to a stable
    name if sanitizing yields an empty string.
    """
    safe = secure_filename(raw_name or "") if raw_name else ""
    if safe:
        return safe
    return f"upload{default_ext}" if default_ext else "upload"


def _set_job(job_id: str, **updates):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)
            JOBS[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()


def _set_validate_job(run_id: str, **updates):
    with VALIDATE_JOBS_LOCK:
        if run_id in VALIDATE_JOBS:
            VALIDATE_JOBS[run_id].update(updates)
            VALIDATE_JOBS[run_id]["updated_at"] = \
                datetime.now(timezone.utc).isoformat()


def _rehydrate_jobs_from_disk() -> int:
    """Repopulate JOBS from completed review run-dirs on disk so a server
    restart doesn't lose the "Recent reviews" list. Skips validation runs
    and any dir missing the full set of completed-review artifacts.
    """
    if not RUNS_DIR.exists():
        return 0

    loaded = 0
    for entry in sorted(RUNS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if _is_validation_dir_name(entry.name):
            continue
        if not _is_review_dir_name(entry.name):
            continue

        report_md = entry / "review_report.md"
        review_data_md = entry / "review_data.md"
        ui_state_json = entry / "_ui_state.json"

        # Completed reviews have all three; partial/errored runs are
        # skipped — without the ranked output the result page can't render.
        if not (report_md.exists() and review_data_md.exists() and ui_state_json.exists()):
            logger.debug("Skipping incomplete run dir: %s", entry.name)
            continue

        pdf_files = list(entry.glob("*.pdf"))
        filename = pdf_files[0].name if pdf_files else "(restored)"

        mtime = datetime.fromtimestamp(report_md.stat().st_mtime, tz=timezone.utc).isoformat()

        paper_title = ""
        n_issues = 0
        provider = ""
        model = ""
        launched_at = ""
        try:
            ui_state = json.loads(ui_state_json.read_text())
            paper_title = ui_state.get("paper", {}).get("title", "")
            n_issues = len(ui_state.get("ranked_clusters", []))
            provider = ui_state.get("llm_provider", "")
            model = ui_state.get("llm_model", "")
            launched_at = ui_state.get("launched_at", "")
        except Exception as e:
            logger.warning("Could not parse %s: %s", ui_state_json, e)

        JOBS[entry.name] = {
            "status": "done",
            "message": f"Review complete: {n_issues} ranked issues",
            "filename": filename,
            "paper_title": paper_title,
            "provider": provider,
            "model": model,
            "started_at": launched_at or mtime,
            "job_dir": str(entry),
            "report_md": str(report_md),
            "review_data_md": str(review_data_md),
            "ui_state_json": str(ui_state_json),
            "n_issues": n_issues,
            "created_at": mtime,
            "updated_at": mtime,
            "restored": True,
        }
        loaded += 1

    if loaded:
        logger.info("Restored %d existing review(s) from %s", loaded, RUNS_DIR)
    return loaded


# iOS Safari and Chrome cache form-triggered redirect targets, so a
# delete + redirect can show the just-deleted row still on the list page
# until the user re-navigates. Force revalidation on these list pages.
_NO_CACHE_ENDPOINTS = {"review_launcher", "validate_form"}


@app.after_request
def _no_cache_for_list_pages(resp):
    if request.endpoint in _NO_CACHE_ENDPOINTS:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


_rehydrate_jobs_from_disk()
