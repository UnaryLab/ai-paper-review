"""Concrete LLM client implementations, one per provider.

Each client structurally implements the ``LLMClient`` protocol from
``base.py`` (single ``complete(system, user, max_tokens) -> str``
method). SDKs are imported lazily inside each client's ``__init__`` so
unused providers don't drag in unused dependencies.
"""
