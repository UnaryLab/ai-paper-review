"""Model page: provider availability grid + per-session override form.

The override form writes to process env vars (read by ``load_config``
on every request); ``config.yaml`` is never touched, so a restart
reverts to the on-disk values.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from flask import flash, redirect, render_template, request, url_for

from ai_paper_review.llm.config import (
    SUPPORTED_PROVIDERS,
    _VALIDATION_INHERIT_SENTINEL,
    load_config,
)
from ai_paper_review.llm.probing import (
    _PROVIDER_LABELS,
    _UI_PROVIDERS,
    describe_config,
    probe_providers,
)

from .app import app


# Maps form field name → env var that ``load_config`` reads. Each side
# is prefixed with its stage (``review_*`` / ``validation_*``) so it's
# unambiguous which path a given override flows through.
_MODEL_OVERRIDE_ENVS = {
    "review_provider":     "PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE",
    "review_model":        "PAPER_REVIEW_REVIEW_MODEL_OVERRIDE",
    "review_base_url":     "PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE",
    "validation_provider": "PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE",
    "validation_model":    "PAPER_REVIEW_VALIDATION_MODEL_OVERRIDE",
    "validation_base_url": "PAPER_REVIEW_VALIDATION_BASE_URL_OVERRIDE",
}


@app.get("/model")
def model_settings():
    try:
        cfg = load_config()
    except Exception as e:
        # ValueError from load_config is already flashed by the app-wide
        # context processor.
        if not isinstance(e, ValueError):
            flash(f"Could not load config: {e}")
        cfg = None
    providers = []
    status = {}
    if cfg:
        try:
            providers = probe_providers(cfg)
            status = describe_config(cfg)
        except Exception as e:
            flash(f"Could not probe providers: {e}")
            providers = []
            status = {}
    # The provider selects need options even when probing failed, or the
    # required select blocks every submit.
    provider_options = providers or [
        {"name": p, "label": _PROVIDER_LABELS.get(p, p)} for p in _UI_PROVIDERS
    ]

    config_path = (cfg.config_path if (cfg and cfg.config_path)
                   else str(Path.cwd() / "config.yaml"))

    overrides_active = any(
        os.environ.get(k) for k in _MODEL_OVERRIDE_ENVS.values()
    )

    # Resolved (post-inheritance) values so the template can show what
    # the validation stage *actually* runs with — important when the
    # validation form fields are blank and inherit from review.
    if cfg:
        resolved_validation = {
            "provider": cfg.resolve_provider("validation"),
            "model":    cfg.resolve_model("validation"),
            "base_url": cfg.resolve_base_url_for_stage("validation") or "",
            "inherits": cfg.validation_provider is None
                        and cfg.validation_model is None
                        and cfg.validation_base_url is None,
        }
    else:
        resolved_validation = None

    return render_template(
        "model_settings.html",
        cfg=cfg,
        providers=providers,
        provider_options=provider_options,
        status=status,
        config_path=config_path,
        overrides_active=overrides_active,
        resolved_validation=resolved_validation,
    )


@app.post("/model")
def model_settings_apply():
    """Apply form values as process env vars (session-scoped — not
    written to disk; revert on restart)."""
    action = request.form.get("action", "apply")
    if action == "reset":
        for env in _MODEL_OVERRIDE_ENVS.values():
            os.environ.pop(env, None)
        flash("Session overrides cleared — now using config.yaml values.")
        return redirect(url_for("model_settings"))

    review_provider = (request.form.get("review_provider") or "").strip().lower()
    if not review_provider:
        flash("Review provider is required.")
        return redirect(url_for("model_settings"))
    if review_provider not in SUPPORTED_PROVIDERS:
        flash(f"Unsupported review provider: {review_provider!r}. "
              f"Must be one of: {', '.join(SUPPORTED_PROVIDERS)}.")
        return redirect(url_for("model_settings"))

    validation_provider = (request.form.get("validation_provider") or "").strip().lower()
    if validation_provider and validation_provider not in SUPPORTED_PROVIDERS:
        flash(f"Unsupported validation provider: {validation_provider!r}. "
              f"Must be one of: {', '.join(SUPPORTED_PROVIDERS)}.")
        return redirect(url_for("model_settings"))

    review_model = (request.form.get("review_model") or "").strip()
    if not review_model:
        flash("Review model is required.")
        return redirect(url_for("model_settings"))

    # Review fields: blank = clear override, so value falls back to
    # config.yaml (the review stage has no "inherit from X" concept).
    # Validation fields: blank = write the inherit sentinel so
    # ``load_config`` forces the validation-side to fall back to the
    # review stage at resolve time — otherwise a blank validation
    # field would silently revert to ``config.yaml``'s
    # ``llm_validation`` block, which is the opposite of what the
    # "(inherit from review provider)" dropdown implies.
    _VALIDATION_FIELDS = {
        "validation_provider", "validation_model", "validation_base_url",
    }
    for field in ("review_base_url", "validation_base_url"):
        url = (request.form.get(field) or "").strip()
        if not url:
            continue
        try:
            parsed = urlparse(url)
            ok = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        except ValueError:
            ok = False
        if not ok:
            flash(f"Invalid {field.replace('_', ' ')}: {url!r}. "
                  "Use an http:// or https:// URL with a host.")
            return redirect(url_for("model_settings"))

    # The base URL fields are prefilled with the current values, so after
    # a provider switch the old provider's URL comes back as if typed.
    # Drop a posted URL that just echoes the current one for a changed
    # provider.
    try:
        cur = load_config()
    except Exception:
        cur = None
    stale_fields = set()
    if cur:
        if review_provider != cur.resolve_provider("review"):
            stale_fields.add("review_base_url")
        new_val_provider = validation_provider or review_provider
        if new_val_provider != cur.resolve_provider("validation"):
            stale_fields.add("validation_base_url")

    for field, env in _MODEL_OVERRIDE_ENVS.items():
        val = (request.form.get(field) or "").strip()
        if field in stale_fields and val == (
                cur.resolve_base_url_for_stage(field.split("_")[0]) or ""):
            val = ""
        if val:
            os.environ[env] = val
        elif field in _VALIDATION_FIELDS:
            os.environ[env] = _VALIDATION_INHERIT_SENTINEL
        else:
            os.environ.pop(env, None)

    flash("Changes applied for this session (not written to config.yaml). "
          "Restart the server or press the button at the bottom to reset to "
          "config.yaml values.")
    return redirect(url_for("model_settings"))
