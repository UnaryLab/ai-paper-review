"""Reviewer-database management — listing, upload, view, delete.

Owns the singleton ``REVIEWERS`` (the bundled default DB parsed once at
startup), the ``list_available_databases`` / ``resolve_database_path``
helpers used by the review submit flow, and the ``/database/...`` HTTP
routes.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List

from flask import abort, flash, redirect, render_template, request, send_file, url_for

from ai_paper_review.review.reviewer_db import (
    AttributionTables,
    Reviewer,
    parse_reviewer_database,
    parse_reviewer_db,
)

from .app import (
    DATABASES_DIR,
    DEFAULT_DB_PATH,
    WORKDIR,
    app,
    logger,
)


def list_available_databases() -> List[Dict[str, Any]]:
    """Discover all reviewer DBs (bundled default + user uploads under
    ``DATABASES_DIR``). The ``is_default`` flag distinguishes the bundled
    DB from uploads — templates render it as a "default" badge in place
    of the Delete button.
    """
    out: List[Dict[str, Any]] = []

    def _make_label(path: Path) -> str:
        try:
            first_line = path.read_text().splitlines()[0].lstrip("# ").strip()
        except Exception:
            first_line = ""
        title = first_line or path.stem
        return f"{title}  ({path.name})"

    try:
        default_reviewers = parse_reviewer_db(str(DEFAULT_DB_PATH))
        out.append({
            "id": "__default__",
            "label": _make_label(DEFAULT_DB_PATH),
            "path": str(DEFAULT_DB_PATH),
            "n_reviewers": len(default_reviewers),
            "is_default": True,
            "can_delete": False,
        })
    except Exception as e:
        logger.exception("Bundled default DB failed to parse: %s", e)

    if DATABASES_DIR.exists():
        for p in sorted(DATABASES_DIR.glob("*.md")):
            try:
                n = len(parse_reviewer_db(str(p)))
                out.append({
                    "id": p.name,
                    "label": _make_label(p),
                    "path": str(p),
                    "n_reviewers": n,
                    "is_default": False,
                    "can_delete": True,
                })
            except Exception as e:
                logger.warning("Skipping malformed DB at %s: %s", p, e)
    return out


def resolve_database_path(database_id: str) -> Path:
    """``__default__`` → bundled DB. Anything else is a filename inside
    ``DATABASES_DIR``. Path-traversal characters are rejected.
    """
    if not database_id or database_id == "__default__":
        return DEFAULT_DB_PATH
    if "/" in database_id or "\\" in database_id or ".." in database_id:
        raise ValueError(f"Invalid database id: {database_id!r}")
    p = DATABASES_DIR / database_id
    if not p.exists():
        raise FileNotFoundError(f"Database not found: {database_id}")
    return p


# Loaded once at startup for the /reviewers browsing page; per-review the
# selected DB is reloaded in the job thread so non-default uploads work.
# Both the reviewer list and the validation attribution tables come from
# the same parse so a future custom default DB ships its own tables
# without touching the web module.
try:
    _DEFAULT_DB = parse_reviewer_database(str(DEFAULT_DB_PATH))
    REVIEWERS: List[Reviewer] = _DEFAULT_DB.reviewers
    DEFAULT_DB_TABLES: AttributionTables = _DEFAULT_DB.tables
    logger.info("Loaded %d reviewers from default DB %s", len(REVIEWERS), DEFAULT_DB_PATH)
    logger.info("Runtime data directory: %s", WORKDIR)
    logger.info("User-uploaded databases directory: %s", DATABASES_DIR)
except Exception:
    logger.exception("Failed to load default reviewer DB")
    REVIEWERS = []
    DEFAULT_DB_TABLES = AttributionTables()


def _load_reviewers_for_database_id(database_id: str) -> List[Reviewer]:
    """``__default__`` returns the bundled REVIEWERS list (parsed at
    startup); any other id is parsed on demand from DATABASES_DIR.
    Per-request parse cost is fine — files are <1MB and this only runs
    on browser pages.
    """
    if not database_id or database_id == "__default__":
        return REVIEWERS
    if "/" in database_id or "\\" in database_id or ".." in database_id:
        raise FileNotFoundError(database_id)
    p = DATABASES_DIR / database_id
    if not p.exists():
        raise FileNotFoundError(str(p))
    return parse_reviewer_db(str(p))


@app.get("/database")
def databases_list():
    """List the available reviewer databases: bundled default + user uploads.

    Uploaded files live in ``{workdir}/databases/``.
    """
    dbs = list_available_databases()
    return render_template("database.html", databases=dbs,
                           databases_dir=str(DATABASES_DIR))


@app.get("/database/template.yaml")
def databases_download_template():
    """Serve the bundled YAML config (comparch_reviewer_cfg.yaml) as a
    downloadable template for building a new reviewer database."""
    from ai_paper_review import data as _data_pkg
    tpl = Path(_data_pkg.__file__).parent / "comparch_reviewer_cfg.yaml"
    if not tpl.exists():
        abort(404)
    return send_file(str(tpl), as_attachment=True,
                     download_name="reviewer_database_config.yaml",
                     mimetype="text/yaml")


@app.post("/database/upload")
def databases_upload():
    """Accept a ``.md`` reviewer database after validating it parses to
    at least one reviewer — surfaces malformed DBs at upload time rather
    than as a cryptic crash inside a review job."""
    from .jobs import _safe_upload_name

    file = request.files.get("database")
    if not file or not file.filename:
        flash("Please choose a .md database to upload.")
        return redirect(url_for("databases_list"))
    if not file.filename.lower().endswith(".md"):
        flash("Only .md files are accepted.")
        return redirect(url_for("databases_list"))

    name = _safe_upload_name(file.filename, default_ext=".md")
    if not name or name.startswith("."):
        flash("Invalid filename.")
        return redirect(url_for("databases_list"))

    target = DATABASES_DIR / name
    # Save → parse → atomic move into place so a bad upload never
    # replaces a working DB with the same name.
    tmp = DATABASES_DIR / f".upload_{uuid.uuid4().hex[:8]}_{name}"
    try:
        file.save(str(tmp))
        try:
            parsed = parse_reviewer_db(str(tmp))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            flash(f"Database did not parse: {type(e).__name__}: {e}")
            return redirect(url_for("databases_list"))
        if not parsed:
            tmp.unlink(missing_ok=True)
            flash("Database parsed but contains zero reviewers. "
                  "Generate one with `ai-paper-review-generate-db` or follow "
                  "the bundled format.")
            return redirect(url_for("databases_list"))
        tmp.replace(target)
        flash(f"Uploaded '{name}' — {len(parsed)} reviewers.")
    except Exception as e:
        logger.exception("Database upload failed")
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        flash(f"Upload failed: {e}")
    return redirect(url_for("databases_list"))


@app.post("/database/<name>/delete")
def databases_delete(name: str):
    """Remove a user-uploaded database. The bundled default is protected."""
    if name == "__default__" or "/" in name or "\\" in name or ".." in name:
        flash("Refusing to delete that.")
        return redirect(url_for("databases_list"))
    p = DATABASES_DIR / name
    if not p.exists():
        flash(f"Database {name!r} not found (already deleted?).")
        return redirect(url_for("databases_list"))
    try:
        p.unlink()
        flash(f"Deleted database '{name}'.")
    except Exception as e:
        logger.warning("Failed to delete %s: %s", p, e)
        flash(f"Delete failed: {e}")
    return redirect(url_for("databases_list"))


@app.get("/database/<name>/view")
def databases_view(name: str):
    """Browse the reviewers belonging to a specific database."""
    try:
        all_reviewers = _load_reviewers_for_database_id(name)
    except FileNotFoundError:
        flash(f"Database {name!r} not found.")
        return redirect(url_for("databases_list"))
    except Exception as e:
        flash(f"Database {name!r} failed to parse: {e}")
        return redirect(url_for("databases_list"))

    db_label = name
    for db in list_available_databases():
        if db["id"] == name:
            db_label = db["label"]
            break

    domain = request.args.get("domain") or ""
    persona = request.args.get("persona") or ""
    q = (request.args.get("q") or "").strip().lower()
    items = all_reviewers
    if domain:
        items = [r for r in items if r.domain == domain]
    if persona:
        items = [r for r in items if r.persona == persona]
    if q:
        items = [
            r for r in items
            if q in r.id.lower()
            or q in r.domain.lower()
            or q in r.persona.lower()
            or any(q in k.lower() for k in r.keywords)
        ]
    domains = sorted({r.domain for r in all_reviewers})
    personas = sorted({r.persona for r in all_reviewers})
    return render_template(
        "reviewers.html",
        reviewers=items,
        domains=domains,
        personas=personas,
        selected_domain=domain,
        selected_persona=persona,
        q=q,
        total=len(all_reviewers),
        database_id=name,
        database_label=db_label,
    )


@app.get("/reviewers")
def reviewers_list():
    """Legacy route → /database/__default__/view."""
    return redirect(url_for("databases_view", name="__default__"))


@app.get("/database/<name>/reviewers/<rid>")
def databases_reviewer_detail(name: str, rid: str):
    """Individual reviewer page, scoped to the DB it belongs to."""
    try:
        reviewers = _load_reviewers_for_database_id(name)
    except FileNotFoundError:
        abort(404)
    r = next((x for x in reviewers if x.id == rid), None)
    if r is None:
        abort(404)
    return render_template("reviewer_detail.html", r=r,
                           database_id=name)


@app.get("/reviewers/<rid>")
def reviewer_detail(rid: str):
    """Legacy route → per-DB reviewer detail under the default."""
    return redirect(url_for("databases_reviewer_detail", name="__default__",
                            rid=rid))
