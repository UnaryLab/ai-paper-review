"""Build a ready-to-call :class:`LLMClient` from an :class:`LLMConfig`.

The dispatch table ``_PROVIDER_CLASS`` maps each provider string to the
right concrete client class. ``make_client`` resolves provider / model /
base_url / API key for a given use case (review or validation), instantiates
the client, and wraps it in :class:`RetryClient` so callers never see a
bare provider client.
"""
from __future__ import annotations

import logging

from .clients.anthropic import AnthropicClient
from .clients.base import LLMClient
from .clients.claude import ClaudeSDKClient
from .clients.copilot import CopilotSDKClient
from .clients.google import GoogleClient
from .clients.openai import OpenAIClient
from .clients.xai import XaiClient
from .config import _ENV_FALLBACK, LLMConfig, SUPPORTED_PROVIDERS, load_config
from .retrying import RetryClient
from .utils import _is_local_url

logger = logging.getLogger("llm_client")


_PROVIDER_CLASS = {
    "anthropic_api":         AnthropicClient,
    "openai_api":            OpenAIClient,
    "xai_api":               XaiClient,          # Chat Completions for text, Responses API + /v1/files for PDFs
    "github_api":            OpenAIClient,       # OpenAI-compatible REST
    "openai_compatible_api": OpenAIClient,
    "google_api":            GoogleClient,
    "copilot_sdk":           CopilotSDKClient,
    "claude_sdk":            ClaudeSDKClient,
}


def make_client(config: LLMConfig, use_case: str = "default") -> LLMClient:
    """Instantiate a client for this use case's resolved provider + model.

    Which provider is used depends on ``use_case`` — ``"validation"`` picks
    ``validation_provider`` (with fallback to ``provider``); everything else
    uses ``provider``. Same pattern for model (see ``resolve_model``).
    """
    provider = config.resolve_provider(use_case)
    klass = _PROVIDER_CLASS.get(provider)
    if klass is None:
        raise ValueError(
            f"No client implementation for provider {provider!r}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}."
        )

    api_key = config.resolve_api_key(provider)

    # Per-stage URL — validation may point at a different Ollama host
    # than review even when both use the same provider name.
    base_url = config.resolve_base_url_for_stage(use_case)

    if not api_key and _is_local_url(provider, base_url):
        api_key = "not-needed"
        logger.info("Local server detected (base_url=%s) — using placeholder key.",
                    base_url)

    if not api_key:
        envs = ", ".join(_ENV_FALLBACK.get(provider, []))
        raise RuntimeError(
            f"No API key for provider {provider!r}. "
            f"Set it under `api_keys.{provider}:` in config.yaml, or export {envs}."
        )

    model = config.resolve_model(use_case)
    logger.info("LLM: provider=%s model=%s use_case=%s "
                "(request_delay=%.1fs, max_retries=%d, retry_base=%.1fs)%s",
                provider, model, use_case,
                config.request_delay,
                config.max_retries, config.retry_base_delay,
                f" base_url={base_url}" if base_url else "")

    raw = klass(model=model, api_key=api_key, base_url=base_url)
    return RetryClient(raw, max_retries=config.max_retries,
                       base_delay=config.retry_base_delay)


def default_client(use_case: str = "default") -> LLMClient:
    """Convenience: load config from disk and build a client in one step."""
    return make_client(load_config(), use_case=use_case)
