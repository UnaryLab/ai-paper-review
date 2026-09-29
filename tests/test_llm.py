"""Config loader, provider probe, and CLI override behavior."""
from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace

import pytest

import ai_paper_review
from ai_paper_review.llm import probing  # imported as a module so monkeypatch can swap _copilot_sdk_installed at its source
from ai_paper_review.llm.clients.claude import ClaudeSDKClient
from ai_paper_review.llm.clients.copilot import CopilotSDKClient
from ai_paper_review.llm.config import LLMConfig, SUPPORTED_PROVIDERS, load_config
from ai_paper_review.llm.factory import _PROVIDER_CLASS, make_client
from ai_paper_review.llm.probing import describe_config, probe_providers
from ai_paper_review.llm.retrying import RetryClient
from ai_paper_review.llm.utils import (
    env_vars_for,
    is_local_provider,
    provider_supports_pdf,
)


def test_public_api_imports():
    # The package __init__ files deliberately expose nothing — every
    # name is reached via its explicit submodule path. Confirm the
    # canonical paths resolve.
    from ai_paper_review import default_db_path
    from ai_paper_review.llm.config import load_config
    from ai_paper_review.llm.factory import make_client
    from ai_paper_review.llm.probing import probe_providers
    assert callable(default_db_path)
    assert callable(load_config)
    assert callable(make_client)
    assert callable(probe_providers)


def test_default_db_path_exists():
    p = ai_paper_review.default_db_path()
    assert p.exists(), f"bundled DB not found at {p}"
    body = p.read_text()
    assert "#### R001" in body and "#### R200" in body


def test_load_config_without_file_or_env(isolated_config):
    cfg = load_config()
    # Canonical name uses the ``_api`` suffix.
    assert cfg.review_provider == "anthropic_api"
    assert cfg.resolve_api_key("anthropic_api") is None
    assert cfg.config_path is None


def test_load_config_from_cwd_yaml(config_with_openai):
    cfg = load_config()
    assert cfg.review_provider == "openai_api"
    assert cfg.review_model == "gpt-4o"
    assert cfg.resolve_api_key("openai_api") == "sk-test-openai"


def test_env_var_fallback(isolated_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = load_config()
    assert cfg.resolve_api_key("anthropic_api") == "sk-ant-test"
    # Other providers are still not configured.
    assert cfg.resolve_api_key("openai_api") is None


def test_cli_override_env_vars(isolated_config, monkeypatch):
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", "google_api")
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_MODEL_OVERRIDE", "gemini-2.5-pro")
    cfg = load_config()
    assert cfg.review_provider == "google_api"
    assert cfg.review_model == "gemini-2.5-pro"


def test_validation_inherit_sentinel_overrides_config_yaml(isolated_config, monkeypatch):
    """When the Model page user picks "— inherit from review —" and
    blanks the validation model field, the web handler writes the
    ``__inherit__`` sentinel into ``PAPER_REVIEW_VALIDATION_*_OVERRIDE``
    env vars. ``load_config`` must then resolve validation to None
    (triggering inherit-from-review) instead of silently falling
    through to ``config.yaml``'s ``llm_validation`` block — which
    would revert the user's session change and leave them confused."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: anthropic_api\n"
        "  model: claude-opus-5-5\n"
        "llm_validation:\n"
        "  provider: openai_api\n"        # config.yaml says openai for validation
        "  model: gpt-4o-mini\n"
    )
    # Baseline: no env vars set → config.yaml's llm_validation wins.
    cfg = load_config()
    assert cfg.review_provider == "anthropic_api"
    assert cfg.validation_provider == "openai_api"
    assert cfg.validation_model == "gpt-4o-mini"

    # Web handler writes the sentinel for blank validation fields.
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "__inherit__")
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_MODEL_OVERRIDE", "__inherit__")
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_BASE_URL_OVERRIDE", "__inherit__")
    cfg = load_config()
    # Validation fields are all None — load_config skipped config.yaml.
    assert cfg.validation_provider is None
    assert cfg.validation_model is None
    assert cfg.validation_base_url is None
    # resolve_*("validation") now inherits from the review stage.
    assert cfg.resolve_provider("validation") == "anthropic_api"
    assert cfg.resolve_model("validation") == "claude-opus-5-5"


def test_validation_env_override_with_explicit_value_wins(isolated_config, monkeypatch):
    """Non-sentinel env values still override config.yaml — regression
    guard for the sentinel refactor above."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: anthropic_api\n"
        "  model: claude-opus-5-5\n"
        "llm_validation:\n"
        "  provider: openai_api\n"
        "  model: gpt-4o-mini\n"
    )
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "google_api")
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_MODEL_OVERRIDE", "gemini-2.5-flash")
    cfg = load_config()
    assert cfg.validation_provider == "google_api"
    assert cfg.validation_model == "gemini-2.5-flash"


def test_probe_providers_shape(isolated_config, monkeypatch):
    # Force both SDK probes to a known state so the test doesn't depend on
    # whether the local env has those packages installed.
    monkeypatch.setattr(probing, "_copilot_sdk_installed", lambda: False)
    monkeypatch.setattr(probing, "_claude_sdk_installed", lambda: False)
    probed = probe_providers()
    assert len(probed) == 7
    names = [p["name"] for p in probed]
    assert set(names) == {"anthropic_api", "openai_api", "google_api", "xai_api",
                           "copilot_sdk", "claude_sdk",
                           "openai_compatible_api"}
    for p in probed:
        assert set(p.keys()) >= {"name", "label", "configured", "key_source",
                                  "is_review_default", "env_vars"}
        assert p["configured"] is False  # no keys anywhere, SDKs mocked absent


def test_probe_providers_mixed(config_with_openai, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-google")
    probed = {p["name"]: p for p in probe_providers()}
    assert probed["openai_api"]["configured"] is True
    assert probed["openai_api"]["key_source"] == "config.yaml"
    assert probed["google_api"]["configured"] is True
    assert probed["google_api"]["key_source"] == "env var"
    assert probed["anthropic_api"]["configured"] is False
    assert probed["xai_api"]["configured"] is False


def test_describe_config_hides_keys(config_with_openai):
    desc = describe_config()
    # Secrets must never appear in describe_config output.
    assert "sk-test-openai" not in str(desc)
    assert desc["providers_configured"]["openai_api"] is True
    assert desc["active_key_source"] == "config.yaml"


def test_env_vars_for():
    assert "ANTHROPIC_API_KEY" in env_vars_for("anthropic_api")
    assert "OPENAI_API_KEY" in env_vars_for("openai_api")
    assert "XAI_API_KEY" in env_vars_for("xai_api")
    assert env_vars_for("bogus") == []


def test_make_client_errors_without_key(isolated_config):
    cfg = load_config()
    with pytest.raises(RuntimeError, match="No API key"):
        make_client(cfg, use_case="review")


def test_unsupported_provider_rejected(isolated_config, monkeypatch):
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", "bogus")
    with pytest.raises(ValueError, match="Unsupported review_provider"):
        load_config()


def test_removed_github_api_provider_rejected(isolated_config, monkeypatch):
    msg = "github_api was removed: GitHub Models is retired; choose another provider"
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "github_api")
    with pytest.raises(ValueError, match=msg):
        load_config()
    # Per-job state sets the provider after load_config, so make_client checks too.
    cfg = LLMConfig(review_provider="github_api")
    with pytest.raises(ValueError, match=msg):
        make_client(cfg, use_case="review")


def test_rate_limit_config_defaults(isolated_config):
    """Default rate-limit config is free-tier-safe."""
    cfg = load_config()
    assert cfg.max_concurrent == 10
    assert cfg.request_delay == 0.0
    assert cfg.max_retries == 2
    assert cfg.retry_base_delay == 5.0


def test_rate_limit_config_from_yaml(isolated_config):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: anthropic_api\n"
        "  model: claude-sonnet\n"
        "  max_concurrent: 8\n"
        "  request_delay: 0.5\n"
        "  max_retries: 5\n"
        "  retry_base_delay: 15.0\n"
        "api_keys:\n"
        "  anthropic_api: sk-test\n"
    )
    cfg = load_config()
    assert cfg.max_concurrent == 8
    assert cfg.request_delay == 0.5
    assert cfg.max_retries == 5
    assert cfg.retry_base_delay == 15.0


@pytest.mark.parametrize("line,expected", [
    ("  max_tokens: 4096\n", 4096),
    ("  max_tokens:\n", 16000),   # empty value
    ("", 16000),                  # key missing
])
def test_review_max_tokens_from_yaml(isolated_config, line, expected):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: anthropic_api\n" + line
    )
    assert load_config().review_max_tokens == expected


def _anthropic_429():
    import anthropic
    import httpx
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError(
        "rate limited", response=httpx.Response(429, request=request), body=None)


@pytest.mark.parametrize("make_exc", [
    _anthropic_429,
    # Copilot SDK errors reach us only as RuntimeError text.
    lambda: RuntimeError("CopilotSDK reported error event(s): session.error: 429 Too Many Requests"),
])
def test_retry_client_retries_on_rate_limit(isolated_config, make_exc):
    """RetryClient should retry on rate-limit exceptions."""
    call_count = 0

    class FakeClient:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise make_exc()
            return "success"

    rc = RetryClient(FakeClient(), max_retries=3, base_delay=0.01)
    result = rc.complete("sys", "user")
    assert result == "success"
    assert call_count == 3


def test_retry_client_raises_after_exhaustion(isolated_config):
    """If all retries are exhausted, the original exception propagates."""
    calls = 0

    class AlwaysFailing:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            nonlocal calls
            calls += 1
            raise RuntimeError("429 rate limit exceeded forever")

    rc = RetryClient(AlwaysFailing(), max_retries=2, base_delay=0.01)
    with pytest.raises(RuntimeError, match="429"):
        rc.complete("sys", "user")
    assert calls == 3


def test_retry_client_does_not_retry_non_rate_errors(isolated_config):
    """Non-rate-limit errors should propagate immediately, not be retried."""
    call_count = 0

    class BadInput:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            nonlocal call_count
            call_count += 1
            raise ValueError("Invalid input")

    rc = RetryClient(BadInput(), max_retries=3, base_delay=0.01)
    import pytest
    with pytest.raises(ValueError, match="Invalid input"):
        rc.complete("sys", "user")
    assert call_count == 1  # no retries on non-rate-limit errors


def test_openai_compatible_works_without_key_when_base_url_set(isolated_config):
    """Ollama / local servers don't need an API key. The code should use a
    placeholder key when openai_compatible_api has a base_url but no api_key."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: llama3.1:8b\n"
        "  base_url: http://localhost:11434/v1\n"
    )
    cfg = load_config()
    assert cfg.review_provider == "openai_compatible_api"
    assert cfg.resolve_api_key("openai_compatible_api") is None
    assert cfg.resolve_base_url("openai_compatible_api") == "http://localhost:11434/v1"

    # probe_providers should show it as configured (green in the UI)
    probed = {p["name"]: p for p in probe_providers(cfg)}
    oc = probed["openai_compatible_api"]
    assert oc["configured"] is True
    assert oc["auth_kind"] == "local_server"
    assert "local server" in oc["key_source"]
    assert oc["base_url"] == "http://localhost:11434/v1"

    # make_client should succeed with a placeholder key
    client = make_client(cfg, use_case="review")
    assert client.model == "llama3.1:8b"


def test_copilot_sdk_in_supported_providers():
    assert "copilot_sdk" in SUPPORTED_PROVIDERS
    assert _PROVIDER_CLASS["copilot_sdk"] is CopilotSDKClient


def test_copilot_sdk_is_keyless(isolated_config):
    """copilot_sdk should be marked local/keyless via is_local_provider."""
    cfg = LLMConfig(review_provider="copilot_sdk")
    assert is_local_provider(cfg, "copilot_sdk") is True


def test_copilot_sdk_probe_when_not_installed(isolated_config, monkeypatch):
    """When the SDK isn't installed, the provider shows as unavailable
    with a helpful unavailable_reason."""
    monkeypatch.setattr(probing, "_copilot_sdk_installed", lambda: False)
    probed = {p["name"]: p for p in probe_providers()}
    assert "copilot_sdk" in probed
    assert probed["copilot_sdk"]["configured"] is False
    assert probed["copilot_sdk"]["auth_kind"] == "sdk_install"
    assert "SDK not installed" in probed["copilot_sdk"]["unavailable_reason"]
    assert "github-copilot-sdk" in probed["copilot_sdk"]["unavailable_reason"]
    # Must NOT suggest an API key is needed
    assert "API key" not in probed["copilot_sdk"]["unavailable_reason"]


def test_copilot_sdk_probe_when_installed(isolated_config, monkeypatch):
    """When the SDK is installed AND a config.yaml exists, the provider
    shows as available.

    The second condition is intentional: the UI policy is that green
    requires both the provider's own creds (SDK install here) AND a
    user-created config.yaml, so the Model page's provider cards stay
    consistent with the missing-config banner in base.html. A fixture
    without a config.yaml would correctly render copilot_sdk red.
    """
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n  provider: copilot_sdk\n  model: copilot-vscode\n"
    )
    monkeypatch.setattr(probing, "_copilot_sdk_installed", lambda: True)
    probed = {p["name"]: p for p in probe_providers()}
    assert probed["copilot_sdk"]["configured"] is True
    assert probed["copilot_sdk"]["auth_kind"] == "sdk_install"
    assert probed["copilot_sdk"]["key_source"] == "Copilot CLI auth"
    assert probed["copilot_sdk"]["unavailable_reason"] == ""


def test_copilot_sdk_client_raises_clear_error_when_sdk_missing(isolated_config, monkeypatch):
    """If the user selects copilot_sdk but doesn't have the SDK installed,
    they should get a helpful ImportError, not a cryptic one."""
    # Ensure the copilot package is not importable
    import sys
    monkeypatch.setitem(sys.modules, "copilot", None)
    import pytest
    with pytest.raises(ImportError, match="copilot"):
        CopilotSDKClient(model="test")


def test_claude_sdk_in_supported_providers():
    assert "claude_sdk" in SUPPORTED_PROVIDERS
    assert _PROVIDER_CLASS["claude_sdk"] is ClaudeSDKClient


def test_claude_sdk_is_keyless(isolated_config):
    """claude_sdk should be marked local/keyless via is_local_provider."""
    cfg = LLMConfig(review_provider="claude_sdk")
    assert is_local_provider(cfg, "claude_sdk") is True


def test_claude_sdk_probe_when_not_installed(isolated_config, monkeypatch):
    """When the SDK isn't installed, the provider shows as unavailable
    with a helpful unavailable_reason."""
    monkeypatch.setattr(probing, "_claude_sdk_installed", lambda: False)
    probed = {p["name"]: p for p in probe_providers()}
    assert "claude_sdk" in probed
    assert probed["claude_sdk"]["configured"] is False
    assert probed["claude_sdk"]["auth_kind"] == "sdk_install"
    assert "SDK not installed" in probed["claude_sdk"]["unavailable_reason"]
    assert "claude-agent-sdk" in probed["claude_sdk"]["unavailable_reason"]
    # Must NOT suggest an API key is needed
    assert "API key" not in probed["claude_sdk"]["unavailable_reason"]


def test_claude_sdk_probe_when_installed(isolated_config, monkeypatch):
    """When the SDK is installed AND a config.yaml exists, the provider
    shows as available. Mirrors the copilot_sdk policy: green requires
    both the SDK install AND a user-created config.yaml so the Model
    page's provider cards stay consistent with the missing-config banner.
    """
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n  provider: claude_sdk\n  model: claude-opus-5-5\n"
    )
    monkeypatch.setattr(probing, "_claude_sdk_installed", lambda: True)
    probed = {p["name"]: p for p in probe_providers()}
    assert probed["claude_sdk"]["configured"] is True
    assert probed["claude_sdk"]["auth_kind"] == "sdk_install"
    assert probed["claude_sdk"]["key_source"] == "Claude Code CLI auth"
    assert probed["claude_sdk"]["unavailable_reason"] == ""


def test_claude_sdk_client_raises_clear_error_when_sdk_missing(isolated_config, monkeypatch):
    """If the user selects claude_sdk but doesn't have the SDK installed,
    they should get a helpful ImportError, not a cryptic one."""
    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    import pytest
    with pytest.raises(ImportError, match="Claude Agent"):
        ClaudeSDKClient(model="test")


# ---------------------------------------------------------------------------
# PDF passthrough: providers that can consume pdf_path directly vs. providers
# that need pre-extracted text.
# ---------------------------------------------------------------------------

def test_provider_supports_pdf_capability_map():
    """The capability set must stay aligned with which clients actually
    implement PDF attachment in their complete() method. Drifting this
    silently would let the reviewer dispatcher send pdf_path to a client
    that ignores it, yielding a review of an empty paper."""
    assert provider_supports_pdf("anthropic_api", None) is True
    assert provider_supports_pdf("openai_api", None) is True
    assert provider_supports_pdf("openai_api", "https://api.openai.com/v1") is True
    # openai_api pointed anywhere else (Azure, proxies) gets extracted text.
    assert provider_supports_pdf(
        "openai_api", "https://r.openai.azure.com/openai/v1/") is False
    # xAI uses its own PDF path (upload /v1/files + Responses API) on
    # agentic models (e.g. grok-4.20), wired up via XaiClient.
    assert provider_supports_pdf("xai_api", "https://api.x.ai/v1") is True
    assert provider_supports_pdf("google_api", None) is True
    assert provider_supports_pdf("claude_sdk", None) is True
    # Text-only providers.
    assert provider_supports_pdf("openai_compatible_api", "https://api.openai.com/v1") is False
    assert provider_supports_pdf("copilot_sdk", None) is False
    assert provider_supports_pdf("bogus", None) is False


def test_retry_client_forwards_pdf_path(isolated_config):
    """RetryClient must pass pdf_path through to the wrapped client —
    otherwise the review pipeline silently drops the PDF on retry."""
    seen: dict = {}

    class CapturingClient:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            seen["pdf_path"] = pdf_path
            return "ok"

    rc = RetryClient(CapturingClient(), max_retries=2, base_delay=0.01)
    rc.complete("sys", "u", pdf_path="/tmp/paper.pdf")
    assert seen["pdf_path"] == "/tmp/paper.pdf"

    # Default path — pdf_path=None must still be forwarded explicitly,
    # not dropped, so the inner client sees a consistent signature.
    seen.clear()
    rc.complete("sys", "u")
    assert seen["pdf_path"] is None


def test_reviewer_dispatch_passes_pdf_for_capable_provider(tmp_path, monkeypatch, isolated_config):
    """When the review provider supports PDF input, _run_single_reviewer
    sends a short user message and forwards pdf_path to the LLM."""
    from ai_paper_review.review.reviewer_dispatching import _run_single_reviewer
    from ai_paper_review.review.reviewer_db import Reviewer

    captured: dict = {}

    class CapturingLLM:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            captured["user"] = user
            captured["pdf_path"] = pdf_path
            # Return a minimal parseable review so _parse_llm_output accepts it.
            return (
                "# Review\n\n"
                "## Comment 1\n"
                "- **Summary:** Test\n"
                "- **Description:** Body.\n"
                "- **Severity:** minor\n"
            )

    reviewer = Reviewer(
        id="R001", persona="Tester", domain="Testing", focus="",
        style="", keywords=[], system_prompt="You are a test reviewer.",
    )
    paper = {"title": "T", "abstract": "A", "full_text": "F" * 20000}

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake\n")
    _run_single_reviewer(reviewer, paper, CapturingLLM(), pdf_path=str(pdf))

    assert captured["pdf_path"] == str(pdf)
    # No paper-body scaffolding on the PDF path — the model reads the
    # paper from the attached PDF.
    assert "Paper body" not in captured["user"]
    assert "attached paper" in captured["user"].lower()
    # The reviewer's persona text (formerly the `system` argument) now
    # lives inside the user message so the provider's prompt cache
    # can share the (system + PDF) prefix across reviewers.
    assert "Your reviewing role for this paper" in captured["user"]
    assert "You are a test reviewer." in captured["user"]
    # Format constraints moved into the shared system prompt — they
    # should NOT appear inline in the user message anymore.
    from ai_paper_review.review.reviewer_dispatching import SHARED_REVIEWER_SYSTEM
    assert "## Comment N" in SHARED_REVIEWER_SYSTEM
    assert "## Comment N" not in captured["user"]


def test_reviewer_dispatch_falls_back_to_text_for_text_only_provider(isolated_config):
    """When pdf_path is None (text-only provider), the full scaffolded
    user message with title/abstract/body is sent."""
    from ai_paper_review.review.reviewer_dispatching import _run_single_reviewer
    from ai_paper_review.review.reviewer_db import Reviewer

    captured: dict = {}

    class CapturingLLM:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            captured["user"] = user
            captured["pdf_path"] = pdf_path
            return (
                "# Review\n\n"
                "## Comment 1\n"
                "- **Summary:** Test\n"
                "- **Description:** Body.\n"
                "- **Severity:** minor\n"
            )

    reviewer = Reviewer(
        id="R001", persona="Tester", domain="Testing", focus="",
        style="", keywords=[], system_prompt="You are a test reviewer.",
    )
    paper = {"title": "Interesting Paper", "abstract": "The abstract.",
             "full_text": "Body text here."}
    _run_single_reviewer(reviewer, paper, CapturingLLM(), pdf_path=None)

    assert captured["pdf_path"] is None
    assert "Paper title: Interesting Paper" in captured["user"]
    assert "Abstract:" in captured["user"]
    assert "Paper body" in captured["user"]
    # The reviewer's persona text now lives in the user message too
    # (same shape on the text path, for consistency with the PDF path).
    assert "Your reviewing role for this paper" in captured["user"]
    assert "You are a test reviewer." in captured["user"]


# ---------------------------------------------------------------------------
# Repair-before-requery: when an LLM returns parseable but empty output,
# prefer a markdown-repair pass on the raw content over a fresh re-query.
# ---------------------------------------------------------------------------

def _make_reviewer_for_repair_tests():
    from ai_paper_review.review.reviewer_db import Reviewer
    return Reviewer(
        id="R001", persona="Tester", domain="Testing", focus="",
        style="", keywords=[], system_prompt="You are a test reviewer.",
    )


def test_clarity_and_persona_reviewers_share_system_prompt(isolated_config, tmp_path):
    """The caching story (N+1 review sessions on one paper share the
    (system + PDF) cached prefix) depends on every session sending the
    EXACT same ``system`` argument. If this test fails, Anthropic /
    OpenAI prompt caching will miss across sessions and large-PDF token
    cost balloons."""
    from ai_paper_review.review.clarity import (
        run_clarity_review,
        CLARITY_REVIEWER_PERSONA,
    )
    from ai_paper_review.review.reviewer_dispatching import (
        SHARED_REVIEWER_SYSTEM, _run_single_reviewer,
    )
    from ai_paper_review.review.reviewer_db import Reviewer

    seen_systems: list = []

    class CapturingLLM:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            seen_systems.append(system)
            return (
                "# Review\n\n## Comment 1\n"
                "- **Summary:** s\n- **Description:** d\n- **Severity:** minor\n"
            )

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-fake\n")
    paper = {"title": "T", "abstract": "A", "full_text": "F"}

    persona_r = Reviewer(
        id="R001", persona="Tester", domain="D", focus="",
        style="", keywords=[], system_prompt="persona-specific content",
    )
    llm = CapturingLLM()

    _run_single_reviewer(persona_r, paper, llm, pdf_path=str(pdf))
    run_clarity_review(paper, llm=llm, pdf_path=str(pdf))

    # Both calls used the shared system prompt, byte-for-byte identical.
    assert len(seen_systems) == 2
    assert seen_systems[0] == SHARED_REVIEWER_SYSTEM
    assert seen_systems[1] == SHARED_REVIEWER_SYSTEM
    assert seen_systems[0] == seen_systems[1]
    # The clarity reviewer's persona name still surfaces — it just
    # lives in the user message now rather than in ``system``.
    # (Captured-user isn't shown here; separate test covers that.)
    # Quick sanity: seen_systems don't leak reviewer-specific content.
    assert "persona-specific content" not in seen_systems[0]
    assert CLARITY_REVIEWER_PERSONA not in seen_systems[0]


def test_google_client_creates_explicit_cache_once_and_reuses(tmp_path):
    """Gemini explicit context cache: first PDF-bearing call creates
    a cache holding (system + PDF); every subsequent call on the same
    pdf_path reuses the cache via ``cached_content=<name>`` so only
    the per-reviewer user text is shipped."""
    from ai_paper_review.llm.clients.google import GoogleClient

    # Fake the google-genai surface: caches.create returns an object
    # with a `.name`; models.generate_content returns `.text`.
    class _FakeCaches:
        def __init__(self):
            self.creates = []

        def create(self, *, model, config):
            self.creates.append({"model": model, "config": config})
            class _C:
                name = "cachedContents/fake-" + str(len(self.creates))
            return _C()

    class _FakeModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, *, model, config, contents):
            self.calls.append({"model": model, "config": config, "contents": contents})
            class _R:
                text = "ok"
            return _R()

    class _FakeClient:
        def __init__(self):
            self.caches = _FakeCaches()
            self.models = _FakeModels()

    # Stub the SDK import so GoogleClient.__init__ succeeds. types are
    # only used for Part.from_bytes (PDF bytes) and config dataclasses;
    # the fake types below provide enough surface for that.
    class _FakeTypes:
        class Part:
            @staticmethod
            def from_bytes(*, data, mime_type):
                return {"pdf_bytes": len(data), "mime": mime_type}

        class GenerateContentConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def __repr__(self):
                return f"GenerateContentConfig({self.kwargs})"

        class CreateCachedContentConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    # Bypass real SDK init.
    gc = GoogleClient.__new__(GoogleClient)
    gc._client = _FakeClient()
    gc._types = _FakeTypes
    gc.model = "gemini-2.5-pro"
    gc._cache_names = {}
    import threading
    gc._cache_lock = threading.Lock()

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n")

    gc.complete("sys", "u1", pdf_path=str(pdf))
    gc.complete("sys", "u2", pdf_path=str(pdf))
    gc.complete("sys", "u3", pdf_path=str(pdf))

    # Cache created exactly once despite three complete() calls.
    assert len(gc._client.caches.creates) == 1
    assert gc._cache_names[str(pdf)] == "cachedContents/fake-1"

    # All three generate_content calls went through the cached path —
    # their config carries cached_content and the (shorter) contents
    # is the per-reviewer user text, not [pdf, user].
    assert len(gc._client.models.calls) == 3
    for c in gc._client.models.calls:
        assert c["config"].kwargs.get("cached_content") == "cachedContents/fake-1"
        # system_instruction should NOT be re-sent — it's in the cache.
        assert "system_instruction" not in c["config"].kwargs
        # Contents is just the user string on the cached path.
        assert isinstance(c["contents"], str)

    # Sanity: the three calls had different user texts.
    assert [c["contents"] for c in gc._client.models.calls] == ["u1", "u2", "u3"]


def test_google_client_falls_back_when_cache_creation_fails(tmp_path):
    """If Gemini refuses to create a cache (e.g. PDF below minimum
    cacheable token count), the client must fall back to the un-cached
    PDF path rather than erroring out — the review is still useful."""
    from ai_paper_review.llm.clients.google import GoogleClient

    class _FailingCaches:
        def __init__(self):
            self.attempts = 0

        def create(self, *, model, config):
            self.attempts += 1
            raise RuntimeError("content too short to cache")

    class _FakeModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, *, model, config, contents):
            self.calls.append({"config": config, "contents": contents})
            class _R:
                text = "ok"
            return _R()

    class _FakeClient:
        def __init__(self):
            self.caches = _FailingCaches()
            self.models = _FakeModels()

    class _FakeTypes:
        class Part:
            @staticmethod
            def from_bytes(*, data, mime_type):
                return {"pdf_bytes": len(data), "mime": mime_type}

        class GenerateContentConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class CreateCachedContentConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    gc = GoogleClient.__new__(GoogleClient)
    gc._client = _FakeClient()
    gc._types = _FakeTypes
    gc.model = "gemini-2.5-flash"
    gc._cache_names = {}
    import threading
    gc._cache_lock = threading.Lock()

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\nshort\n")

    gc.complete("sys", "u1", pdf_path=str(pdf))
    gc.complete("sys", "u2", pdf_path=str(pdf))

    # Cache creation was attempted only ONCE — the None sentinel
    # memoises the failure so we don't ping the API on every call.
    assert gc._client.caches.attempts == 1
    assert gc._cache_names[str(pdf)] is None

    # Both calls fell through to the un-cached PDF path: system
    # re-sent each time, and contents is [pdf_part, user].
    assert len(gc._client.models.calls) == 2
    for c in gc._client.models.calls:
        assert "system_instruction" in c["config"].kwargs
        assert isinstance(c["contents"], list)
        assert len(c["contents"]) == 2  # [pdf_part, user]


def test_openai_client_forwards_prompt_cache_key_for_pdf_calls(tmp_path):
    """With a PDF attached, OpenAI gets a stable ``prompt_cache_key``
    routing hint so every reviewer call on the same paper lands on
    the same backend — OpenAI's automatic prompt cache is server-
    local, so consistent routing maximises cache-hit rate."""
    from ai_paper_review.llm.clients.openai import OpenAIClient

    class _FakeChatCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            class _R:
                class _Choice:
                    class _Msg:
                        content = "ok"
                    message = _Msg()
                    finish_reason = "stop"
                choices = [_Choice()]
            return _R()

    class _FakeChat:
        def __init__(self):
            self.completions = _FakeChatCompletions()

    class _FakeClient:
        def __init__(self):
            self.chat = _FakeChat()

    oc = OpenAIClient(model="gpt-4o", api_key="x")
    oc._client = _FakeClient()

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n")
    oc.complete("sys", "u1", pdf_path=str(pdf))
    oc.complete("sys", "u2", pdf_path=str(pdf))  # same PDF → same key

    calls = oc._client.chat.completions.calls
    assert len(calls) == 2
    extra1 = calls[0].get("extra_body", {})
    extra2 = calls[1].get("extra_body", {})
    assert "prompt_cache_key" in extra1
    assert "prompt_cache_key" in extra2
    # Two calls with the SAME pdf_path must produce the same routing
    # key — this is the whole point of the hint.
    assert extra1["prompt_cache_key"] == extra2["prompt_cache_key"]
    # Without a PDF the hint isn't sent (no benefit; avoids polluting
    # the cache namespace with one-off text calls).
    oc.complete("sys", "text-only")
    assert calls[2].get("extra_body") is None or "prompt_cache_key" not in calls[2]["extra_body"]


def test_openai_pdf_cache_key_differs_across_papers(tmp_path):
    """Different PDFs must get different routing keys, or OpenAI
    would route unrelated reviewer calls to the same backend and
    fight each other for cache slots."""
    from ai_paper_review.llm.clients.openai import _pdf_cache_key
    assert _pdf_cache_key("/tmp/a.pdf") != _pdf_cache_key("/tmp/b.pdf")
    # Same path → same key (stable hash).
    assert _pdf_cache_key("/tmp/a.pdf") == _pdf_cache_key("/tmp/a.pdf")


def test_anthropic_client_streams_above_max_tokens_threshold():
    """The Anthropic SDK refuses non-streaming calls whose expected
    duration exceeds 10 minutes (computed from ``max_tokens`` × slow-
    model throughput). The validator's batch-similarity call can ask
    for 32 K tokens, which trips that ceiling on Opus / extended
    thinking. Above ~16 K the client must route through
    ``messages.stream`` to avoid the error."""
    from ai_paper_review.llm.clients.anthropic import AnthropicClient

    create_calls = []
    stream_calls = []

    class _FakeStreamContext:
        def __init__(self, text):
            self._text = text
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def get_final_message(self):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self._text)],
                stop_reason="end_turn")

    class _FakeMessages:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            class _B:
                type = "text"
                text = "non-stream reply"
            class _M:
                content = [_B()]
                stop_reason = "end_turn"
            return _M()

        def stream(self, **kwargs):
            stream_calls.append(kwargs)
            return _FakeStreamContext("stream-reply")

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    ac = AnthropicClient(model="claude-opus-5-5", api_key="x")
    ac._client = _FakeClient()

    # Low budget → non-streaming create.
    r1 = ac.complete("sys", "u", max_tokens=4000)
    assert r1 == "non-stream reply"
    assert len(create_calls) == 1 and len(stream_calls) == 0

    # High budget → streaming. The final message's text comes back as
    # the full reply, same return-type as the non-streaming path.
    r2 = ac.complete("sys", "u", max_tokens=32000)
    assert r2 == "stream-reply"
    assert len(create_calls) == 1 and len(stream_calls) == 1
    # The streaming call carries the same args as create would have —
    # the switch is about transport, not about what's sent.
    assert stream_calls[0]["max_tokens"] == 32000
    assert stream_calls[0]["system"] == "sys"


def test_anthropic_client_marks_pdf_block_with_cache_control(tmp_path):
    """Anthropic prompt caching requires an explicit ``cache_control``
    marker on the cacheable block. Without it the PDF is re-processed
    on every sibling reviewer call and the whole cache-sharing design
    collapses. Mock the Anthropic SDK and inspect the request payload."""
    from ai_paper_review.llm.clients.anthropic import AnthropicClient

    class _FakeMessages:
        def __init__(self):
            self.last_kwargs = None

        def create(self, **kwargs):
            self.last_kwargs = kwargs
            class _R:
                content = []
                stop_reason = "end_turn"
            return _R()

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    ac = AnthropicClient(model="claude-opus-5-5", api_key="x")
    fake = _FakeClient()
    ac._client = fake

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n")
    ac.complete("sys", "user text", pdf_path=str(pdf))

    msg = fake.messages.last_kwargs["messages"][0]
    blocks = msg["content"]
    # PDF comes first, text comes second.
    assert blocks[0]["type"] == "document"
    assert blocks[1]["type"] == "text"
    # The document block carries the cache-control marker. This is the
    # single line that enables (system + PDF) prefix caching across
    # every reviewer on the same paper.
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}


def test_shared_reviewer_system_prompt_has_critical_rules():
    """The shared reviewer system prompt is what every persona reviewer
    and the clarity reviewer use as their LLM ``system`` argument, so
    the provider's prompt cache can share the (system + PDF) prefix
    across every review session on a paper. Prompt edits that silently
    drop any of these rules will undo the schema tightening — pin the
    content so a future refactor can't regress the rules quietly."""
    from ai_paper_review import prompts as _prompts
    text = _prompts.load("shared_reviewer_system")
    # The opening token the parser hinges on.
    assert "`# Review`" in text
    # Heading-level rules.
    assert "## Comment N" in text
    assert "single `#`" in text
    # Non-empty bullets — these are the fields the parser's
    # `_has_valid_comments` check reads.
    assert "**Summary:**" in text
    assert "**Description:**" in text
    # Categorical constraints on structured fields.
    assert "**Severity:**" in text
    assert "major" in text and "minor" in text
    # The anti-padding directive (biggest source of 0-comment reviews).
    assert "do" in text.lower() and "pad" in text.lower()


def test_call_and_parse_repairs_when_zero_valid_comments(isolated_config):
    """Parse succeeds but zero comments carry summary/description — the
    next LLM call MUST be a markdown-repair pass on the raw output, not
    a fresh review request. If repair returns valid comments, we're done
    in two calls (reviewer + repair)."""
    from ai_paper_review import prompts as _prompts
    from ai_paper_review.review.reviewer_dispatching import _call_and_parse

    repair_system = _prompts.load("markdown_repair_system")
    reviewer = _make_reviewer_for_repair_tests()

    calls: list = []

    class ScriptedLLM:
        model = "scripted"

        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            calls.append({"system": system, "user": user})
            if len(calls) == 1:
                # Parseable markdown, but the comment has no summary /
                # description — e.g. the LLM emitted just a rating
                # template.
                return (
                    "# Review\n\n"
                    "## Comment 1\n\n"
                    "- **Severity:** minor\n"
                )
            # Second call is expected to be the REPAIR — return good output.
            return (
                "# Review\n\n"
                "## Comment 1\n\n"
                "- **Summary:** Good summary from repair.\n"
                "- **Description:** Good description from repair.\n"
                "- **Severity:** minor\n"
            )

    result = _call_and_parse(reviewer, "initial user message", ScriptedLLM())

    # Exactly one initial call + one repair call.
    assert len(calls) == 2
    # First call: the SHARED reviewer system prompt (identical across
    # every reviewer on this paper so the (system + PDF) prefix cache-
    # hits). The per-reviewer persona sits inside the user message.
    from ai_paper_review.review.reviewer_dispatching import SHARED_REVIEWER_SYSTEM
    assert calls[0]["system"] == SHARED_REVIEWER_SYSTEM
    assert calls[0]["user"] == "initial user message"
    # Second call: repair system prompt (NOT the shared reviewer system).
    assert calls[1]["system"] == repair_system
    # Repair's user prompt is the raw output from the first call —
    # that's how we know this was markdown fix-up, not a re-query.
    assert "Comment 1" in calls[1]["user"]
    assert "Severity" in calls[1]["user"]
    # Final parsed result has usable comments.
    from ai_paper_review.review.reviewer_dispatching import _has_valid_comments
    assert _has_valid_comments(result)
    assert "repair" in result["comments"][0]["summary"].lower()
    # Reviewers that went through a repair must be flagged so the
    # final report / web page can count them.
    assert result.get("_format_repaired") is True


def test_node_format_report_stashes_retry_counts_and_ended_at(isolated_config):
    """The review report body doesn't render the LLM / timing /
    format-fix-retries metadata (that lives in the prepended provenance
    block). ``node_format_report`` still tallies the counts and captures
    ``ended_at`` into state so the caller can thread them into
    ``format_provenance``."""
    from ai_paper_review.review.ranking import node_format_report

    state = {
        "paper": {"title": "Sample", "abstract": "abs text"},
        "selected": [],
        "ranked": [],
        "raw_reviews": [
            {"_reviewer_id": "R001", "_persona": "P1", "_domain": "D1",
             "comments": [], "_format_repaired": True},
            {"_reviewer_id": "R002", "_persona": "P2", "_domain": "D2",
             "comments": [], "_format_repaired": False},
            {"_reviewer_id": "R003", "_persona": "P3", "_domain": "D3",
             "comments": [], "_format_repaired": True},
        ],
        "clarity_review": {
            "_reviewer_id": "G001", "_persona": "Writing Clarity Reviewer",
            "_domain": "Writing",
            "comments": [], "_format_repaired": False,
        },
        "launched_at": "2026-04-23T02:35:00+00:00",
    }
    out_state = node_format_report(state)
    md = out_state["report_md"]

    # None of the provenance fields appear in the body — they live only
    # in the prepended ``<!-- provenance -->`` block the caller wraps
    # around this output.
    assert "**LLM:**" not in md
    assert "**Base URL:**" not in md
    assert "**Launched:**" not in md
    assert "**Ended:**" not in md
    assert "Format-fix retries:" not in md

    # Counts + ended_at are stashed in state for the caller.
    # R001 + R003 repaired; R002 + clarity clean ⇒ 2 of 4.
    assert out_state["n_format_repairs"] == 2
    assert out_state["n_reviewers_total"] == 4
    assert out_state["ended_at"]  # non-empty ISO8601 string

    # Body still carries paper metadata + reviewer sections.
    assert "**Title:** Sample" in md
    assert "## Selected Reviewers" in md


def test_call_and_parse_flags_no_repair_on_clean_output(isolated_config):
    """When the LLM returns usable output on the first call, the
    returned dict must carry ``_format_repaired=False`` so the report's
    format-fix tally stays accurate."""
    from ai_paper_review.review.reviewer_dispatching import _call_and_parse

    class CleanLLM:
        model = "clean"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            return (
                "# Review\n\n## Comment 1\n"
                "- **Summary:** s\n- **Description:** d\n- **Severity:** minor\n"
            )

    reviewer = _make_reviewer_for_repair_tests()
    result = _call_and_parse(reviewer, "u", CleanLLM())
    assert result.get("_format_repaired") is False


def test_call_and_parse_repairs_when_parse_fails(isolated_config):
    """Existing behavior preserved: malformed markdown also goes through
    the repair pass rather than being re-queried."""
    from ai_paper_review import prompts as _prompts
    from ai_paper_review.review.reviewer_dispatching import _call_and_parse

    repair_system = _prompts.load("markdown_repair_system")
    reviewer = _make_reviewer_for_repair_tests()

    calls: list = []

    class ScriptedLLM:
        model = "scripted"

        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            calls.append({"system": system, "user": user})
            if len(calls) == 1:
                # Something that won't parse as our review schema.
                return "<html>not markdown at all</html>"
            return (
                "# Review\n\n"
                "## Comment 1\n\n"
                "- **Summary:** Recovered summary.\n"
                "- **Description:** Recovered desc.\n"
                "- **Severity:** minor\n"
            )

    result = _call_and_parse(reviewer, "initial", ScriptedLLM())
    assert len(calls) == 2
    assert calls[1]["system"] == repair_system
    assert "<html>" in calls[1]["user"]
    from ai_paper_review.review.reviewer_dispatching import _has_valid_comments
    assert _has_valid_comments(result)


def test_call_and_parse_raises_on_empty_response(isolated_config):
    """Empty output means the LLM never produced anything — repair won't
    help (no raw to fix). Raise so the caller can surface the failure
    or do a full re-query explicitly."""
    import pytest
    from ai_paper_review.review.reviewer_dispatching import _call_and_parse

    calls: list = []

    class EmptyLLM:
        model = "empty"

        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            calls.append(system)
            return ""

    reviewer = _make_reviewer_for_repair_tests()
    with pytest.raises(ValueError, match="empty response"):
        _call_and_parse(reviewer, "initial", EmptyLLM())
    # Only one call — no repair attempted.
    assert len(calls) == 1


def test_run_single_reviewer_uses_repair_before_full_requery(isolated_config):
    """End-to-end: the outer retry loop should NOT fire a full re-query
    when the first LLM call returned non-empty output. The repair pass
    inside _call_and_parse salvages it in two calls total."""
    from ai_paper_review.review.reviewer_dispatching import _run_single_reviewer

    calls: list = []

    class ScriptedLLM:
        model = "scripted"

        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            calls.append({"system": system, "user": user[:80]})
            if len(calls) == 1:
                return "# Review\n\n## Comment 1\n- **Severity:** minor\n"
            # Repair call returns a usable review — no outer re-query.
            return (
                "# Review\n\n## Comment 1\n"
                "- **Summary:** ok\n"
                "- **Description:** ok\n"
                "- **Severity:** minor\n"
            )

    reviewer = _make_reviewer_for_repair_tests()
    paper = {"title": "T", "abstract": "A", "full_text": "F"}
    result = _run_single_reviewer(reviewer, paper, ScriptedLLM(), pdf_path=None)

    # Exactly two LLM calls: initial + repair. No full re-query.
    assert len(calls) == 2
    # Final review has one good comment.
    assert len(result["comments"]) == 1
    assert result["comments"][0]["summary"] == "ok"


def test_clarity_call_and_parse_repairs_when_zero_valid_comments(isolated_config):
    """Clarity reviewer mirrors the per-reviewer dispatcher's
    repair-before-requery policy."""
    from ai_paper_review import prompts as _prompts
    from ai_paper_review.review.clarity import _call_and_parse as clarity_call_and_parse

    repair_system = _prompts.load("markdown_repair_system")
    calls: list = []

    class ScriptedLLM:
        model = "scripted"

        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            calls.append({"system": system, "user": user})
            if len(calls) == 1:
                return "# Review\n\n## Comment 1\n- **Severity:** minor\n"
            return (
                "# Review\n\n## Comment 1\n"
                "- **Summary:** s\n- **Description:** d\n- **Severity:** minor\n"
            )

    result = clarity_call_and_parse("user msg", ScriptedLLM())
    assert len(calls) == 2
    # Repair call should be flagged by its system prompt.
    assert calls[1]["system"] == repair_system
    assert result["comments"][0]["summary"] == "s"


# ---------------------------------------------------------------------------
# XaiClient — Grok via the OpenAI SDK pointed at api.x.ai, with the
# upload-then-reference PDF flow on the Responses API.
# ---------------------------------------------------------------------------

class _FakeFilesAPI:
    """Stand-in for openai's ``client.files``. Records uploads and
    deletes, returns incrementing file_ids."""

    def __init__(self):
        self.uploads = []
        self.deletions = []
        self._next_id = 1

    def create(self, file, purpose):
        file_id = f"file_fake_{self._next_id}"
        self._next_id += 1
        self.uploads.append({"file_id": file_id, "purpose": purpose,
                             "file_kind": type(file).__name__})
        class _Uploaded:
            pass
        u = _Uploaded()
        u.id = file_id
        return u

    def delete(self, file_id):
        self.deletions.append(file_id)


class _FakeResponsesAPI:
    """Stand-in for openai's ``client.responses``. Captures calls and
    returns a scripted output."""

    def __init__(self, output_text="Response text from Grok"):
        self.calls = []
        self.output_text = output_text

    def create(self, **kwargs):
        self.calls.append(kwargs)
        class _Resp:
            pass
        r = _Resp()
        r.output_text = self.output_text
        return r


class _FakeChatCompletionsAPI:
    """Stand-in for openai's ``client.chat.completions``."""

    def __init__(self, content="Chat text"):
        self.calls = []
        self._content = content

    def create(self, **kwargs):
        self.calls.append(kwargs)
        class _Msg:
            pass
        class _Choice:
            pass
        class _Resp:
            pass
        msg = _Msg(); msg.content = self._content
        ch = _Choice(); ch.message = msg; ch.finish_reason = "stop"
        r = _Resp(); r.choices = [ch]
        return r


class _FakeChatAPI:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAIClient:
    def __init__(self):
        self.files = _FakeFilesAPI()
        self.responses = _FakeResponsesAPI()
        self.chat = _FakeChatAPI(_FakeChatCompletionsAPI())


def _make_xai_with_fake_sdk(model="grok-4.20-reasoning"):
    """Build an XaiClient and swap its internal SDK client for the fakes
    above. The real ``openai.OpenAI(...)`` constructor is still invoked
    during ``__init__`` (openai is installed in the test env), but we
    replace ``self._client`` immediately so no network is touched."""
    from ai_paper_review.llm.clients.xai import XaiClient
    client = XaiClient(model=model, api_key="test-key")
    fake = _FakeOpenAIClient()
    client._client = fake
    return client, fake


def test_xai_client_text_only_uses_chat_completions(isolated_config):
    """Without pdf_path, XaiClient stays on Chat Completions — same path
    as the OpenAI-compatible client."""
    client, fake = _make_xai_with_fake_sdk()
    fake.chat.completions._content = "text reply"
    result = client.complete("sys prompt", "user msg")
    assert result == "text reply"
    # One chat call, zero responses/files traffic.
    assert len(fake.chat.completions.calls) == 1
    assert len(fake.responses.calls) == 0
    assert len(fake.files.uploads) == 0
    # Messages shape matches the shared OpenAIClient pattern.
    call = fake.chat.completions.calls[0]
    assert call["model"] == "grok-4.20-reasoning"
    assert call["messages"][0] == {"role": "system", "content": "sys prompt"}
    assert call["messages"][1] == {"role": "user", "content": "user msg"}


def test_xai_client_pdf_uses_responses_api_with_file_upload(isolated_config, tmp_path):
    """With pdf_path, XaiClient uploads the PDF, then calls Responses
    API with an input_file content block and the system prompt in
    ``instructions``."""
    client, fake = _make_xai_with_fake_sdk()
    fake.responses.output_text = "Grok's review of the paper"

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake\n")

    result = client.complete("You are a reviewer.", "Review this.", pdf_path=str(pdf))
    assert result == "Grok's review of the paper"

    # Exactly one upload, one responses call, zero chat-completions.
    assert len(fake.files.uploads) == 1
    assert fake.files.uploads[0]["purpose"] == "assistants"
    assert len(fake.responses.calls) == 1
    assert len(fake.chat.completions.calls) == 0

    # Inspect the Responses request shape.
    call = fake.responses.calls[0]
    assert call["model"] == "grok-4.20-reasoning"
    # System prompt rides on top-level ``instructions`` (not inside ``input``).
    assert call["instructions"] == "You are a reviewer."
    # ``tools`` must NOT be passed — xAI auto-activates attachment_search.
    assert "tools" not in call
    # The input content mixes input_text + input_file, in that order.
    content = call["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "Review this."}
    assert content[1] == {"type": "input_file", "file_id": "file_fake_1"}
    # Budget uses the Responses-API name, not ``max_tokens``.
    assert "max_output_tokens" in call
    assert "max_tokens" not in call


def test_xai_client_reuses_cached_file_id_across_calls(isolated_config, tmp_path):
    """N parallel reviewers against the same PDF must upload it once,
    not N times — otherwise we'd burn xAI storage and wall-time."""
    client, fake = _make_xai_with_fake_sdk()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    # Three back-to-back complete() calls with the same pdf_path.
    for _ in range(3):
        client.complete("sys", "user", pdf_path=str(pdf))

    # Upload only once; three Responses calls, all referencing the same file_id.
    assert len(fake.files.uploads) == 1
    assert len(fake.responses.calls) == 3
    refs = [c["input"][0]["content"][1]["file_id"] for c in fake.responses.calls]
    assert refs == ["file_fake_1", "file_fake_1", "file_fake_1"]


def test_xai_client_cleanup_deletes_uploaded_files(isolated_config, tmp_path):
    """cleanup_uploaded_files() must delete everything the client
    uploaded so long-lived processes don't accumulate storage."""
    client, fake = _make_xai_with_fake_sdk()
    pdf1 = tmp_path / "a.pdf"; pdf1.write_bytes(b"%PDF-1.4\n")
    pdf2 = tmp_path / "b.pdf"; pdf2.write_bytes(b"%PDF-1.4\n")

    client.complete("sys", "user", pdf_path=str(pdf1))
    client.complete("sys", "user", pdf_path=str(pdf2))
    assert len(fake.files.uploads) == 2

    client.cleanup_uploaded_files()
    assert sorted(fake.files.deletions) == ["file_fake_1", "file_fake_2"]
    # Cache is cleared, so a subsequent call re-uploads.
    client.complete("sys", "user", pdf_path=str(pdf1))
    assert len(fake.files.uploads) == 3


def test_factory_wires_xai_to_XaiClient(isolated_config):
    """The factory must hand back an XaiClient for provider=xai so the
    PDF flow actually reaches the Responses API."""
    from ai_paper_review.llm.clients.xai import XaiClient
    from ai_paper_review.llm.config import LLMConfig
    from ai_paper_review.llm.factory import make_client

    cfg = LLMConfig(review_provider="xai_api",
                    review_model="grok-4.20-reasoning",
                    api_keys={"xai_api": "test-key"})
    client = make_client(cfg, use_case="review")
    # RetryClient wraps the raw client; unwrap to check the concrete type.
    inner = getattr(client, "_inner", client)
    assert isinstance(inner, XaiClient)


# ---------------------------------------------------------------------------
# claude_sdk: subprocess env, fatal errors, request delay
# ---------------------------------------------------------------------------


def _stub_claude_query(monkeypatch, messages=(), exc=None):
    """Replace claude_agent_sdk.query with a stub; return the list that
    collects the options each call received."""
    import claude_agent_sdk
    seen = []

    async def fake_query(prompt, options):
        seen.append(options)
        if exc is not None:
            raise exc
        for m in messages:
            yield m

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return seen


def test_claude_sdk_client_hides_api_key_from_cli(isolated_config, monkeypatch):
    """An exported ANTHROPIC_API_KEY must not reach the CLI subprocess
    (it would win over the OAuth login), and must stay set for this
    process so anthropic_api keeps working."""
    import os
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    seen = _stub_claude_query(monkeypatch, [SimpleNamespace(content=[SimpleNamespace(text="ok")])])

    assert ClaudeSDKClient(model="m").complete("sys", "user") == "ok"
    assert seen[0].env["ANTHROPIC_API_KEY"] == ""
    assert seen[0].setting_sources == []
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-real"


@pytest.mark.parametrize("kind", ["hard_limit", "cli_missing", "auth_failed"])
def test_claude_sdk_client_raises_fatal_error(isolated_config, monkeypatch, kind):
    from claude_agent_sdk import CLINotFoundError
    from ai_paper_review.llm.clients.base import FatalLLMError

    resets_at = int(time.time()) + 3600
    if kind == "hard_limit":
        info = SimpleNamespace(status="rejected", resets_at=resets_at,
                               rate_limit_type="five_hour")
        _stub_claude_query(monkeypatch, [SimpleNamespace(rate_limit_info=info)])
    elif kind == "cli_missing":
        _stub_claude_query(monkeypatch, exc=CLINotFoundError())
    else:
        _stub_claude_query(monkeypatch, [SimpleNamespace(
            content=[SimpleNamespace(text="Invalid API key")], error="authentication_failed")])

    with pytest.raises(FatalLLMError) as ei:
        ClaudeSDKClient(model="m").complete("sys", "user")
    if kind == "hard_limit":
        when = datetime.fromtimestamp(resets_at).astimezone()
        assert when.strftime("%Y-%m-%d %H:%M %Z") in str(ei.value)


@pytest.mark.parametrize("message, retried", [
    # Rejected, but the limit resets within 5 minutes.
    (SimpleNamespace(rate_limit_info=SimpleNamespace(
        status="rejected", resets_at=int(time.time()) + 60)), True),
    (SimpleNamespace(content=[SimpleNamespace(text="API Error: 429")],
                     error="rate_limit"), True),
    (SimpleNamespace(content=[SimpleNamespace(text="API Error: 529")],
                     error="server_error"), True),
    (SimpleNamespace(content=[SimpleNamespace(text="API Error: 400")],
                     error="invalid_request"), False),
    (SimpleNamespace(is_error=True, subtype="error_max_turns",
                     errors=None, result=None), False),
])
def test_claude_sdk_client_raises_on_cli_errors(isolated_config, monkeypatch,
                                                message, retried):
    """CLI error messages raise instead of being returned as the review,
    and only rate-limit / server errors are retried."""
    from ai_paper_review.llm.clients.base import FatalLLMError
    from ai_paper_review.llm.clients import claude
    from ai_paper_review.llm.retrying import _is_rate_limit_error

    async def no_sleep(s):
        pass
    monkeypatch.setattr(claude.asyncio, "sleep", no_sleep)
    _stub_claude_query(monkeypatch, [message])
    with pytest.raises(RuntimeError) as ei:
        ClaudeSDKClient(model="m").complete("sys", "user")
    assert not isinstance(ei.value, FatalLLMError)
    assert _is_rate_limit_error(ei.value) is retried


def test_claude_sdk_client_returns_text_after_last_tool_use(isolated_config,
                                                            monkeypatch):
    _stub_claude_query(monkeypatch, [
        SimpleNamespace(content=[SimpleNamespace(text="I will read it."),
                                 SimpleNamespace(name="Read", input={})]),
        SimpleNamespace(content=[SimpleNamespace(text="## Review")]),
        SimpleNamespace(is_error=False, result="## Review"),
    ])
    assert ClaudeSDKClient(model="m").complete("sys", "user") == "## Review"


def test_claude_sdk_client_limits_tools(isolated_config, monkeypatch, tmp_path):
    """Text calls get no tools; PDF calls get only Read, on a copy of the
    PDF in a temporary directory that is removed after the call."""
    from pathlib import Path
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    seen = _stub_claude_query(
        monkeypatch, [SimpleNamespace(content=[SimpleNamespace(text="ok")])])
    client = ClaudeSDKClient(model="m")
    client.complete("sys", "user")
    client.complete("sys", "user", pdf_path=str(pdf))

    text_opts, pdf_opts = seen
    assert text_opts.tools == []
    assert pdf_opts.tools == ["Read"]
    assert pdf_opts.permission_mode == "dontAsk"
    assert not pdf_opts.allowed_tools
    assert pdf_opts.add_dirs == [pdf_opts.cwd]
    assert "no-session-persistence" in pdf_opts.extra_args
    assert not Path(pdf_opts.cwd).exists()


def test_retry_client_does_not_retry_fatal_error(isolated_config):
    from ai_paper_review.llm.clients.base import FatalLLMError
    calls = 0

    class Fatal:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            nonlocal calls
            calls += 1
            raise FatalLLMError("usage limit reached (rate limit, 429)")

    with pytest.raises(FatalLLMError):
        RetryClient(Fatal(), max_retries=3, base_delay=0.01).complete("s", "u")
    assert calls == 1


class _CountingLLM:
    model = "fake"

    def __init__(self, exc=None):
        self.calls = 0
        self.exc = exc

    def complete(self, system, user, max_tokens=4000, pdf_path=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return ""


_PAPER = {"title": "T", "abstract": "A", "full_text": "Body"}


def _run_persona_reviewer(llm):
    from ai_paper_review.review.reviewer_db import Reviewer
    from ai_paper_review.review.reviewer_dispatching import _run_single_reviewer
    r = Reviewer(id="R1", persona="p", domain="d", focus="f", style="s",
                 keywords=[], system_prompt="be a reviewer")
    return _run_single_reviewer(r, _PAPER, llm)


def _run_clarity_reviewer(llm):
    from ai_paper_review.review.clarity import run_clarity_review
    return run_clarity_review(_PAPER, llm=llm)


@pytest.mark.parametrize("run", [_run_persona_reviewer, _run_clarity_reviewer])
def test_review_loops_stop_on_fatal_error(isolated_config, run):
    from ai_paper_review.llm.clients.base import FatalLLMError
    llm = _CountingLLM(exc=FatalLLMError("not logged in"))
    with pytest.raises(FatalLLMError):
        run(llm)
    assert llm.calls == 1


def test_persona_reviewer_does_not_requery_call_errors(isolated_config):
    llm = _CountingLLM(exc=RuntimeError("401 invalid x-api-key"))
    assert "401" in _run_persona_reviewer(llm)["error"]
    assert llm.calls == 1


_VALID_REVIEW = ("# Review\n\n## Comment 1\n"
                 "- **Summary:** s\n- **Description:** d\n- **Severity:** minor\n")


def test_clarity_reviewer_reruns_after_call_error(isolated_config, monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    class FlakyLLM(_CountingLLM):
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("connection reset")
            return _VALID_REVIEW

    llm = FlakyLLM()
    result = _run_clarity_reviewer(llm)
    assert result["comments"][0]["summary"] == "s"
    assert "error" not in result
    assert llm.calls == 2
    assert len(sleeps) == 1


def test_clarity_reviewer_fails_run_when_every_attempt_errors(
        isolated_config, monkeypatch):
    from ai_paper_review.review.constants import CLARITY_RERUNS
    monkeypatch.setattr("time.sleep", lambda s: None)
    llm = _CountingLLM(exc=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError, match="Clarity reviewer failed"):
        _run_clarity_reviewer(llm)
    assert llm.calls == CLARITY_RERUNS + 1


def test_node_run_reviewers_cancels_queued_work_on_fatal_error(
        isolated_config, monkeypatch):
    from ai_paper_review.llm.clients.base import FatalLLMError
    from ai_paper_review.review import reviewer_dispatching as rd
    from ai_paper_review.review.reviewer_db import Reviewer
    llm = _CountingLLM(exc=FatalLLMError("not logged in"))
    monkeypatch.setattr(rd, "make_client", lambda cfg, use_case: llm)
    monkeypatch.setattr(rd, "load_config", lambda: LLMConfig(
        review_provider="anthropic_api", max_concurrent=1))
    selected = [(Reviewer(id=f"R{i}", persona="p", domain="d", focus="f",
                          style="s", keywords=[], system_prompt="x"), 1.0)
                for i in range(5)]
    with pytest.raises(FatalLLMError):
        rd.node_run_reviewers({"selected": selected, "paper": _PAPER})
    # One worker: at most the failing call and the one it picked up next.
    assert llm.calls <= 2


def test_copilot_sdk_errors_are_wrapped_as_runtime_error(isolated_config):
    from ai_paper_review.llm.retrying import _is_rate_limit_error

    class JsonRpcError(Exception):
        pass

    class FakeSDKClient:
        async def start(self):
            pass

        async def create_session(self, **kw):
            raise JsonRpcError("429 Too Many Requests")

        async def stop(self):
            pass

    client = CopilotSDKClient.__new__(CopilotSDKClient)
    client._SDKClient = FakeSDKClient
    client.model = "gpt-5"
    with pytest.raises(RuntimeError) as ei:
        client.complete("sys", "user")
    assert isinstance(ei.value.__cause__, JsonRpcError)
    assert _is_rate_limit_error(ei.value)


def test_persona_loop_retries_empty_output(isolated_config):
    from ai_paper_review.review.constants import EMPTY_COMMENT_RETRIES
    llm = _CountingLLM()
    assert _run_persona_reviewer(llm)["comments"] == []
    assert llm.calls == EMPTY_COMMENT_RETRIES + 1


def test_clarity_empty_output_fails_run_without_rerun(isolated_config):
    from ai_paper_review.review.constants import EMPTY_COMMENT_RETRIES
    llm = _CountingLLM()
    with pytest.raises(RuntimeError, match="no valid comments"):
        _run_clarity_reviewer(llm)
    assert llm.calls == EMPTY_COMMENT_RETRIES + 1


def test_request_delay_has_floor_for_claude_sdk_on_any_stage(isolated_config):
    cfg = LLMConfig(review_provider="anthropic_api",
                    validation_provider="claude_sdk", request_delay=0.0)
    assert cfg.request_delay_for(cfg.resolve_provider("review")) == 0.0
    assert cfg.request_delay_for(cfg.resolve_provider("validation")) == 1.0
    cfg.request_delay = 2.5
    assert cfg.request_delay_for("claude_sdk") == 2.5


@pytest.mark.parametrize("logged_in", [True, False])
def test_claude_sdk_probe_checks_cli_login(isolated_config, monkeypatch, logged_in):
    import json
    import subprocess
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setattr(probing, "_CLAUDE_SDK_READY", False)
    monkeypatch.setattr(probing, "_CLAUDE_SDK_FAILED_AT", float("-inf"))
    monkeypatch.setattr(probing, "_claude_cli_path", lambda: "/fake/claude")
    seen_env = {}

    def fake_run(cmd, **kw):
        seen_env.update(kw["env"])
        out = json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai"})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(probing.subprocess, "run", fake_run)
    assert probing._claude_sdk_installed() is logged_in
    assert seen_env["ANTHROPIC_API_KEY"] == ""


def test_ingest_pdf_uses_job_provider_and_model(isolated_config, monkeypatch):
    """node_ingest_pdf must build its client for the per-job provider and
    model, not the config.yaml default."""
    from ai_paper_review.llm import factory
    from ai_paper_review.review import review
    seen = {}

    def fake_make_client(cfg, use_case="default"):
        seen["provider"] = cfg.resolve_provider(use_case)
        seen["model"] = cfg.resolve_model(use_case)
        return object()

    monkeypatch.setattr(factory, "make_client", fake_make_client)
    monkeypatch.setattr(review, "extract_pdf_for_provider", lambda p, prov: "text " * 200)
    monkeypatch.setattr(review, "extract_paper_summary_llm",
                        lambda text, llm: {"title": "T"})
    review.node_ingest_pdf({"pdf_path": "x.pdf", "llm_provider": "claude_sdk",
                            "llm_model": "claude-opus-x"})
    assert seen == {"provider": "claude_sdk", "model": "claude-opus-x"}


def test_node_run_reviewers_raises_only_when_every_reviewer_fails(isolated_config, monkeypatch):
    import pytest
    from ai_paper_review.review import reviewer_dispatching as rd
    from ai_paper_review.review.reviewer_db import Reviewer

    class FakeLLM:
        model = "fake"

    monkeypatch.setattr(rd, "make_client", lambda cfg, use_case: FakeLLM())
    monkeypatch.setattr(rd._time, "sleep", lambda s: None)

    def reviewer(rid):
        return Reviewer(id=rid, persona="P", domain="D", focus="", style="",
                        keywords=[], system_prompt="")

    def fail(r, paper, llm, pdf_path, max_tokens):
        return {"_reviewer_id": r.id, "_persona": r.persona, "_domain": r.domain,
                "error": "401 invalid x-api-key", "comments": []}

    state = {"selected": [(reviewer("R1"), 1.0), (reviewer("R2"), 1.0)],
             "paper": {}}
    monkeypatch.setattr(rd, "_run_single_reviewer", fail)
    with pytest.raises(RuntimeError, match="401 invalid x-api-key"):
        rd.node_run_reviewers(dict(state))

    def one_ok(r, paper, llm, pdf_path, max_tokens):
        if r.id == "R1":
            return {"_reviewer_id": r.id, "_persona": r.persona,
                    "_domain": r.domain, "comments": [{"summary": "s"}]}
        return fail(r, paper, llm, pdf_path, max_tokens)

    monkeypatch.setattr(rd, "_run_single_reviewer", one_ok)
    out = rd.node_run_reviewers(dict(state))
    assert len(out["all_comments"]) == 1


def test_claude_sdk_still_has_find_cli():
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )
    assert callable(getattr(SubprocessCLITransport, "_find_cli", None))


def test_claude_cli_path_falls_back_to_which(monkeypatch):
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    def broken(self):
        raise AttributeError("_find_cli removed")

    monkeypatch.setattr(SubprocessCLITransport, "_find_cli", broken)
    monkeypatch.setattr("shutil.which", lambda name: f"/which/{name}")
    assert probing._claude_cli_path() == "/which/claude"


# ---------------------------------------------------------------------------
# Provider request format: PDF routing, token parameter, Copilot session
# settings, and empty-reply errors.
# ---------------------------------------------------------------------------

_AZURE_URL = "https://r.openai.azure.com/openai/v1/"


class _CapturingReviewLLM:
    model = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens=4000, pdf_path=None):
        self.calls.append({"user": user, "pdf_path": pdf_path,
                           "max_tokens": max_tokens})
        return _VALID_REVIEW


def _node_persona(monkeypatch, cfg, llm, pdf):
    from ai_paper_review.review import reviewer_dispatching as rd
    from ai_paper_review.review.reviewer_db import Reviewer
    monkeypatch.setattr(rd, "make_client", lambda cfg, use_case: llm)
    monkeypatch.setattr(rd, "load_config", lambda: cfg)
    r = Reviewer(id="R1", persona="p", domain="d", focus="f", style="s",
                 keywords=[], system_prompt="x")
    rd.node_run_reviewers({"selected": [(r, 1.0)], "paper": _PAPER, "pdf_path": pdf})


def _node_clarity(monkeypatch, cfg, llm, pdf):
    from ai_paper_review.review import clarity
    monkeypatch.setattr(clarity, "make_client", lambda cfg, use_case: llm)
    monkeypatch.setattr(clarity, "load_config", lambda: cfg)
    clarity.node_run_clarity_review({"paper": _PAPER, "pdf_path": pdf})


@pytest.mark.parametrize("node", [_node_persona, _node_clarity])
@pytest.mark.parametrize("base_url,expect_pdf", [(None, True), (_AZURE_URL, False)])
def test_openai_api_pdf_routing_follows_base_url(
        isolated_config, monkeypatch, tmp_path, node, base_url, expect_pdf):
    pdf = str(tmp_path / "p.pdf")
    cfg = LLMConfig(review_provider="openai_api", review_base_url=base_url,
                    max_concurrent=1)
    llm = _CapturingReviewLLM()
    node(monkeypatch, cfg, llm, pdf)
    call = llm.calls[0]
    if expect_pdf:
        assert call["pdf_path"] == pdf
        assert "Body" not in call["user"]
    else:
        assert call["pdf_path"] is None
        assert "Body" in call["user"]


class _RepairedReviewLLM(_CapturingReviewLLM):
    """The first reply has no usable comment, so the second call is the
    markdown repair."""

    def complete(self, system, user, max_tokens=4000, pdf_path=None):
        reply = super().complete(system, user, max_tokens, pdf_path)
        if len(self.calls) == 1:
            return "# Review\n\n## Comment 1\n- **Severity:** minor\n"
        return reply


@pytest.mark.parametrize("node", [_node_persona, _node_clarity])
def test_review_calls_request_configured_max_tokens(
        isolated_config, monkeypatch, node):
    cfg = LLMConfig(review_provider="openai_compatible_api", max_concurrent=1,
                    review_max_tokens=4096)
    llm = _RepairedReviewLLM()
    node(monkeypatch, cfg, llm, None)
    # The review call, then its markdown-repair call.
    assert [c["max_tokens"] for c in llm.calls] == [4096, 4096]


class _FakeChat:
    def __init__(self, content="ok", finish_reason="stop"):
        self.calls = []
        self.completions = self
        self.chat = self
        self._reply = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason=finish_reason)])

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._reply


@pytest.mark.parametrize("provider,base_url,token_key", [
    ("openai_api", None, "max_completion_tokens"),
    ("openai_api", "https://api.openai.com/v1", "max_completion_tokens"),
    ("openai_api", _AZURE_URL, "max_completion_tokens"),
    ("openai_api", "http://litellm.internal:4000/v1", "max_tokens"),
    ("openai_compatible_api", "http://localhost:11434/v1", "max_tokens"),
    ("xai_api", None, "max_tokens"),
])
def test_openai_family_token_parameter_per_provider(
        isolated_config, provider, base_url, token_key):
    cfg = LLMConfig(review_provider=provider, review_base_url=base_url,
                    api_keys={provider: "k"})
    client = make_client(cfg, use_case="review")
    fake = _FakeChat()
    client._inner._client = fake
    client.complete("s", "u", max_tokens=1234)
    call = fake.calls[0]
    assert call[token_key] == 1234
    other = {"max_tokens", "max_completion_tokens"} - {token_key}
    assert not other & call.keys()


def test_openai_client_attaches_pdf_only_for_openai_endpoint(isolated_config, tmp_path):
    from ai_paper_review.llm.clients.openai import OpenAIClient
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    for base_url, attached in [(None, True), (_AZURE_URL, False)]:
        oc = OpenAIClient(model="gpt-4o", api_key="x", base_url=base_url)
        fake = _FakeChat()
        oc._client = fake
        oc.complete("s", "u", pdf_path=str(pdf))
        content = fake.calls[0]["messages"][1]["content"]
        assert isinstance(content, list) is attached


def test_openai_client_raises_when_budget_used_up_with_no_text(isolated_config):
    from ai_paper_review.llm.clients.openai import OpenAIClient
    from ai_paper_review.llm.retrying import _is_rate_limit_error
    oc = OpenAIClient(model="o3", api_key="x")
    from ai_paper_review.llm.clients.base import ReplyBlockedError
    oc._client = _FakeChat(content="", finish_reason="length")
    with pytest.raises(ReplyBlockedError, match="max_tokens=50") as ei:
        oc.complete("s", "u", max_tokens=50)
    assert not _is_rate_limit_error(ei.value)
    # Truncated but non-empty output is still returned.
    oc._client = _FakeChat(content="partial", finish_reason="length")
    assert oc.complete("s", "u") == "partial"


def test_copilot_session_is_locked_down(isolated_config):
    import os
    seen = {}

    class FakeSession:
        def on(self, handler):
            self._handler = handler

        async def send(self, prompt):
            seen["prompt"] = prompt
            ev = lambda t, **d: SimpleNamespace(
                type=SimpleNamespace(value=t), data=SimpleNamespace(**d))
            self._handler(ev("assistant.message_delta", delta_content="review"))
            self._handler(ev("session.idle"))

    class FakeSDKClient:
        async def start(self):
            pass

        async def create_session(self, **kw):
            seen.update(kw)
            seen["cwd_existed"] = os.path.isdir(kw["working_directory"])
            return FakeSession()

        async def stop(self):
            pass

    client = CopilotSDKClient.__new__(CopilotSDKClient)
    client._SDKClient = FakeSDKClient
    client.model = "gpt-5"
    assert client.complete("sys text", "user text") == "review"

    assert seen["model"] == "gpt-5"
    assert seen["system_message"] == {"mode": "append", "content": "sys text"}
    assert seen["prompt"] == "user text"
    assert seen["available_tools"] == []
    assert seen["cwd_existed"]
    assert not os.path.exists(seen["working_directory"])
    decision = seen["on_permission_request"](SimpleNamespace(kind="shell"), {})
    assert decision.kind == "reject" or decision.kind.startswith("denied")


@pytest.mark.parametrize("fail", [False, True])
def test_copilot_session_is_deleted_from_disk(isolated_config, fail):
    calls = []

    class FakeSession:
        session_id = "sid-1"

        def on(self, handler):
            self._handler = handler

        async def send(self, prompt):
            ev = lambda t, **d: SimpleNamespace(
                type=SimpleNamespace(value=t), data=SimpleNamespace(**d))
            if fail:
                self._handler(ev("session.error", message="boom"))
            else:
                self._handler(ev("assistant.message_delta", delta_content="ok"))
                self._handler(ev("session.idle"))

        async def __aexit__(self, *exc):
            calls.append("disconnect")

    class FakeSDKClient:
        async def start(self):
            pass

        async def create_session(self, **kw):
            return FakeSession()

        async def delete_session(self, session_id):
            calls.append(("delete", session_id))
            if fail:
                raise RuntimeError("delete failed")

        async def stop(self):
            calls.append("stop")

    client = CopilotSDKClient.__new__(CopilotSDKClient)
    client._SDKClient = FakeSDKClient
    client.model = "gpt-5"
    if fail:
        with pytest.raises(RuntimeError, match="boom"):
            client.complete("s", "u")
    else:
        assert client.complete("s", "u") == "ok"
    assert calls == ["disconnect", ("delete", "sid-1"), "stop"]


def _fake_gemini(resp):
    import threading
    from ai_paper_review.llm.clients.google import GoogleClient
    from google.genai import types as gtypes
    gc = GoogleClient.__new__(GoogleClient)
    gc._client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kw: resp))
    gc._types = gtypes
    gc.model = "gemini-2.5-pro"
    gc._cache_names = {}
    gc._cache_lock = threading.Lock()
    return gc


def _gemini_resp(text=None, block=None, finish=None):
    return SimpleNamespace(
        text=text,
        prompt_feedback=SimpleNamespace(block_reason=block) if block else None,
        candidates=[SimpleNamespace(finish_reason=finish)] if finish else None,
    )


@pytest.mark.parametrize("resp_kw,match", [
    ({"block": "SAFETY"}, "blocked"),
    ({"finish": "SAFETY", "text": "partial"}, "SAFETY"),
    ({"finish": "MAX_TOKENS"}, "max_tokens=77"),
])
def test_google_client_raises_on_blocked_or_empty_reply(isolated_config, resp_kw, match):
    from google.genai import types as gtypes
    from ai_paper_review.llm.retrying import _is_rate_limit_error
    if "block" in resp_kw:
        resp_kw["block"] = gtypes.BlockedReason(resp_kw["block"])
    if "finish" in resp_kw:
        resp_kw["finish"] = gtypes.FinishReason(resp_kw["finish"])
    from ai_paper_review.llm.clients.base import ReplyBlockedError
    gc = _fake_gemini(_gemini_resp(**resp_kw))
    with pytest.raises(ReplyBlockedError, match=match) as ei:
        gc.complete("s", "u", max_tokens=77)
    assert not _is_rate_limit_error(ei.value)


def test_google_client_returns_truncated_text(isolated_config):
    from google.genai import types as gtypes
    gc = _fake_gemini(_gemini_resp(text=" cut ", finish=gtypes.FinishReason.MAX_TOKENS))
    assert gc.complete("s", "u") == "cut"


def test_title_extraction_budget_leaves_room_for_thinking(isolated_config):
    from ai_paper_review.review.pdf_ingestion import extract_paper_summary_llm
    seen = {}

    class LLM:
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            seen["max_tokens"] = max_tokens
            return "Title: T\nAbstract: A"

    extract_paper_summary_llm("text", LLM())
    assert seen["max_tokens"] >= 4000


def test_default_review_model_is_current_sonnet(isolated_config):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n  provider: anthropic_api\n")
    assert load_config().resolve_model("review") == "claude-sonnet-5-5"
    assert LLMConfig().review_model == "claude-sonnet-5-5"
    import inspect
    default = inspect.signature(ClaudeSDKClient).parameters["model"].default
    assert default == "claude-sonnet-5-5"


def _fake_anthropic(reply):
    from ai_paper_review.llm.clients.anthropic import AnthropicClient

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return reply

    ac = AnthropicClient(model="claude-sonnet-5-5", api_key="x")
    ac._client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: reply, stream=lambda **kw: _Stream()))
    return ac


def _anthropic_reply(text, stop_reason):
    blocks = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


@pytest.mark.parametrize("max_tokens", [77, 32000])   # create and stream paths
@pytest.mark.parametrize("text,stop_reason,match", [
    ("", "refusal", "refused"),
    ("partial", "refusal", "refused"),
    ("", "max_tokens", "max_tokens="),
])
def test_anthropic_client_raises_on_refusal_or_empty_budget(
        isolated_config, max_tokens, text, stop_reason, match):
    from ai_paper_review.llm.retrying import _is_rate_limit_error
    from ai_paper_review.llm.clients.base import ReplyBlockedError
    ac = _fake_anthropic(_anthropic_reply(text, stop_reason))
    with pytest.raises(ReplyBlockedError, match=match) as ei:
        ac.complete("s", "u", max_tokens=max_tokens)
    assert not _is_rate_limit_error(ei.value)


@pytest.mark.parametrize("max_tokens", [77, 32000])
def test_anthropic_client_returns_truncated_text(isolated_config, max_tokens):
    ac = _fake_anthropic(_anthropic_reply("cut", "max_tokens"))
    assert ac.complete("s", "u", max_tokens=max_tokens) == "cut"


@pytest.mark.parametrize("provider,base_url,local", [
    ("claude_sdk", None, True),
    ("copilot_sdk", None, True),
    ("openai_compatible_api", "http://localhost:11434/v1", True),
    ("openai_compatible_api", "http://127.0.0.1:8000/v1", True),
    ("openai_compatible_api", "http://[::1]:8000/v1", True),
    ("openai_compatible_api", "http://0.0.0.0:8000/v1", True),
    ("openai_compatible_api", "http://gpu-box.local:8000/v1", True),
    ("openai_compatible_api", "http://192.168.1.5:11434/v1", True),
    ("openai_compatible_api", "http://10.0.0.2:8000/v1", True),
    ("openai_compatible_api", "http://172.16.3.4:8000/v1", True),
    ("openai_compatible_api", "http://host.docker.internal:11434/v1", True),
    ("openai_compatible_api", "http://8.8.8.8:8000/v1", False),
    ("openai_compatible_api", "http://172.32.0.1:8000/v1", False),
    ("openai_compatible_api", "https://api.together.xyz/v1", False),
    ("openai_compatible_api", "http://localhost.example.com/v1", False),
    ("openai_compatible_api", None, False),
    ("anthropic_api", "http://localhost:8080", False),
])
def test_is_local_url_only_for_keyless_sdks_and_local_hosts(provider, base_url, local):
    from ai_paper_review.llm.utils import _is_local_url
    assert _is_local_url(provider, base_url) is local


def test_remote_openai_compatible_without_key_is_not_keyless(isolated_config):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: llama-3\n"
        "  base_url: https://api.together.xyz/v1\n"
    )
    cfg = load_config()
    assert is_local_provider(cfg, "openai_compatible_api") is False
    oc = {p["name"]: p for p in probe_providers(cfg)}["openai_compatible_api"]
    assert oc["configured"] is False
    with pytest.raises(RuntimeError, match="No API key"):
        make_client(cfg, use_case="review")


def test_clarity_reviewer_does_not_rerun_blocked_reply(isolated_config, monkeypatch):
    from ai_paper_review.llm.clients.base import ReplyBlockedError
    monkeypatch.setattr("time.sleep", lambda s: None)
    llm = _CountingLLM(exc=ReplyBlockedError("Claude refused the request"))
    with pytest.raises(ReplyBlockedError):
        _run_clarity_reviewer(llm)
    assert llm.calls == 1


@pytest.mark.parametrize("content,refusal,finish_reason", [
    (None, "I can't help you get around a rate limit.", "stop"),
    ("", None, "content_filter"),
])
@pytest.mark.parametrize("client", ["openai", "xai"])
def test_openai_family_raises_on_refusal_or_content_filter(
        isolated_config, content, refusal, finish_reason, client):
    from ai_paper_review.llm.clients.base import ReplyBlockedError
    from ai_paper_review.llm.clients.openai import OpenAIClient
    from ai_paper_review.llm.clients.xai import XaiClient
    from ai_paper_review.llm.retrying import _is_rate_limit_error
    c = (OpenAIClient(model="gpt-4o", api_key="x") if client == "openai"
         else XaiClient(model="grok-4", api_key="x"))
    fake = _FakeChat()
    fake._reply = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, refusal=refusal),
        finish_reason=finish_reason)])
    c._client = fake
    with pytest.raises(ReplyBlockedError) as ei:
        c.complete("s", "u")
    assert not _is_rate_limit_error(ei.value)


def test_provider_override_drops_base_url_of_old_provider(isolated_config, monkeypatch):
    """A config.yaml base_url belongs to its provider: switching provider
    per job (state or --provider) must not send the new provider's key to it."""
    from ai_paper_review.llm import factory
    from ai_paper_review.review import review
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: llama-3\n"
        "  base_url: https://api.together.xyz/v1\n"
        "api_keys:\n"
        "  anthropic_api: sk-ant-test\n"
    )
    built = []

    def fake_make_client(cfg, use_case="default"):
        built.append(make_client(cfg, use_case))
        return object()

    monkeypatch.setattr(factory, "make_client", fake_make_client)
    monkeypatch.setattr(review, "extract_pdf_for_provider", lambda p, prov: "text " * 200)
    monkeypatch.setattr(review, "extract_paper_summary_llm",
                        lambda text, llm: {"title": "T"})
    review.node_ingest_pdf({"pdf_path": "x.pdf", "llm_provider": "anthropic_api"})
    assert "together" not in str(built[0]._inner._client.base_url)

    # CLI --provider sets the env override; same rule.
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", "anthropic_api")
    assert load_config().resolve_base_url_for_stage("review") is None
    # An explicit base_url override is kept.
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE", "https://proxy.example/v1")
    assert load_config().resolve_base_url_for_stage("review") == "https://proxy.example/v1"


def test_google_client_checks_block_before_reading_text(isolated_config):
    from google.genai import types as gtypes
    from ai_paper_review.llm.clients.base import ReplyBlockedError

    class Blocked:
        prompt_feedback = SimpleNamespace(block_reason=gtypes.BlockedReason("SAFETY"))
        candidates = None

        @property
        def text(self):
            raise ValueError("no text on a blocked reply")

    with pytest.raises(ReplyBlockedError, match="blocked"):
        _fake_gemini(Blocked()).complete("s", "u")


def test_claude_sdk_waits_for_short_rate_limit_reset(isolated_config, monkeypatch):
    from ai_paper_review.llm.clients import claude
    from ai_paper_review.llm.retrying import _is_rate_limit_error
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(claude.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(claude.time, "time", lambda: 1000.0)
    info = SimpleNamespace(status="rejected", resets_at=1120,
                           rate_limit_type="five_hour")
    _stub_claude_query(monkeypatch, [SimpleNamespace(rate_limit_info=info)])
    with pytest.raises(RuntimeError) as ei:
        ClaudeSDKClient(model="m").complete("sys", "user")
    assert _is_rate_limit_error(ei.value)
    assert slept == [121]


def test_retry_client_forwards_cleanup_uploaded_files(isolated_config):
    cleaned = []

    class WithCleanup:
        model = "fake"
        def cleanup_uploaded_files(self):
            cleaned.append(True)

    class WithoutCleanup:
        model = "fake"

    RetryClient(WithCleanup()).cleanup_uploaded_files()
    assert cleaned == [True]
    RetryClient(WithoutCleanup()).cleanup_uploaded_files()  # no-op


def test_openai_compatible_does_not_send_openai_key_to_other_hosts(isolated_config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "  base_url: https://api.thirdparty.example/v1\n"
    )
    cfg = load_config()
    assert cfg.resolve_api_key("openai_compatible_api") is None
    with pytest.raises(RuntimeError, match="No API key"):
        make_client(cfg, use_case="review")
    assert cfg.resolve_api_key("openai_compatible_api",
                               "https://api.openai.com/v1") == "sk-real-openai"


def test_is_local_provider_resolves_base_url_per_stage(isolated_config):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "  base_url: https://api.thirdparty.example/v1\n"
        "llm_validation:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://localhost:11434/v1\n"
    )
    cfg = load_config()
    assert is_local_provider(cfg, "openai_compatible_api") is False
    assert is_local_provider(cfg, "openai_compatible_api", use_case="review") is False
    assert is_local_provider(cfg, "openai_compatible_api", use_case="validation") is True


@pytest.mark.parametrize("url,local", [
    ("http://ollama:11434/v1", True),
    ("http://gpu-box.lan:8000/v1", True),
    ("http://mybox.local:8000/v1", True),
    ("http://100.100.1.2:11434/v1", True),
    ("http://100.128.0.1:11434/v1", False),
    ("https://api.thirdparty.example/v1", False),
])
def test_is_local_url_private_hosts(url, local):
    from ai_paper_review.llm.utils import _is_local_url
    assert _is_local_url("openai_compatible_api", url) is local


def test_rate_limit_config_empty_yaml_values_use_defaults(isolated_config):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: anthropic_api\n"
        "  request_delay:\n"
        "  max_retries:\n"
        "  retry_base_delay:\n"
        "  max_concurrent:\n"
    )
    cfg = load_config()
    assert (cfg.request_delay, cfg.max_retries,
            cfg.retry_base_delay, cfg.max_concurrent) == (0.0, 2, 5.0, 10)


def test_validation_provider_override_drops_yaml_base_url(isolated_config, monkeypatch):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: anthropic_api\n"
        "llm_validation:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://localhost:11434/v1\n"
    )
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "xai_api")
    cfg = load_config()
    assert cfg.validation_base_url is None
    assert cfg.resolve_base_url_for_stage("validation") == "https://api.x.ai/v1"
    # Overriding to the same provider keeps the YAML base_url.
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "openai_compatible_api")
    assert load_config().validation_base_url == "http://localhost:11434/v1"
    # "__inherit__" resolves to the review provider (anthropic_api), which
    # differs from the YAML one, so the Ollama base_url is dropped.
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "__inherit__")
    assert load_config().validation_base_url is None


def test_review_provider_override_drops_inherited_validation_base_url(isolated_config, monkeypatch):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: http://gpu1:8000/v1\n"
        "llm_validation:\n"
        "  base_url: http://gpu2:8000/v1\n"
    )
    assert load_config().validation_base_url == "http://gpu2:8000/v1"
    monkeypatch.setenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", "xai_api")
    cfg = load_config()
    assert cfg.resolve_provider("validation") == "xai_api"
    assert cfg.validation_base_url is None
    assert cfg.resolve_base_url_for_stage("validation") == "https://api.x.ai/v1"
    # An explicit validation base_url override still applies.
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_BASE_URL_OVERRIDE", "http://gpu3:8000/v1")
    assert load_config().validation_base_url == "http://gpu3:8000/v1"


def test_retry_client_negative_max_retries_calls_once(isolated_config):
    class Ok:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            return "ok"

    assert RetryClient(Ok(), max_retries=-1).complete("sys", "user") == "ok"


def _httpx_connect_error():
    import httpx
    return httpx.ConnectError("connection reset",
                              request=httpx.Request("POST", "https://api.example/v1"))


@pytest.mark.parametrize("make_exc", [_httpx_connect_error, lambda: TimeoutError("timed out")])
def test_retry_client_retries_transport_errors(isolated_config, monkeypatch, make_exc):
    from ai_paper_review.llm import retrying
    monkeypatch.setattr(retrying.time, "sleep", lambda s: None)
    calls = 0

    class Flaky:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise make_exc()
            return "ok"

    assert RetryClient(Flaky(), max_retries=2, base_delay=0.01).complete("sys", "user") == "ok"
    assert calls == 2


def test_anthropic_client_streams_above_sdk_per_model_cap(monkeypatch):
    """The SDK refuses non-streaming calls above its per-model cap,
    below the client's own 16 K threshold."""
    from anthropic import _constants
    from ai_paper_review.llm.clients.anthropic import AnthropicClient

    monkeypatch.setitem(_constants.MODEL_NONSTREAMING_TOKENS,
                        "claude-opus-4-1-20250805", 8192)

    calls = []

    class _Stream:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def get_final_message(self):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="streamed")],
                stop_reason="end_turn")

    class _Messages:
        def create(self, **kw):
            calls.append("create")
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="created")],
                stop_reason="end_turn")
        def stream(self, **kw):
            calls.append("stream")
            return _Stream()

    ac = AnthropicClient(model="claude-opus-4-1-20250805", api_key="x")
    ac._client = SimpleNamespace(messages=_Messages())
    assert ac.complete("s", "u", max_tokens=8192) == "created"
    assert ac.complete("s", "u", max_tokens=10000) == "streamed"
    assert calls == ["create", "stream"]


@pytest.mark.parametrize("resp", [
    SimpleNamespace(output_text="", status="completed", incomplete_details=None,
                    output=[SimpleNamespace(content=[SimpleNamespace(
                        type="refusal", refusal="I can't help with that.")])]),
    SimpleNamespace(output_text="", status="incomplete", output=[],
                    incomplete_details=SimpleNamespace(reason="content_filter")),
    SimpleNamespace(output_text="", status="incomplete", output=[],
                    incomplete_details=SimpleNamespace(reason="max_output_tokens")),
])
def test_xai_client_pdf_path_raises_on_blocked_reply(isolated_config, tmp_path, resp):
    from ai_paper_review.llm.clients.base import ReplyBlockedError
    from ai_paper_review.llm.clients.xai import XaiClient

    uploads = SimpleNamespace(create=lambda file, purpose: SimpleNamespace(id="file_1"))
    client = XaiClient(model="grok-4.20-reasoning", api_key="k")
    client._client = SimpleNamespace(
        files=uploads, responses=SimpleNamespace(create=lambda **kw: resp))
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(ReplyBlockedError):
        client.complete("s", "u", pdf_path=str(pdf))


def test_google_client_falls_back_when_cached_call_fails(tmp_path):
    """An expired (past-TTL) context cache makes the cached call fail;
    the client must retry without the cache and stop using it."""
    import threading
    from ai_paper_review.llm.clients.google import GoogleClient

    calls = []

    class _Models:
        def generate_content(self, *, model, config, contents):
            calls.append(config.kwargs)
            if "cached_content" in config.kwargs:
                raise RuntimeError("403 CachedContent not found (or expired)")
            return SimpleNamespace(text="ok")

    class _Types:
        class Part:
            @staticmethod
            def from_bytes(*, data, mime_type):
                return "pdf"
        class GenerateContentConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
        CreateCachedContentConfig = GenerateContentConfig

    gc = GoogleClient.__new__(GoogleClient)
    gc._client = SimpleNamespace(
        models=_Models(),
        caches=SimpleNamespace(create=lambda **kw: SimpleNamespace(name="cachedContents/x")))
    gc._types = _Types
    gc.model = "gemini-2.5-pro"
    gc._cache_names = {}
    gc._cache_lock = threading.Lock()
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    assert gc.complete("sys", "u1", pdf_path=str(pdf)) == "ok"
    assert gc.complete("sys", "u2", pdf_path=str(pdf)) == "ok"
    # cached (fails), uncached, then uncached only.
    assert ["cached_content" in c for c in calls] == [True, False, False]
    assert calls[1]["system_instruction"] == "sys"


@pytest.mark.parametrize("status, retried", [(429, True), (500, True),
                                             (529, True), (400, False)])
def test_claude_sdk_client_retries_result_api_error_status(isolated_config, monkeypatch,
                                                           status, retried):
    """A ResultMessage with is_error and api_error_status 429 / 5xx is
    retried; other statuses are not."""
    import claude_agent_sdk
    from ai_paper_review.llm.retrying import _is_rate_limit_error

    async def fake_query(prompt, options):
        yield SimpleNamespace(is_error=True, subtype="success", errors=None,
                              result="API Error: Overloaded",
                              api_error_status=status)

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    with pytest.raises(RuntimeError) as ei:
        ClaudeSDKClient(model="m").complete("sys", "user")
    assert _is_rate_limit_error(ei.value) is retried


def test_copilot_idle_timeout_resets_while_events_stream(isolated_config, monkeypatch):
    """A reply that keeps streaming past the idle timeout completes; a
    session with no events for the timeout still raises."""
    import asyncio
    from ai_paper_review.llm.clients import copilot

    monkeypatch.setattr(copilot, "_IDLE_TIMEOUT_S", 0.1)
    ev = lambda t, **d: SimpleNamespace(type=SimpleNamespace(value=t),
                                        data=SimpleNamespace(**d))

    def make_sdk(n_deltas):
        class FakeSession:
            def on(self, handler):
                self._handler = handler

            async def send(self, prompt):
                async def stream():
                    for _ in range(n_deltas):
                        await asyncio.sleep(0.04)
                        self._handler(ev("assistant.message_delta", delta_content="x"))
                    if n_deltas:
                        self._handler(ev("session.idle"))
                self._task = asyncio.ensure_future(stream())

        class FakeSDKClient:
            async def start(self):
                pass
            async def create_session(self, **kw):
                return FakeSession()
            async def stop(self):
                pass
        return FakeSDKClient

    client = CopilotSDKClient.__new__(CopilotSDKClient)
    client.model = "gpt-5"
    # 10 deltas over ~0.4 s, longer than the 0.1 s timeout.
    client._SDKClient = make_sdk(10)
    assert client.complete("s", "u") == "x" * 10
    client._SDKClient = make_sdk(0)
    with pytest.raises(RuntimeError, match="no events"):
        client.complete("s", "u")


@pytest.mark.parametrize("url, expected", [
    (None, True),
    ("https://api.openai.com/v1", True),
    ("https://eu.api.openai.com/v1", True),
    ("https://openai.com.attacker.example/v1", False),
    ("https://attacker.example/openai.com/v1", False),
    ("https://myres.openai.azure.com", False),
])
def test_is_openai_endpoint_matches_hostname_not_substring(isolated_config, monkeypatch,
                                                           url, expected):
    from ai_paper_review.llm.utils import is_openai_endpoint
    assert is_openai_endpoint(url) is expected
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    key = load_config().resolve_api_key("openai_compatible_api", url)
    assert (key == "sk-real-openai") is expected


def test_no_key_message_omits_openai_env_var_for_other_hosts(isolated_config):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  model: m\n"
        "  base_url: https://api.groq.example/v1\n"
    )
    with pytest.raises(RuntimeError, match="No API key") as ei:
        make_client(load_config(), use_case="review")
    assert "OPENAI_API_KEY" not in str(ei.value)
    assert "api_keys.openai_compatible_api" in str(ei.value)
    assert env_vars_for("openai_compatible_api") == []
    assert env_vars_for("openai_compatible_api",
                        "https://api.openai.com/v1") == ["OPENAI_API_KEY"]
    assert env_vars_for("openai_api") == ["OPENAI_API_KEY"]


def _google_client_raising_on_cached_call(tmp_path, exc):
    import threading
    from ai_paper_review.llm.clients.google import GoogleClient

    calls = []

    class _Models:
        def generate_content(self, *, model, config, contents):
            calls.append(config.kwargs)
            if "cached_content" in config.kwargs:
                raise exc
            return SimpleNamespace(text="ok")

    class _Types:
        class Part:
            @staticmethod
            def from_bytes(*, data, mime_type):
                return "pdf"
        class GenerateContentConfig:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
        CreateCachedContentConfig = GenerateContentConfig

    gc = GoogleClient.__new__(GoogleClient)
    gc._client = SimpleNamespace(
        models=_Models(),
        caches=SimpleNamespace(create=lambda **kw: SimpleNamespace(name="cachedContents/x")))
    gc._types = _Types
    gc.model = "gemini-2.5-pro"
    gc._cache_names = {}
    gc._cache_lock = threading.Lock()
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    return gc, str(pdf), calls


@pytest.mark.parametrize("code, status, falls_back", [
    (404, "NOT_FOUND", True),
    (403, "PERMISSION_DENIED", True),
    (429, "RESOURCE_EXHAUSTED", False),
    (500, "INTERNAL", False),
])
def test_google_client_falls_back_only_on_cache_missing(tmp_path, code, status, falls_back):
    from google.genai import errors
    cls = errors.ClientError if code < 500 else errors.ServerError
    exc = cls(code, {"error": {"message": "boom", "status": status}})
    gc, pdf, calls = _google_client_raising_on_cached_call(tmp_path, exc)
    if falls_back:
        assert gc.complete("sys", "u1", pdf_path=pdf) == "ok"
        assert gc._cache_names[pdf] is None
    else:
        with pytest.raises(type(exc)):
            gc.complete("sys", "u1", pdf_path=pdf)
        assert gc._cache_names[pdf] == "cachedContents/x"
        assert len(calls) == 1


def test_validation_override_equal_to_inherited_provider_keeps_yaml_base_url(
        isolated_config, monkeypatch):
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_compatible_api\n"
        "  base_url: https://api.thirdparty.example/v1\n"
        "llm_validation:\n"
        "  base_url: http://localhost:11434/v1\n"
    )
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "openai_compatible_api")
    assert load_config().validation_base_url == "http://localhost:11434/v1"
    monkeypatch.setenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", "xai_api")
    assert load_config().validation_base_url is None


@pytest.mark.parametrize("key, value, ok", [
    ("max_tokens", "8k", False),
    ("max_tokens", 0, False),
    ("max_concurrent", 0, False),
    ("request_delay", -0.5, False),
    ("retry_base_delay", -1, False),
    ("max_retries", -1, False),
    ("max_concurrent", "true", False),
    ("max_retries", "false", False),
    ("request_delay", "true", False),
    ("retry_base_delay", "false", False),
    ("max_concurrent", ".inf", False),
    ("request_delay", ".inf", False),
    ("request_delay", ".nan", False),
    ("max_retries", "'3.5'", "must be an integer"),
    ("max_tokens", 1, True),
    ("max_concurrent", 1, True),
    ("request_delay", 0, True),
    ("retry_base_delay", 0, True),
    ("max_retries", 0, True),
    ("max_tokens", None, True),
])
def test_rate_limit_config_rejects_bad_values(isolated_config, key, value, ok):
    yaml_value = "" if value is None else f" {value}"
    (isolated_config / "config.yaml").write_text(
        f"llm_review:\n  provider: anthropic_api\n  {key}:{yaml_value}\n"
    )
    if ok is True:
        cfg = load_config()
        if value is None:
            assert cfg.review_max_tokens == LLMConfig.review_max_tokens
    else:
        msg = ok or ".*"
        with pytest.raises(ValueError, match=f"(?i)llm_review.{key} {msg}.*{value}"):
            load_config()
