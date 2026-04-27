"""Small helpers used across the llm package.

* :func:`env_vars_for` — expose the env-var fallback list for a
  provider (UI surfaces this to users in "no API key" messages).
* :func:`is_local_provider` / :func:`_is_local_url` — decide whether a
  provider + base_url combination is keyless. Local Ollama / vLLM
  servers and the Copilot SDK fall into this bucket. ``make_client``
  uses the private variant to skip the API-key check; the UI
  (``probe_providers``) uses the public variant.
* :func:`provider_supports_pdf` — which providers accept the paper
  PDF directly (native content-block or CLI-file-read) vs. need
  pre-extracted text. The reviewer dispatcher branches on this to
  decide between a short "review the attached paper" user message
  (+ ``pdf_path``) and the full text-scaffolded user message.
"""
from __future__ import annotations

from typing import Optional

from .config import _ENV_FALLBACK, LLMConfig


# Providers whose complete() can ingest a PDF when given pdf_path:
#   - anthropic_api → base64 document content block
#   - openai_api    → file content block on Chat Completions
#                     (gpt-4o, gpt-4.1, etc.)
#   - xai_api       → upload to /v1/files, then reference via
#                     {"type":"input_file","file_id":...} on the
#                     Responses API. grok-4-class models only;
#                     handled by the dedicated XaiClient.
#   - google_api    → Part.from_bytes mime=application/pdf
#   - claude_sdk    → CLI Read tool on the absolute path
#
# Explicitly NOT capable (stay on text-extracted path):
#   - github_api, openai_compatible_api → OpenAI-SDK clients pointed at
#     endpoints that don't implement the file content block
#   - copilot_sdk → Copilot CLI accepts only markdown text
_PDF_CAPABLE_PROVIDERS = frozenset({
    "anthropic_api", "openai_api", "xai_api", "google_api", "claude_sdk",
})


def provider_supports_pdf(provider: str) -> bool:
    """True when the provider's client can consume ``pdf_path`` natively."""
    return provider in _PDF_CAPABLE_PROVIDERS


def env_vars_for(provider: str) -> list:
    """Public accessor for the list of env-var names that can supply a
    key for ``provider``."""
    return list(_ENV_FALLBACK.get(provider, []))


def _is_local_url(provider: str, base_url: Optional[str]) -> bool:
    """True when ``provider`` + ``base_url`` combination is keyless —
    either a CLI-backed SDK (Copilot, Claude Agent) or
    ``openai_compatible_api`` pointed at a local server. Used by
    make_client to skip the API-key check.
    """
    if provider in ("copilot_sdk", "claude_sdk"):
        return True
    return provider == "openai_compatible_api" and bool(base_url)


def is_local_provider(cfg: LLMConfig, provider: str) -> bool:
    """True if `provider` is keyless — either a local server (Ollama /
    llama.cpp / vLLM) or a CLI-backed SDK (Copilot, Claude Agent — uses
    the running CLI's auth).

    Callers (``probe_providers``, conversion.py's missing-key check) use
    this to mark the provider as available in the web UI.
    """
    if provider in ("copilot_sdk", "claude_sdk"):
        return True
    return (
        provider == "openai_compatible_api"
        and not cfg.resolve_api_key(provider)
        and bool(cfg.resolve_base_url(provider))
    )
