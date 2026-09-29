"""Provider availability probing — for the web UI's picker + diagnostics.

Three public entry points:

* :func:`describe_config` — JSON-friendly summary of the active config
  (no secrets). Used by the web UI's status endpoint and the
  ``__main__`` debug dump.
* :func:`probe_providers` — full per-provider availability descriptors
  (configured / auth_kind / key_source / unavailable_reason / labels)
  consumed by the Model page's provider grid.
* :func:`_copilot_sdk_installed` — does the local Python env have the
  ``copilot`` SDK importable? Tests monkey-patch this to script
  Copilot-installed scenarios.

The "configured" status follows a strict policy: a provider is only
green when **both** its own credential/SDK check passes AND the user
has a ``config.yaml`` on disk. This keeps the UI consistent with the
missing-config banner ``base.html`` shows when ``config.yaml`` is
absent.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from .clients.claude import CLI_ENV_OVERRIDES
from .config import LLMConfig, key_env_vars, load_config
from .utils import is_local_provider

logger = logging.getLogger("llm_client")


_PROVIDER_LABELS = {
    "anthropic_api":         "Anthropic (Claude)",
    "openai_api":            "OpenAI (GPT)",
    "google_api":            "Google (Gemini)",
    "xai_api":               "xAI (Grok)",
    "copilot_sdk":           "GitHub Copilot (Python SDK)",
    "claude_sdk":            "Claude Agent (Python SDK)",
    "openai_compatible_api": "OpenAI-compatible (Ollama / Together / Azure / …)",
}

# Providers the user can choose in the UI. Names use the ``_api`` suffix
# for HTTP-based providers and the ``_sdk`` suffix for CLI-backed SDKs
# so the kind is visible at a glance in the dropdown.
_UI_PROVIDERS = (
    "anthropic_api", "openai_api", "google_api", "xai_api",
    "claude_sdk", "copilot_sdk",
    "openai_compatible_api",
)


_SDK_PROBE_LOGGED = False
_CLAUDE_SDK_PROBE_LOGGED = ""
_CLAUDE_SDK_READY = False
_CLAUDE_SDK_FAILED_AT = float("-inf")  # time.monotonic() of the last False


def _copilot_sdk_installed() -> bool:
    """Check if the Copilot SDK is importable.

    Uses ``importlib.util.find_spec`` rather than actually importing — that
    avoids side effects from the SDK's init, and avoids pyflakes warnings
    about unused imports. Checks both the top-level package and the
    ``copilot.session`` submodule, since ``CopilotSDKClient`` needs both.
    Guards against partial installs where the top-level package is present
    but a required submodule is missing.

    On the first probe that returns False, logs a helpful message explaining
    the most likely cause (SDK installed in a different Python env).
    """
    global _SDK_PROBE_LOGGED
    import importlib.util

    try:
        if importlib.util.find_spec("copilot") is None:
            if not _SDK_PROBE_LOGGED:
                logger.info(
                    "copilot_sdk provider: `copilot` package not found in %s. "
                    "Install with `pip install github-copilot-sdk` in the same "
                    "environment used to start the server.",
                    sys.executable,
                )
                _SDK_PROBE_LOGGED = True
            return False
        if importlib.util.find_spec("copilot.session") is None:
            logger.warning(
                "copilot_sdk provider: top-level `copilot` package is installed "
                "but `copilot.session` submodule is missing — possible partial/"
                "broken install."
            )
            return False
        return True
    except (ImportError, ValueError) as e:
        # ValueError: "__spec__ is None" for malformed installs.
        logger.debug("Copilot SDK not importable: %s", e)
        return False
    except Exception as e:
        logger.warning("Unexpected error probing Copilot SDK: %s", e)
        return False


def _claude_cli_path() -> Optional[str]:
    """The CLI the SDK runs, found with the SDK's own lookup: its bundled
    binary, ``claude`` on PATH, then the usual install locations. If that
    private lookup fails for any other reason, falls back to
    ``shutil.which("claude")``."""
    from claude_agent_sdk import ClaudeAgentOptions, CLINotFoundError
    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )
        return SubprocessCLITransport("", ClaudeAgentOptions())._find_cli()
    except CLINotFoundError:
        return None
    except Exception as e:
        logger.debug("SDK CLI lookup failed, using shutil.which: %s", e)
        return shutil.which("claude")


def _claude_sdk_installed() -> bool:
    """Check that claude_sdk can run: the SDK is importable, the Claude
    Code CLI is found, and ``claude auth status --json`` reports
    ``loggedIn``.

    Uses ``importlib.util.find_spec`` so a probe can't trigger the SDK's
    init side effects. The auth check runs with the same env overrides
    as :class:`ClaudeSDKClient`, so it reports the login the SDK will
    use. A True result is kept for the life of the process; a False
    result is kept for 30 seconds and then checked again, so a later
    ``claude auth login`` shows up without a restart. Each distinct
    reason for False is logged once.
    """
    global _CLAUDE_SDK_READY, _CLAUDE_SDK_PROBE_LOGGED, _CLAUDE_SDK_FAILED_AT
    if _CLAUDE_SDK_READY:
        return True
    if time.monotonic() - _CLAUDE_SDK_FAILED_AT < 30:
        return False
    import importlib.util

    reason = ""
    try:
        if importlib.util.find_spec("claude_agent_sdk") is None:
            reason = (
                f"`claude_agent_sdk` package not found in {sys.executable}. "
                "Install with `pip install claude-agent-sdk` in the same "
                "environment used to start the server"
            )
        elif (cli := _claude_cli_path()) is None:
            reason = "Claude Code CLI (`claude`) not found"
        else:
            proc = subprocess.run(
                [cli, "auth", "status", "--json"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, **CLI_ENV_OVERRIDES},
            )
            if not json.loads(proc.stdout).get("loggedIn"):
                reason = "Claude Code CLI is not logged in; run `claude auth login`"
    except Exception as e:
        reason = f"could not check the Claude Code login ({type(e).__name__}: {e})"

    if reason:
        _CLAUDE_SDK_FAILED_AT = time.monotonic()
        if reason != _CLAUDE_SDK_PROBE_LOGGED:
            logger.info("claude_sdk provider not ready: %s", reason)
            _CLAUDE_SDK_PROBE_LOGGED = reason
        return False
    _CLAUDE_SDK_READY = True
    return True


def describe_config(cfg: Optional[LLMConfig] = None) -> Dict[str, Any]:
    """Return a dict that's safe to show in the UI (no secrets)."""
    cfg = cfg or load_config()
    # Mirror probe_providers' policy: env vars alone don't make a provider
    # "configured" for display, even though they do drive runtime resolution.
    has_config = bool(cfg.config_path)
    configured: Dict[str, bool] = {}
    for p in _UI_PROVIDERS:
        if p == "copilot_sdk":
            creds_ok = _copilot_sdk_installed()
        elif p == "claude_sdk":
            creds_ok = _claude_sdk_installed()
        else:
            creds_ok = bool(cfg.resolve_api_key(p)) or is_local_provider(cfg, p)
        configured[p] = creds_ok and has_config

    if cfg.review_provider == "copilot_sdk":
        active_key_source = "Copilot CLI auth" if _copilot_sdk_installed() else "SDK not installed"
    elif cfg.review_provider == "claude_sdk":
        active_key_source = "Claude Code CLI auth" if _claude_sdk_installed() else "SDK not installed or CLI not logged in"
    else:
        active_key_source = (
            "config.yaml" if cfg.api_keys.get(cfg.review_provider)
            else "env var" if cfg.resolve_api_key(cfg.review_provider)
            else "none"
        )

    return {
        "review_provider":     cfg.review_provider,
        "review_model":        cfg.review_model,
        "validation_provider": cfg.validation_provider or cfg.review_provider,
        "validation_model":    cfg.validation_model or cfg.review_model,
        "config_path":         cfg.config_path,
        "providers_configured": configured,
        "active_key_source": active_key_source,
        "request_delay":    cfg.request_delay_for(cfg.review_provider),
        "max_retries":      cfg.max_retries,
        "retry_base_delay": cfg.retry_base_delay,
        "max_concurrent":   cfg.max_concurrent,
        "max_tokens":       cfg.review_max_tokens,
    }


def probe_providers(cfg: Optional[LLMConfig] = None) -> list:
    """Return a list of provider availability descriptors for the UI picker.

    Each entry has:
      name, label, configured, is_review_default, review_model, base_url,
      auth_kind — one of "api_key", "sdk_install", "local_server"
      key_source — short description of where the credential came from (when configured)
      unavailable_reason — short description of why configured=False (when not)
      env_vars — env var names checked (may be empty for keyless providers)

    ``is_review_default`` and ``review_model`` answer "is this the
    review-side selection" — validation-side defaults are handled by
    the Model page's separate validation form block.
    """
    cfg = cfg or load_config()
    # Green requires BOTH config.yaml present AND the provider's own
    # creds/SDK. This keeps the picker dots consistent with the
    # missing-config banner shown in base.html.
    has_config = bool(cfg.config_path)
    _CONFIG_MISSING_REASON = (
        "config.yaml not found — create one from config.example.yaml"
    )
    out = []
    for p in _UI_PROVIDERS:
        key = cfg.resolve_api_key(p)
        base_url = cfg.resolve_base_url(p)

        if p == "copilot_sdk":
            auth_kind = "sdk_install"
            creds_ok = _copilot_sdk_installed()
            creds_source = "Copilot CLI auth" if creds_ok else ""
            creds_reason = (
                "" if creds_ok
                else "SDK not installed — run: pip install github-copilot-sdk"
            )
        elif p == "claude_sdk":
            auth_kind = "sdk_install"
            creds_ok = _claude_sdk_installed()
            creds_source = "Claude Code CLI auth" if creds_ok else ""
            creds_reason = (
                "" if creds_ok
                else "SDK not installed or CLI not logged in; run: "
                     "pip install claude-agent-sdk, then claude auth login"
            )
        elif p == "openai_compatible_api" and is_local_provider(cfg, p):
            auth_kind = "local_server"
            creds_ok = True
            creds_source = f"local server at {base_url}"
            creds_reason = ""
        else:
            auth_kind = "api_key"
            creds_ok = key is not None
            if creds_ok:
                creds_source = "config.yaml" if cfg.api_keys.get(p) else "env var"
                creds_reason = ""
            else:
                creds_source = ""
                envs = " or ".join(key_env_vars(p, base_url))
                creds_reason = (
                    f"no API key — set api_keys.{p} in config.yaml"
                    + (f", or export {envs}" if envs else "")
                )

        configured = creds_ok and has_config
        if configured:
            key_source = creds_source
            unavailable_reason = ""
        else:
            key_source = ""
            parts: list = []
            if not has_config:
                parts.append(_CONFIG_MISSING_REASON)
            if not creds_ok:
                parts.append(creds_reason)
            unavailable_reason = "; ".join(parts)

        out.append({
            "name": p,
            "label": _PROVIDER_LABELS.get(p, p),
            "configured": configured,
            "auth_kind": auth_kind,
            "key_source": key_source,
            "unavailable_reason": unavailable_reason,
            "is_review_default": (p == cfg.review_provider),
            "env_vars": key_env_vars(p, base_url),
            "base_url": base_url or "",
            "review_model": cfg.review_model if p == cfg.review_provider else "",
        })
    return out
