"""Topic-similarity reviewer selection.

The pipeline picks the top-N reviewers whose keyword profile best matches
the paper. The default selection diversifies on persona so all 20
personas in a domain don't dominate one paper — see
:func:`select_reviewers` docstring for the algorithm and trade-offs.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from .constants import (
    DEFAULT_N_REVIEWERS,
    MAX_N_REVIEWERS,
    MIN_N_REVIEWERS,
)
from .reviewer_db import Reviewer, parse_reviewer_db

logger = logging.getLogger("review_system")


class Embedder:
    """Pluggable embedder: prefers sentence-transformers, falls back to TF-IDF."""

    def __init__(self):
        self._model = None
        self._backend = "tfidf"
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            self._backend = "sbert"
            logger.info("Using sentence-transformers embedder")
        except Exception:
            logger.info("sentence-transformers not available; falling back to TF-IDF")

    def embed(self, texts: List[str]) -> np.ndarray:
        if self._backend == "sbert":
            return np.array(self._model.encode(texts, normalize_embeddings=True))
        from sklearn.feature_extraction.text import TfidfVectorizer
        vect = TfidfVectorizer(
            ngram_range=(1, 2), max_features=8000, stop_words="english"
        )
        M = vect.fit_transform(texts).astype(np.float32).toarray()
        norms = np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
        return M / norms

    @property
    def backend(self) -> str:
        """'sbert' or 'tfidf' — downstream can auto-tune thresholds from this."""
        return self._backend


def rank_reviewers_by_similarity(
    paper: Dict[str, str],
    reviewers: List[Reviewer],
    embedder: Optional[Embedder] = None,
) -> List[Tuple[Reviewer, float]]:
    """Return all reviewers sorted by cosine similarity to the paper, descending.

    This is the raw similarity landscape before persona-diversifying
    selection truncates it to top-N. Exposed so callers can surface the
    full score distribution (e.g. the ``selection_similarities.md``
    output artifact) without re-embedding.
    """
    embedder = embedder or Embedder()
    paper_text = f"{paper['title']}\n\n{paper['abstract']}\n\n{paper['full_text'][:4000]}"
    reviewer_texts = [r.keyword_text() for r in reviewers]
    vectors = embedder.embed([paper_text] + reviewer_texts)
    paper_vec, reviewer_vecs = vectors[0], vectors[1:]
    sims = reviewer_vecs @ paper_vec  # cosine, since vectors are L2-normalized
    return sorted(zip(reviewers, sims), key=lambda x: -float(x[1]))


def select_reviewers(
    paper: Dict[str, str],
    reviewers: List[Reviewer],
    k: int = DEFAULT_N_REVIEWERS,
    embedder: Optional[Embedder] = None,
    diversify_personas: bool = True,
    domain_bleed: float = 0.15,
    selection_similarities: Optional[List[Tuple[Reviewer, float]]] = None,
) -> List[Tuple[Reviewer, float]]:
    """Select the top-N reviewers by topic similarity to the paper.

    Pure top-N would collapse to a single domain because all 20 reviewers
    in a domain share keyword profiles. Since the 20 personas exist to
    deliver *varying review aspects*, the default strategy is
    persona-diversifying:

      1. Compute paper-reviewer cosine similarity.
      2. Greedy pick, highest score first.
      3. Skip candidates whose persona is already selected.
      4. ``domain_bleed`` softly allows cross-domain picks when a second
         domain scores within ``domain_bleed`` of the best remaining
         in-domain candidate.

    Set ``diversify_personas=False`` for naive top-N (no persona
    diversification, no soft domain cap).

    Pass ``selection_similarities`` to reuse a precomputed full ranking
    from :func:`rank_reviewers_by_similarity` — avoids re-embedding when
    the caller has already computed it (as the web worker and CLI both
    do so they can emit ``selection_similarities.md``).
    """
    if selection_similarities is None:
        selection_similarities = rank_reviewers_by_similarity(
            paper, reviewers, embedder=embedder,
        )

    if not diversify_personas:
        return selection_similarities[:k]

    selected: List[Tuple[Reviewer, float]] = []
    used_personas: set = set()
    domain_counts: Dict[str, int] = {}
    domain_cap = max(1, int(round(k * (1.0 - domain_bleed))))

    for reviewer, score in selection_similarities:
        if len(selected) >= k:
            break
        if reviewer.persona in used_personas:
            continue
        if domain_counts.get(reviewer.domain, 0) >= domain_cap:
            # Soft cap — accept only if no under-used domain has a
            # candidate within `domain_bleed` of this one.
            best_other = next(
                (s for r, s in selection_similarities
                 if r.persona not in used_personas
                 and domain_counts.get(r.domain, 0) < domain_cap),
                None,
            )
            if best_other is not None and score < best_other - domain_bleed:
                continue
        selected.append((reviewer, score))
        used_personas.add(reviewer.persona)
        domain_counts[reviewer.domain] = domain_counts.get(reviewer.domain, 0) + 1

    return selected


def node_load_db(state):
    logger.info("Loading reviewer DB: %s", state["db_path"])
    state["reviewers"] = parse_reviewer_db(state["db_path"])
    logger.info("Loaded %d reviewers", len(state["reviewers"]))
    return state


def node_select_reviewers(state):
    """Select top-N reviewers, clamping ``n_reviewers`` to the allowed range.

    Avoid ``or DEFAULT_N_REVIEWERS`` here — 0 is falsy and would sneak
    past as "unset" instead of being clamped to MIN.

    Also stashes the full pre-selection ranking as
    ``state["selection_similarities"]`` so the worker can write
    ``selection_similarities.md`` without re-embedding every reviewer.
    """
    raw = state.get("n_reviewers")
    if raw is None:
        requested = DEFAULT_N_REVIEWERS
    else:
        try:
            requested = int(raw)
        except (TypeError, ValueError):
            requested = DEFAULT_N_REVIEWERS
    k = max(MIN_N_REVIEWERS, min(MAX_N_REVIEWERS, requested))
    if k != requested:
        logger.warning(
            "n_reviewers=%d clamped to %d (allowed range %d..%d)",
            requested, k, MIN_N_REVIEWERS, MAX_N_REVIEWERS,
        )
    # Write the normalized value back so downstream nodes / the report /
    # the UI state all see what the user actually got.
    state["n_reviewers"] = k
    logger.info("Selecting top-%d reviewers by topic similarity", k)
    selection_similarities = rank_reviewers_by_similarity(
        state["paper"], state["reviewers"],
    )
    selected = select_reviewers(
        state["paper"], state["reviewers"], k=k,
        selection_similarities=selection_similarities,
    )
    state["selection_similarities"] = selection_similarities
    state["selected"] = selected
    for r, s in selected:
        logger.info("  %s | %s | %s | score=%.3f", r.id, r.domain, r.persona, s)
    return state


def format_selection_similarities_md(
    paper: Dict[str, str],
    selection_similarities: List[Tuple[Reviewer, float]],
    selected: List[Tuple[Reviewer, float]],
    n_requested: int,
) -> str:
    """Render the full pre-selection similarity ranking as markdown."""
    selected_ids = {r.id for r, _ in selected}
    n_total = len(selection_similarities)

    out: List[str] = []
    out.append("# Reviewer Selection Similarities\n\n")
    out.append(f"**Paper:** {paper.get('title', '(untitled)')}\n\n")
    out.append(f"**Reviewers requested (N):** {n_requested}\n\n")
    out.append(f"**Reviewers in database:** {n_total}\n\n")
    out.append(
        "Each row is a reviewer's cosine similarity to the paper's topic "
        "(its keyword profile vs. the paper text). Higher = better "
        "topical match. The **Selected** column marks the top-N after "
        "persona-diversifying selection — persona diversification and "
        "the `domain_bleed` soft cap can pass over higher-scoring "
        "candidates to widen persona coverage, so the selected rows "
        "aren't always the top-N by similarity alone.\n\n"
    )
    out.append("| Rank | Reviewer | Domain | Persona | Similarity | Selected |\n")
    out.append("|---|---|---|---|---|---|\n")
    for rank, (r, score) in enumerate(selection_similarities, start=1):
        mark = "✓" if r.id in selected_ids else ""
        out.append(
            f"| {rank} | {r.id} | {r.domain} | {r.persona} | "
            f"{float(score):.3f} | {mark} |\n"
        )
    return "".join(out)
