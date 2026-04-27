"""
ai_paper_review
===============

AI paper review system: 200 LLM reviewer personas, topic-based selection,
parallel review, clustering, ranking, and human-feedback calibration.

INTENDED USE
------------
This package is a **draft-polishing aid** for authors working on their own
papers. It helps identify potential weaknesses before submission.

It is **NOT** a peer-review generator. Most venues have strict policies
against using LLMs in assigned reviews, due to concerns about bias,
hallucination, and the potential for compromising the integrity of the
peer-review process. Please use it at your own discretion, and indicate
when you have used it.

Every comment this system produces is a **suggestion to evaluate**, not a
finding to accept. AI reviewers hallucinate, miss context, and
over-confidently flag non-issues. Expect to reject roughly half of what
you see. Use at your own discretion.

Layout
------
Two pipeline packages, one post-pipeline reporter, plus a shared LLM
abstraction:

  - ``ai_paper_review.review``      — paper review pipeline.
       ``review.review``  is the pipeline orchestrator and CLI
       (``ai-paper-review-review``); the sibling modules own
       individual stages.
  - ``ai_paper_review.validation``  — validation pipeline. The CLI
       ``ai-paper-review-validate`` aligns AI vs human and emits the
       per-paper calibration delta. The ``conversion`` sibling (auto-
       run by the web flow when raw human-review text needs reshaping
       into the AI-review schema) is part of the same pipeline.
  - ``ai_paper_review.aggregation`` — post-pipeline cross-paper
       reporter. Rolls up many ``calibration_delta.json`` files into
       tuning recommendations for the reviewer database. Reachable
       from ``/aggregate`` in the web UI; importable for scripted
       use. No CLI.
  - ``ai_paper_review.llm``         — provider-agnostic LLM client.
       Submodules: ``config`` (``LLMConfig``, ``load_config``),
       ``factory`` (``make_client``), ``probing`` (UI helpers),
       ``utils``, ``retrying`` (``RetryClient``), and ``clients/``
       (one module per provider — anthropic, openai, google, copilot).
  - ``ai_paper_review.web``         — Flask UI (``ai-paper-review-web``).

Each pipeline package's submodules are the canonical import targets —
the package ``__init__`` files are intentionally empty, so import
explicitly:

    from ai_paper_review.review.reviewer_db import Reviewer, parse_reviewer_db
    from ai_paper_review.review.review import build_graph, ReviewState
    from ai_paper_review.validation.alignment import align_comments
    from ai_paper_review.aggregation.aggregation import aggregate, load_deltas
    from ai_paper_review.llm.config import load_config
    from ai_paper_review.llm.factory import make_client
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def default_db_path() -> Path:
    """Path to the bundled comparch_reviewer_db.md shipped with the package.

    Works both when the package is installed (from site-packages) and
    when running from a source checkout via `pip install -e .`.
    """
    return Path(str(files("ai_paper_review.data").joinpath("comparch_reviewer_db.md")))


__all__ = [
    "default_db_path",
]
