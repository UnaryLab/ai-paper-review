"""
ai_paper_review.llm
===================

Provider-agnostic LLM wrapper used by ``ai_paper_review.review`` and
``ai_paper_review.validation``.

Modules:

  - ``config``       — ``LLMConfig`` dataclass + ``load_config`` (YAML +
                       env-var overrides) + ``SUPPORTED_PROVIDERS``.
  - ``clients/``     — concrete provider clients, one per file:
                       ``base`` (Protocol), ``anthropic``, ``openai``
                       (also serves xai / github / openai_compatible),
                       ``google``, ``copilot``.
  - ``retrying``     — ``RetryClient`` wrapper with exponential backoff.
  - ``factory``      — ``make_client`` / ``default_client`` — turn a
                       config into a ready LLMClient.
  - ``probing``      — ``probe_providers`` / ``describe_config`` /
                       ``_copilot_sdk_installed`` for the web UI.
  - ``utils``        — ``env_vars_for`` / ``is_local_provider``.

Each module is the single source of truth for one concern. Import
explicitly from the relevant submodule (e.g.
``from ai_paper_review.llm.config import load_config,
LLMConfig``); this package's ``__init__`` deliberately exposes nothing.

Run ``python -m ai_paper_review.llm`` to dump the resolved config to
stdout — handy for verifying which provider/model wins after
config.yaml + env-var override resolution. (Implementation lives in
``__main__.py``.)
"""
