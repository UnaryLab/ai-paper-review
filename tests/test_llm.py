"""Config loader, provider probe, and CLI override behavior."""
from __future__ import annotations


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
        "  model: claude-sonnet-4-5-20250929\n"
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
    assert cfg.resolve_model("validation") == "claude-sonnet-4-5-20250929"


def test_validation_env_override_with_explicit_value_wins(isolated_config, monkeypatch):
    """Non-sentinel env values still override config.yaml — regression
    guard for the sentinel refactor above."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: anthropic_api\n"
        "  model: claude-sonnet-4-5-20250929\n"
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
    assert len(probed) == 8
    names = [p["name"] for p in probed]
    assert set(names) == {"anthropic_api", "openai_api", "google_api", "xai_api",
                           "github_api", "copilot_sdk", "claude_sdk",
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
    assert probed["github_api"]["configured"] is False


def test_github_has_default_base_url():
    """Users shouldn't need to configure base_urls for these built-ins."""
    cfg = LLMConfig(review_provider="github_api")
    assert cfg.resolve_base_url("github_api") == "https://models.github.ai/inference"


def test_github_env_var_fallback(isolated_config, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    cfg = load_config()
    assert cfg.resolve_api_key("github_api") == "ghp_test"


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


def test_retry_client_retries_on_rate_limit(isolated_config):
    """RetryClient should retry on rate-limit-like exceptions."""
    call_count = 0

    class FakeClient:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("429 Too Many Requests")
            return "success"

    rc = RetryClient(FakeClient(), max_retries=3, base_delay=0.01)
    result = rc.complete("sys", "user")
    assert result == "success"
    assert call_count == 3


def test_retry_client_raises_after_exhaustion(isolated_config):
    """If all retries are exhausted, the original exception propagates."""
    class AlwaysFailing:
        model = "fake"
        def complete(self, system, user, max_tokens=4000, pdf_path=None):
            raise Exception("429 rate limit exceeded forever")

    rc = RetryClient(AlwaysFailing(), max_retries=2, base_delay=0.01)
    import pytest
    with pytest.raises(Exception, match="429"):
        rc.complete("sys", "user")


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
        "llm_review:\n  provider: claude_sdk\n  model: claude-sonnet-4-5-20250929\n"
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
    assert provider_supports_pdf("anthropic_api") is True
    assert provider_supports_pdf("openai_api") is True
    # xAI uses its own PDF path (upload /v1/files + Responses API) on
    # grok-4-class models, wired up via XaiClient.
    assert provider_supports_pdf("xai_api") is True
    assert provider_supports_pdf("google_api") is True
    assert provider_supports_pdf("claude_sdk") is True
    # Text-only providers.
    assert provider_supports_pdf("github_api") is False
    assert provider_supports_pdf("openai_compatible_api") is False
    assert provider_supports_pdf("copilot_sdk") is False
    assert provider_supports_pdf("bogus") is False


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
        def __init__(self, chunks):
            self._chunks = chunks
            self.text_stream = iter(chunks)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    class _FakeMessages:
        def create(self, **kwargs):
            create_calls.append(kwargs)
            class _B:
                type = "text"
                text = "non-stream reply"
            class _M:
                content = [_B()]
            return _M()

        def stream(self, **kwargs):
            stream_calls.append(kwargs)
            return _FakeStreamContext(["stream-", "reply"])

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    ac = AnthropicClient(model="claude-sonnet-4-5-20250929", api_key="x")
    ac._client = _FakeClient()

    # Low budget → non-streaming create.
    r1 = ac.complete("sys", "u", max_tokens=4000)
    assert r1 == "non-stream reply"
    assert len(create_calls) == 1 and len(stream_calls) == 0

    # High budget → streaming. The concatenated chunks come back as
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
            return _R()

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    ac = AnthropicClient(model="claude-sonnet-4-5-20250929", api_key="x")
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
    assert "**Paper:** Sample" in md
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
        ch = _Choice(); ch.message = msg
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
