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
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("llm_client")

DEFAULT_MODEL = "claude-sonnet-5-5"


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
    "copilot_sdk":           [],
    "claude_sdk":            [],
}

_DEFAULT_BASE_URLS = {
    "xai_api":    "https://api.x.ai/v1",
}

SUPPORTED_PROVIDERS = (
    "anthropic_api", "openai_api", "google_api", "xai_api",
    "openai_compatible_api",
    "copilot_sdk", "claude_sdk",
)


def key_env_vars(provider: str, base_url: Optional[str]) -> list:
    """Env vars that may supply ``provider``'s key at ``base_url``.
    ``openai_compatible_api`` gets ``OPENAI_API_KEY`` only on OpenAI's own
    endpoint, so the OpenAI key never goes to a third-party host."""
    if provider == "openai_compatible_api":
        from .utils import is_openai_endpoint
        if not is_openai_endpoint(base_url):
            return []
    return list(_ENV_FALLBACK.get(provider, []))


def check_provider(provider: str, field: str = "provider") -> None:
    """Raise ``ValueError`` if ``provider`` is not a supported name."""
    if provider == "github_api":
        raise ValueError(
            "github_api was removed: GitHub Models is retired; choose another provider"
        )
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported {field} {provider!r}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}"
        )


# Sentinel value the ``PAPER_REVIEW_VALIDATION_*_OVERRIDE`` env vars can
# hold to mean "explicit inherit from review" for the current session
# — distinct from "unset" (which falls through to config.yaml). The
# Model page's form submits this when the user picks "— inherit from
# review —" in the validation dropdown and leaves the model field
# blank. See ``_env_override_or_inherit`` inside ``load_config``.
_VALIDATION_INHERIT_SENTINEL = "__inherit__"

# Default for optional args where ``None`` is a meaningful value.
_UNSET: Any = object()


@dataclass
class LLMConfig:
    """Resolved LLM configuration: which provider, model, keys, base URLs.

    The on-disk representation groups fields by *stage*::

        llm_review:                              # required
          provider: anthropic
          model:    claude-sonnet-5-5
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
    review_model: str = DEFAULT_MODEL
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

    # Output-token budget for each reviewer, clarity, and markdown-repair
    # call. Reasoning models count reasoning tokens against this budget, so
    # the client default (4000) can run out before any review text. Models
    # with a smaller output cap need a smaller value.
    review_max_tokens: int = 16000

    def set_review_llm(self, provider: Optional[str], model: Optional[str],
                       base_url: Optional[str] = None) -> None:
        """Point the review stage at ``provider`` / ``model`` (``None`` keeps
        the current value). A new provider drops the current base_url,
        which belongs to the old provider; ``base_url`` sets one explicitly.
        """
        if provider and provider != self.review_provider:
            self.review_provider = provider
            self.review_base_url = None
        if model:
            self.review_model = model
        if base_url:
            self.review_base_url = base_url

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

    def request_delay_for(self, provider: str) -> float:
        """Seconds between dispatched calls for ``provider``.

        claude_sdk routes through a subscription-tier CLI that rejects
        bursts of parallel requests, so it gets at least 1 s.
        """
        if provider == "claude_sdk":
            return max(self.request_delay, 1.0)
        return self.request_delay

    def resolve_api_key(self, provider: str,
                        base_url: Optional[str] = _UNSET) -> Optional[str]:
        """Key from config first; fall back to env vars.

        ``openai_compatible_api`` falls back to ``OPENAI_API_KEY`` only when
        ``base_url`` (default: ``resolve_base_url(provider)``) is OpenAI's
        own endpoint, so the OpenAI key never goes to a third-party host.
        """
        if self.api_keys.get(provider):
            return self.api_keys[provider]
        if base_url is _UNSET:
            base_url = self.resolve_base_url(provider)
        for ev in key_env_vars(provider, base_url):
            v = os.environ.get(ev)
            if v:
                return v
        return None

    def resolve_base_url(self, provider: str) -> Optional[str]:
        """Provider → base URL lookup, ignoring stage.

        Collapses the per-stage base_urls to a single mapping for callers
        that don't know which stage they're in (``probe_providers``,
        ``is_local_provider``). Review's URL wins on conflict; falls
        through to the hardcoded default for xAI, which has a fixed
        endpoint.
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

    def _tuning(key, cast, default, minimum):
        """Read an ``llm_review`` tuning key. An empty YAML value (``key:``)
        loads as None and gives the default; a bool, a non-number, a
        non-finite number, or a value below ``minimum`` raises ValueError."""
        value = review_section.get(key)
        if value is None:
            return default
        kind = "an integer" if cast is int else "a number"
        try:
            if isinstance(value, bool):
                raise ValueError
            result = cast(value)
            if not math.isfinite(result):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                f"config.yaml llm_review.{key} must be {kind}, got {value!r}"
            ) from None
        if result < minimum:
            raise ValueError(
                f"config.yaml llm_review.{key} must be >= {minimum}, got {value!r}"
            )
        return result

    review_provider = (review_section.get("provider") or "anthropic_api").lower()
    review_model = review_section.get("model") or DEFAULT_MODEL
    review_base_url = review_section.get("base_url") or None

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
    # A provider override drops the YAML base_url, which belongs to the
    # YAML provider (same rule as ``set_review_llm``).
    yaml_validation_base_url = validation_section.get("base_url")
    # Compare with the provider the YAML block resolves to (its own, or
    # the inherited review provider), not just its ``provider`` key.
    yaml_effective_provider = (
        (validation_section.get("provider") or review_provider or "").lower() or None
    )
    review_provider_override = (
        (_env_override("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE") or "").lower() or None
    )
    effective_provider = (
        validation_provider or review_provider_override or review_provider
    )
    if effective_provider != yaml_effective_provider:
        yaml_validation_base_url = None
    validation_base_url = _env_override_or_inherit(
        "PAPER_REVIEW_VALIDATION_BASE_URL_OVERRIDE",
        yaml_validation_base_url,
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
        request_delay=_tuning("request_delay", float, 0.0, 0),
        max_retries=_tuning("max_retries", int, 2, 0),
        retry_base_delay=_tuning("retry_base_delay", float, 5.0, 0),
        max_concurrent=_tuning("max_concurrent", int, 10, 1),
        review_max_tokens=_tuning("max_tokens", int, LLMConfig.review_max_tokens, 1),
    )
    cfg.set_review_llm(
        review_provider_override,
        _env_override("PAPER_REVIEW_REVIEW_MODEL_OVERRIDE"),
        _env_override("PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE"),
    )
    check_provider(cfg.review_provider, "review_provider")
    if cfg.validation_provider:
        check_provider(cfg.validation_provider, "validation_provider")
    return cfg
