"""
ai_paper_review.web.app
=======================

Minimal Flask UI over the AI paper review system. Lets a user:

  * Upload a PDF → kick off an AI review in the background → view results
  * Upload raw human reviews (text) → convert to markdown → optionally validate
    against an AI review → see calibration suggestions
  * Browse the reviewer prompts in the active database

This module owns only the cross-cutting pieces: filesystem paths, the
Flask ``app`` instance, the global ``logger``, the config-status
context processor, and the ``main()`` entry point. Each route group
lives in its own sibling module and registers via import-side-effect
(Pattern B) — see the bottom of this file for the registration block.

Runs at http://127.0.0.1:8000 by default:
    conda activate ai-paper-review
    ai-paper-review-web
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask

from ai_paper_review import bundled_db_dir, bundled_db_paths, default_db_path
from ai_paper_review.llm.config import load_config


# Default WORKDIR follows cwd so users who `cd` into their workspace find
# uploads + run artifacts there. Override with PAPER_REVIEW_WORKDIR.
WORKDIR = Path(os.environ.get("PAPER_REVIEW_WORKDIR", Path.cwd() / "ai-paper-review-data"))
UPLOAD_DIR = WORKDIR / "uploads"
RUNS_DIR = WORKDIR / "runs"
DATABASES_DIR = WORKDIR / "databases"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)
DATABASES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DB_PATH = default_db_path()
BUNDLED_DB_DIR = bundled_db_dir()


def _find_docs_root() -> Optional[Path]:
    """Locate the source-tree directory that contains README.md + docs/.

    Returns None when running from an installed wheel without the source
    tree (the /docs page renders a friendly placeholder in that case).
    """
    here = Path(__file__).resolve()
    for parent in [here.parents[i] for i in range(1, 6)]:
        if (parent / "README.md").exists() and (parent / "docs").is_dir():
            return parent
    return None


DOCS_ROOT = _find_docs_root()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB per upload
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai_paper_review.web")


@app.context_processor
def _inject_config_status() -> Dict[str, Any]:
    """Expose config.yaml presence to every template so the missing-config
    banner in base.html doesn't need each route to pass the flag."""
    try:
        cfg = load_config()
        missing = not cfg.config_path
    except Exception:
        missing = True
    return {
        "config_yaml_missing": missing,
        "config_yaml_expected_path": str(Path.cwd() / "config.yaml"),
    }


# Route registration by import side effect. Each submodule decorates its
# handlers with ``@app.route(...)``; importing them attaches the routes
# to the ``app`` instance above. Import order is load-bearing in one
# direction only: ``databases`` and ``jobs`` define shared state
# (REVIEWERS, JOBS, etc.) that ``review`` / ``validation`` consume, so
# they come first.
#
# On a plain first import, ``import_module`` runs the submodule body and
# its decorators register on this ``app``. Under ``importlib.reload(app)``
# (used by the test suite), the submodules are already in ``sys.modules``,
# so a bare ``from . import <sub>`` would NOT re-execute them and the
# new ``app`` instance would have zero routes. Reloading explicitly
# re-runs each submodule body against the fresh ``app``.
import importlib as _importlib
import sys as _sys

for _sub in ("jobs", "databases", "review", "validation", "aggregation",
             "model", "docs"):
    _full = f"{__package__}.{_sub}"
    if _full in _sys.modules:
        _importlib.reload(_sys.modules[_full])
    else:
        _importlib.import_module(f".{_sub}", __package__)

del _importlib, _sys, _sub, _full


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    logger.info("Starting AI Paper Review app on http://%s:%d  (debug=%s)", host, port, debug)
    try:
        _cfg = load_config()
        logger.info("Review LLM: provider=%s model=%s (config: %s)",
                    _cfg.review_provider, _cfg.review_model,
                    _cfg.config_path or "(none — env vars only)")
        if not _cfg.config_path:
            logger.warning(
                "No config.yaml found. Expected at %s (or set PAPER_REVIEW_CONFIG). "
                "Copy config.example.yaml to config.yaml and fill in your API keys. "
                "A banner will remind users on the web UI.",
                Path.cwd() / "config.yaml",
            )
        if not _cfg.resolve_api_key(_cfg.review_provider):
            logger.warning(
                "No API key for review provider '%s'. Paper review and LLM conversion will fail. "
                "Fill config.yaml or export the provider's API key env var.",
                _cfg.review_provider,
            )
    except Exception as e:
        logger.warning("Could not resolve LLM config at startup: %s", e)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
