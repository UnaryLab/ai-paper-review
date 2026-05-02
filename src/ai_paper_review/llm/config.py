"""
ai_paper_review.llm.config
==========================

On-disk configuration loading + the in-memory ``LLMConfig`` dataclass.

The YAML lookup order is:

  1. Path given in the ``PAPER_REVIEW_CONFIG`` env var
  2. ``./config.yaml`` (current working directory)
  3. ``config.yaml`` next to this file (packaged fallback)

Per-stage (review / validation) overrides flow through six env vars
(``PAPER_REVIEW_REVIEW_*_OVERRIDE`` and
``PAPER_REVIEW_VALIDATION_*_OVERRIDE``) that the web UI's Model page
writes; ``load_config`` applies those on top of the YAML so nothing on
disk has to change for a session-scoped provider switch.

Any ``api_keys.*`` entry missing from the config falls back to its
conventional env var (see ``_ENV_FALLBACK`` below). This means a user
who keeps their keys in env vars can still pick a provider via
config.yaml.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("llm_client")


# Env-var fallbacks (classic names most tools recognize). Empty lists for
# the two SDK-backed providers (copilot_sdk, claude_sdk) because both
# inherit their CLI's local auth — no API-key env var applies.
#
# Provider names end with:
#   * ``_api`` — HTTP/REST-based providers that take an API key
#     (or PAT / bearer token); the pipeline calls them via an SDK
#     pointed at the provider's HTTP endpoint.
#   * ``_sdk`` — locally-installed SDKs that inherit a CLI's login
#     (Copilot CLI, Claude Code CLI); no API key.
_ENV_FALLBACK = {
    "anthropic_api":         ["ANTHROPIC_API_KEY"],
    "openai_api":            ["OPENAI_API_KEY"],
    "google_api":            ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "xai_api":               ["XAI_API_KEY"],
    "openai_compatible_api": ["OPENAI_API_KEY"],
    "github_api":            ["GITHUB_TOKEN", "GITHUB_PAT"],
    "copilot_sdk":           [],
    "claude_sdk":            [],
}

_DEFAULT_BASE_URLS = {
    "xai_api":    "https://api.x.ai/v1",
    "github_api": "https://models.github.ai/inference",
}

SUPPORTED_PROVIDERS = (
    "anthropic_api", "openai_api", "google_api", "xai_api",
    "openai_compatible_api", "github_api",
    "copilot_sdk", "claude_sdk",
)


# Sentinel value the ``PAPER_REVIEW_VALIDATION_*_OVERRIDE`` env vars can
# hold to mean "explicit inherit from review" for the current session
# — distinct from "unset" (which falls through to config.yaml). The
# Model page's form submits this when the user picks "— inherit from
# review —" in the validation dropdown and leaves the model field
# blank. See ``_env_override_or_inherit`` inside ``load_config``.
_VALIDATION_INHERIT_SENTINEL = "__inherit__"


@dataclass
class LLMConfig:
    """Resolved LLM configuration: which provider, model, keys, base URLs.

    The on-disk representation groups fields by *stage*::

        llm_review:                              # required
          provider: anthropic
          model:    claude-sonnet-4-5-20250929
          max_concurrent: 10
          # ... other rate-limit knobs ...

        llm_validation:                          # optional — inherits review
          provider: openai
          model:    gpt-4o-mini

    In memory the stages are flattened with a stage prefix — ``.review_*``
    vs ``.validation_*`` — so review and validation are unambiguous at
    every call site. Validation fields are ``None`` when the stage should
    inherit from review; the ``resolve_*`` methods below implement that
    fallback. API keys are shared across stages.
    """
    review_provider: str = "anthropic_api"
    review_model: str = "claude-sonnet-4-5-20250929"
    review_base_url: Optional[str] = None
    validation_provider: Optional[str] = None     # falls back to review_provider
    validation_model: Optional[str] = None        # falls back to review_model
    validation_base_url: Optional[str] = None     # falls back to review_base_url (if same provider)
    api_keys: Dict[str, str] = field(default_factory=dict)
    config_path: Optional[str] = None

    # Defaults are paid-plan-safe; for free tiers, cut max_concurrent to 1–2.
    request_delay: float = 0.0
    max_retries: int = 2
    retry_base_delay: float = 5.0
    max_concurrent: int = 10

    def resolve_model(self, use_case: str) -> str:
        if use_case == "validation":
            return self.validation_model or self.review_model
        return self.review_model

    def resolve_provider(self, use_case: str) -> str:
        if use_case == "validation":
            return self.validation_provider or self.review_provider
        return self.review_provider

    def resolve_base_url_for_stage(self, use_case: str) -> Optional[str]:
        """Base URL for this use case's stage, with provider-aware fallback.

        Validation inherits review's ``review_base_url`` ONLY when both
        stages use the same provider — otherwise there's no shared URL
        semantics between (e.g.) Anthropic and a local Ollama endpoint, so
        we fall through to the provider's hardcoded default (or None).
        """
        if use_case == "validation":
            if self.validation_base_url:
                return self.validation_base_url
            if (not self.validation_provider
                    or self.validation_provider == self.review_provider):
                if self.review_base_url:
                    return self.review_base_url
            return _DEFAULT_BASE_URLS.get(self.resolve_provider(use_case))
        return self.review_base_url or _DEFAULT_BASE_URLS.get(self.review_provider)

    def resolve_api_key(self, provider: str) -> Optional[str]:
        """Key from config first; fall back to env vars."""
        if self.api_keys.get(provider):
            return self.api_keys[provider]
        for ev in _ENV_FALLBACK.get(provider, []):
            v = os.environ.get(ev)
            if v:
                return v
        return None

    def resolve_base_url(self, provider: str) -> Optional[str]:
        """Provider → base URL lookup, ignoring stage.

        Collapses the per-stage base_urls to a single mapping for callers
        that don't know which stage they're in (``probe_providers``,
        ``is_local_provider``). Review's URL wins on conflict; falls
        through to the hardcoded defaults for xAI / GitHub Models /
        Copilot, which have fixed endpoints.
        """
        if provider == self.review_provider and self.review_base_url:
            return self.review_base_url
        if self.validation_provider == provider and self.validation_base_url:
            return self.validation_base_url
        return _DEFAULT_BASE_URLS.get(provider)


def _find_config_file() -> Optional[Path]:
    """Return the first config file that exists, in priority order:
    ``$PAPER_REVIEW_CONFIG`` → ``./config.yaml`` → ``<pkg>/config.yaml``.
    """
    for p in filter(None, [
        os.environ.get("PAPER_REVIEW_CONFIG"),
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parent / "config.yaml",
    ]):
        path = Path(p)
        if path.exists():
            return path
    return None


def load_config(path: Optional[Path] = None) -> LLMConfig:
    """Load and validate the YAML config, with env-var fallback for missing keys."""
    if path is None:
        path = _find_config_file()

    data: Dict[str, Any] = {}
    if path is not None:
        try:
            import yaml
        except ImportError:
            raise ImportError(
                "pyyaml is required to load config.yaml. Install it (already in environment.yml) "
                "or set API keys via environment variables instead."
            )
        try:
            data = yaml.safe_load(Path(path).read_text()) or {}
        except Exception as e:  # pragma: no cover
            logger.warning("Failed to parse %s: %s", path, e)
            data = {}

    review_section = data.get("llm_review") or {}
    validation_section = data.get("llm_validation") or {}

    # Env-var overrides take precedence so the web UI's Model page can
    # apply session-scoped changes without rewriting config.yaml.
    def _env_override(key: str) -> Optional[str]:
        v = os.environ.get(key)
        return v if v is not None and v != "" else None

    def _env_override_or_inherit(key: str, config_value) -> Optional[str]:
        """Validation-side env-var resolver with an inherit sentinel.

        Three states the env var can be in, with matching semantics:

        * ``__inherit__`` (sentinel) → return ``None`` and skip the
          config.yaml fallback. ``resolve_provider("validation")`` etc.
          then inherit from the review stage naturally.
        * Any other non-empty string → use that value as the override.
        * Unset or empty → fall through to ``config_value`` (the
          ``llm_validation:`` entry from config.yaml).

        The sentinel exists because the Model page's "— inherit from
        review —" dropdown + blank-model-field pattern would otherwise
        just clear the env var, silently reverting to config.yaml's
        ``llm_validation`` block — the opposite of what the user asked
        for.
        """
        v = os.environ.get(key)
        if v == _VALIDATION_INHERIT_SENTINEL:
            return None
        if v:
            return v
        return config_value or None

    review_provider = (
        _env_override("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE")
        or review_section.get("provider")
        or "anthropic_api"
    ).lower()
    review_model = (
        _env_override("PAPER_REVIEW_REVIEW_MODEL_OVERRIDE")
        or review_section.get("model")
        or "claude-sonnet-4-5-20250929"
    )
    review_base_url = (
        _env_override("PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE")
        or review_section.get("base_url")
        or None
    )

    # Validation-side env vars accept a sentinel — ``__inherit__`` — that
    # means "for this session, skip config.yaml's llm_validation block
    # and inherit from the review stage at resolve time". That's the
    # signal the Model page sends when the user picks "— inherit from
    # review —" and leaves the field blank: simply popping the env var
    # would fall through to config.yaml, which is the opposite of what
    # the user asked for.
    validation_provider_raw = _env_override_or_inherit(
        "PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE",
        validation_section.get("provider"),
    )
    validation_provider = (
        validation_provider_raw.lower() if validation_provider_raw else None
    )
    validation_model = _env_override_or_inherit(
        "PAPER_REVIEW_VALIDATION_MODEL_OVERRIDE",
        validation_section.get("model"),
    )
    validation_base_url = _env_override_or_inherit(
        "PAPER_REVIEW_VALIDATION_BASE_URL_OVERRIDE",
        validation_section.get("base_url"),
    )

    cfg = LLMConfig(
        review_provider=review_provider,
        review_model=review_model,
        review_base_url=review_base_url,
        validation_provider=validation_provider,
        validation_model=validation_model,
        validation_base_url=validation_base_url,
        api_keys={k.lower(): v for k, v in (data.get("api_keys") or {}).items()},
        config_path=str(path) if path else None,
        # Rate-limit knobs live under llm_review (the high-volume path).
        request_delay=float(review_section.get("request_delay", 0.0)),
        max_retries=int(review_section.get("max_retries", 2)),
        retry_base_delay=float(review_section.get("retry_base_delay", 5.0)),
        max_concurrent=int(review_section.get("max_concurrent", 10)),
    )
    # claude_sdk routes through a subscription-tier CLI and cannot handle
    # simultaneous parallel requests without hitting rate-limit rejection.
    # Enforce a minimum 1 s stagger between dispatched calls when no explicit
    # delay is set, so review and validation chunks don't burst simultaneously.
    if cfg.review_provider == "claude_sdk" and cfg.request_delay < 1.0:
        cfg.request_delay = 1.0

    if cfg.review_provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported review_provider {cfg.review_provider!r}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    if cfg.validation_provider and cfg.validation_provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported validation_provider {cfg.validation_provider!r}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )
    return cfg
