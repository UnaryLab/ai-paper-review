"""Greedy agglomerative clustering of cross-reviewer comments.

Embeds each comment's ``summary + description + keywords`` text and
groups any pair whose cosine similarity exceeds the threshold (env-var
``CLUSTER_THRESHOLD``, default ``0.55``). Produces clusters scored on
how many *distinct* reviewers raised the same point — the consensus
signal that drives ranking downstream.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import numpy as np

from .constants import SEVERITY_WEIGHT
from .selection import Embedder

logger = logging.getLogger("review_system")


def node_cluster_comments(state):
    """Cluster comments by semantic similarity of (summary + description + keywords).

    Also stashes ``state["clustering_similarities"]`` — the full pairwise
    matrix + threshold + per-comment labels — so the worker can emit
    ``clustering_similarities.md`` without re-embedding.
    """
    comments = state["all_comments"]
    if not comments:
        state["clusters"] = []
        state["clustering_similarities"] = {
            "threshold": float(os.environ.get("CLUSTER_THRESHOLD", "0.55")),
            "backend": "n/a",
            "labels": [],
            "matrix": [],
            "cluster_of": [],
        }
        return state

    # Empty-text comments embed identically and form one giant empty cluster.
    n_empty = sum(
        1 for c in comments
        if not (c.get("summary") or c.get("description") or c.get("keywords"))
    )
    if n_empty:
        logger.warning(
            "Clustering input: %d/%d comments have empty summary+description+keywords. "
            "These will embed identically and form one giant empty-text cluster. "
            "Check upstream parse warnings.",
            n_empty, len(comments),
        )

    logger.info("Clustering %d comments", len(comments))
    embedder = Embedder()
    texts = [
        f"{c.get('summary','')} {c.get('description','')} {' '.join(c.get('keywords', []))}"
        for c in comments
    ]
    vecs = embedder.embed(texts)  # L2-normalized
    # Compute the full pairwise cosine matrix once — used both by the
    # clustering loop below and by format_clustering_similarities_md.
    sim_matrix = vecs @ vecs.T

    threshold = float(os.environ.get("CLUSTER_THRESHOLD", "0.55"))
    n = len(comments)
    assigned = [-1] * n
    clusters: List[List[int]] = []
    for i in range(n):
        if assigned[i] >= 0:
            continue
        cid = len(clusters)
        clusters.append([i])
        assigned[i] = cid
        for j in range(i + 1, n):
            if assigned[j] >= 0:
                continue
            if float(sim_matrix[i, j]) >= threshold:
                clusters[cid].append(j)
                assigned[j] = cid

    out: List[Dict[str, Any]] = []
    for cid, idxs in enumerate(clusters):
        members = [comments[i] for i in idxs]
        reviewers_in_cluster = sorted({m["_reviewer_id"] for m in members})
        severities = [SEVERITY_WEIGHT.get(m.get("severity", "minor"), 1.0) for m in members]
        # Pick the highest-severity comment as representative; ties go to longest.
        rep = max(
            members,
            key=lambda m: (
                SEVERITY_WEIGHT.get(m.get("severity", "minor"), 1.0),
                len(m.get("description", "")),
            ),
        )
        out.append(
            {
                "cluster_id": f"C{cid:03d}",
                "size": len(members),
                "num_distinct_reviewers": len(reviewers_in_cluster),
                "reviewers": reviewers_in_cluster,
                "avg_severity_weight": float(np.mean(severities)),
                "max_severity_weight": float(np.max(severities)),
                "representative": rep,
                "members": members,
            }
        )

    state["clusters"] = out
    # Per-comment labels for the similarity file. ``R001.c2`` style:
    # reviewer ID + 1-based index of this comment within that reviewer's
    # comment list — short enough for matrix headers, unambiguous enough
    # to trace back to review_data.md.
    per_reviewer_seq: Dict[str, int] = {}
    labels: List[str] = []
    for c in comments:
        rid = c.get("_reviewer_id", "?")
        per_reviewer_seq[rid] = per_reviewer_seq.get(rid, 0) + 1
        labels.append(f"{rid}.c{per_reviewer_seq[rid]}")
    state["clustering_similarities"] = {
        "threshold": threshold,
        "backend": embedder.backend,
        "labels": labels,
        "matrix": sim_matrix.tolist(),
        "cluster_of": assigned,  # index-aligned with labels: cid of each comment
    }
    logger.info("Formed %d clusters", len(out))
    return state


def format_clustering_similarities_md(
    paper: Dict[str, Any],
    comments: List[Dict[str, Any]],
    clustering_similarities: Dict[str, Any],
) -> str:
    """Render the pairwise comment-similarity landscape as markdown."""
    threshold = float(clustering_similarities.get("threshold", 0.55))
    backend = clustering_similarities.get("backend", "?")
    labels: List[str] = list(clustering_similarities.get("labels") or [])
    matrix: List[List[float]] = list(clustering_similarities.get("matrix") or [])
    cluster_of: List[int] = list(clustering_similarities.get("cluster_of") or [])
    n = len(labels)

    near_miss_floor = max(0.0, threshold - 0.15)

    out: List[str] = []
    out.append("# Clustering Similarities\n\n")
    out.append(f"**Title:** {paper.get('title', '(untitled)')}\n\n")
    out.append(f"**Comments clustered:** {n}\n\n")
    out.append(f"**Similarity threshold:** {threshold:.3f} "
               "(env: `CLUSTER_THRESHOLD`)\n\n")
    out.append(f"**Embedder backend:** `{backend}` "
               "(`sbert` → sentence-transformers `all-MiniLM-L6-v2`; "
               "`tfidf` → scikit-learn fallback)\n\n")
    out.append(
        "Each row below is one ordered pair of comments (rows i < j over "
        "the full comment list). Similarity is cosine (L2-normalized "
        "embeddings). Pairs at or above the threshold merged into the "
        "same cluster; the clustering is greedy single-link, so a pair "
        "below threshold can still end up in the same cluster via a "
        "higher-scoring chain of intermediate pairs.\n\n"
    )

    # ---- View 1: sorted near-threshold pair list ----
    out.append("## Pairs near / above the clustering threshold\n\n")
    out.append(f"Every pair with similarity ≥ {near_miss_floor:.3f} "
               f"(threshold − 0.15), descending. The **Same cluster?** "
               "column reflects the final clustering decision after the "
               "greedy chain — so some pairs just below threshold still "
               "read as same-cluster.\n\n")
    out.append("| i | j | Comment A | Comment B | Similarity | ≥ threshold | Same cluster? |\n")
    out.append("|---|---|---|---|---|---|---|\n")
    rows: List[tuple] = []
    for i in range(n):
        for j in range(i + 1, n):
            s = float(matrix[i][j])
            if s >= near_miss_floor:
                rows.append((i, j, s))
    rows.sort(key=lambda r: -r[2])
    for i, j, s in rows:
        ge_thr = "✓" if s >= threshold else ""
        same_cl = (
            "✓" if (cluster_of and cluster_of[i] == cluster_of[j]) else ""
        )
        out.append(
            f"| {i} | {j} | {labels[i]} | {labels[j]} | "
            f"{s:.3f} | {ge_thr} | {same_cl} |\n"
        )
    if not rows:
        out.append("| — | — | — | — | (no pairs ≥ threshold − 0.15) | — | — |\n")

    # ---- View 2: full upper-triangle matrix ----
    out.append("\n## Full upper-triangle similarity matrix\n\n")
    out.append(
        "Columns and rows are labelled with `<reviewer_id>.c<idx>` "
        "(the idx is 1-based within that reviewer's own comment list, "
        "same ordering as `review_data.md`). Diagonal entries are "
        "always 1.000 and are omitted. Lower-triangle entries are the "
        "mirror of upper-triangle and are also omitted.\n\n"
    )
    out.append("| | " + " | ".join(labels) + " |\n")
    out.append("|---" + "|---" * n + "|\n")
    for i in range(n):
        cells: List[str] = []
        for j in range(n):
            if j <= i:
                cells.append("")
            else:
                cells.append(f"{float(matrix[i][j]):.3f}")
        out.append(f"| **{labels[i]}** | " + " | ".join(cells) + " |\n")

    return "".join(out)
