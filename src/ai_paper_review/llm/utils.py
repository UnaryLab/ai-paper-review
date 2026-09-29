"""Small helpers used across the llm package.

* :func:`env_vars_for` — expose the env-var fallback list for a
  provider (UI surfaces this to users in "no API key" messages).
* :func:`is_local_provider` / :func:`_is_local_url` — decide whether a
  provider + base_url combination is keyless. Local Ollama / vLLM
  servers and the Copilot SDK fall into this bucket. ``make_client``
  uses the private variant to skip the API-key check; the UI
  (``probe_providers``) uses the public variant.
* :func:`provider_supports_pdf`: which provider + base_url pairs accept the paper
  PDF directly (native content-block or CLI-file-read) vs. need
  pre-extracted text. The reviewer dispatcher branches on this to
  decide between a short "review the attached paper" user message
  (+ ``pdf_path``) and the full text-scaffolded user message.
"""
from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlparse

from .config import _UNSET, LLMConfig, key_env_vars, load_config


# Providers whose complete() can ingest a PDF when given pdf_path:
#   - anthropic_api → base64 document content block
#   - openai_api    → file content block on Chat Completions
#                     (gpt-4o, gpt-4.1, etc.), only on OpenAI's own
#                     endpoint (no base_url, or an openai.com one)
#   - xai_api       → upload to /v1/files, then reference via
#                     {"type":"input_file","file_id":...} on the
#                     Responses API. Models with agentic tool
#                     calling only (e.g. grok-4.20, grok-4.5);
#                     handled by the dedicated XaiClient.
#   - google_api    → Part.from_bytes mime=application/pdf
#   - claude_sdk    → CLI Read tool on the absolute path
#
# Explicitly NOT capable (stay on text-extracted path):
#   - openai_compatible_api → OpenAI-SDK client pointed at
#     endpoints that don't implement the file content block
#   - copilot_sdk → Copilot CLI accepts only markdown text
_PDF_CAPABLE_PROVIDERS = frozenset({
    "anthropic_api", "openai_api", "xai_api", "google_api", "claude_sdk",
})


def is_openai_endpoint(base_url: Optional[str]) -> bool:
    """True for OpenAI's own endpoint: no ``base_url``, or a host that is
    ``openai.com`` or a subdomain of it."""
    if not base_url:
        return True
    host = (urlparse(base_url).hostname or "").lower()
    return host == "openai.com" or host.endswith(".openai.com")


def provider_supports_pdf(provider: str, base_url: Optional[str]) -> bool:
    """True when the provider's client can consume ``pdf_path`` natively.

    ``openai_api`` with a non-OpenAI ``base_url`` (Azure, proxies) is
    text-only: those endpoints do not accept the ``file`` content block.
    """
    if provider == "openai_api":
        return is_openai_endpoint(base_url)
    return provider in _PDF_CAPABLE_PROVIDERS


def env_vars_for(provider: str, base_url: Optional[str] = _UNSET) -> list:
    """Public accessor for the list of env-var names that can supply a
    key for ``provider``. For ``openai_compatible_api`` this is empty
    unless ``base_url`` (default: the configured one) is OpenAI's own
    endpoint, matching :meth:`LLMConfig.resolve_api_key`."""
    if provider == "openai_compatible_api" and base_url is _UNSET:
        base_url = load_config().resolve_base_url(provider)
    return key_env_vars(provider, base_url)


_LOCAL_HOSTS = frozenset({"localhost", "host.docker.internal"})


def _is_local_url(provider: str, base_url: Optional[str]) -> bool:
    """True when ``provider`` + ``base_url`` combination is keyless:
    either a CLI-backed SDK (Copilot, Claude Agent) or
    ``openai_compatible_api`` pointed at a local or private-network
    host (``localhost``, ``host.docker.internal``, a single-label name such
    as ``ollama``, a ``*.local`` / ``*.lan`` name, or a loopback / private /
    CGNAT IP such as 127.0.0.1, 192.168.x.x, 10.x.x.x, 172.16-31.x.x,
    100.64-127.x.x). Used by make_client to skip the API-key check.
    """
    if provider in ("copilot_sdk", "claude_sdk"):
        return True
    if provider != "openai_compatible_api" or not base_url:
        return False
    host = (urlparse(base_url).hostname or "").lower()
    if host in _LOCAL_HOSTS or host.endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Single-label names (e.g. a docker-compose service "ollama").
        return bool(host) and "." not in host
    return (ip.is_private or ip.is_loopback
            or ip in ipaddress.ip_network("100.64.0.0/10"))


def is_local_provider(cfg: LLMConfig, provider: str,
                      use_case: Optional[str] = None) -> bool:
    """True if `provider` is keyless — either a local server (Ollama /
    llama.cpp / vLLM) or a CLI-backed SDK (Copilot, Claude Agent — uses
    the running CLI's auth).

    Callers (``probe_providers``, conversion.py's missing-key check) use
    this to mark the provider as available in the web UI.

    With ``use_case`` (``"review"`` / ``"validation"``) the base URL is
    resolved for that stage, as ``make_client`` does; without it, the
    stage-agnostic ``resolve_base_url`` is used.
    """
    if use_case is None:
        base_url = cfg.resolve_base_url(provider)
    else:
        base_url = cfg.resolve_base_url_for_stage(use_case)
    if provider == "openai_compatible_api" and cfg.resolve_api_key(provider, base_url):
        return False
    return _is_local_url(provider, base_url)
