"""Shared pytest fixtures.

Keeps tests fast and offline — no LLM calls, no network, no sentence-transformers
downloads required.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect config lookup to a temp directory so tests don't pick up the
    user's real config.yaml or env vars."""
    monkeypatch.delenv("PAPER_REVIEW_CONFIG", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_REVIEW_MODEL_OVERRIDE", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_VALIDATION_MODEL_OVERRIDE", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_VALIDATION_BASE_URL_OVERRIDE", raising=False)
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def config_with_openai(isolated_config):
    """A config.yaml in cwd with OpenAI configured (new stage-per-key layout)."""
    (isolated_config / "config.yaml").write_text(
        "llm_review:\n"
        "  provider: openai_api\n"
        "  model: gpt-4o\n"
        "api_keys:\n"
        "  openai_api: sk-test-openai\n"
    )
    return isolated_config


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def actual_review(fixtures_dir):
    from ai_paper_review.review.parsing import load_reviews_file
    return load_reviews_file(str(fixtures_dir / "actual.md"))


@pytest.fixture
def ai_review(fixtures_dir):
    from ai_paper_review.review.parsing import load_reviews_file
    return load_reviews_file(str(fixtures_dir / "ai.md"))


@pytest.fixture
def mock_llm_for_fixtures():
    """Stub LLM whose ``complete()`` returns a batch similarity-matrix
    response tuned to the bundled ``actual.md`` / ``ai.md`` fixture:

      - Reviewer_qFvT-C1 ↔ R001-C1 (novelty): 0.85 → ``same``
      - Reviewer_qFvT-C2 ↔ R004-C1 (evaluation): 0.82 → ``same``
      - R017-C1 / R017-C2 have no good human match → false alarms.

    The strength-contradiction detection for R017-C1 (which contradicts the
    "clean and well-engineered" strength) is handled by an embedding
    post-pass inside ``align_comments_batch_llm``, not by the LLM itself.
    """
    # Scripted scores, keyed by (human_id, ai_id). Pairs not listed → 0.05.
    scores = {
        ("Reviewer_qFvT-C1", "R001-C1"): 0.85,
        ("Reviewer_qFvT-C2", "R004-C1"): 0.82,
    }

    class _StubLLM:
        model = "stub-llm-for-tests"

        def complete(self, system_prompt, user_msg, max_tokens=8000):
            # Extract the human + AI ids that appear in the prompt so we
            # emit a correct N×M block even if the fixture changes.
            human_ids = re.findall(r"- \*\*(Reviewer_\w+-C\d+)\*\*:", user_msg)
            ai_ids = re.findall(r"- \*\*(R\d+-C\d+)\*\*:", user_msg)
            lines = ["### Similarity scores", ""]
            for hid in human_ids:
                for aid in ai_ids:
                    lines.append(f"{hid} | {aid} | "
                                 f"{scores.get((hid, aid), 0.05):.2f}")
            lines += ["", "### Ranked human comments", ""]
            best = {}
            for hid in human_ids:
                top = max(((scores.get((hid, aid), 0.05), aid) for aid in ai_ids),
                          default=(0.0, "?"))
                best[hid] = top
            for rank, (hid, (sim, aid)) in enumerate(
                    sorted(best.items(), key=lambda t: -t[1][0]), start=1):
                lines.append(f"{rank}. {hid} — best_match={aid} sim={sim:.2f}")
            return "\n".join(lines)

    return _StubLLM()
