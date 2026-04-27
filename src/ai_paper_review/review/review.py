"""Main review entry point.

Two roles in one file:

1. **Pipeline orchestration** — ``ReviewState`` typed dict, LangGraph
   ``build_graph()`` wiring, ``run_linear()`` fallback, and the
   top-level ``node_ingest_pdf`` node that dispatches to the right PDF
   extractor based on the active provider.
2. **CLI entry** — ``main()`` is the ``ai-paper-review-review`` console
   script: parses flags, pushes provider/model overrides into env vars,
   runs the graph, writes ``review_report.md`` + per-reviewer
   ``review_data.md`` next to the PDF.

The seven pipeline nodes live in the other submodules of this package
(``reviewer_db``, ``pdf_ingestion``, ``selection``,
``reviewer_dispatching``, ``parsing``, ``clustering``, ``ranking``).
This file just wires them together.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple, TypedDict

from .clarity import node_run_clarity_review
from .clustering import node_cluster_comments
from .constants import (
    DEFAULT_N_REVIEWERS,
    MAX_N_REVIEWERS,
    MIN_N_REVIEWERS,
    RECOMMENDED_MAX_N_REVIEWERS,
    RECOMMENDED_MIN_N_REVIEWERS,
)
from .parsing import review_dict_to_markdown
from .pdf_ingestion import extract_paper_summary, extract_pdf_for_provider
from .ranking import node_format_report, node_rank_clusters
from .reviewer_db import Reviewer
from .reviewer_dispatching import node_run_reviewers
from .selection import node_load_db, node_select_reviewers

logger = logging.getLogger("review_system")


class ReviewState(TypedDict, total=False):
    pdf_path: str
    db_path: str
    paper: Dict[str, str]
    reviewers: List[Reviewer]
    selected: List[Tuple[Reviewer, float]]
    # Full reviewer-vs-paper ranking before persona-diversifying selection
    # truncates it to top-N. Used to write selection_similarities.md.
    selection_similarities: List[Tuple[Reviewer, float]]
    raw_reviews: List[Dict[str, Any]]
    all_comments: List[Dict[str, Any]]
    clusters: List[Dict[str, Any]]
    # Pairwise comment-similarity matrix + metadata, stashed by
    # node_cluster_comments for the clustering_similarities.md artifact.
    clustering_similarities: Dict[str, Any]
    ranked: List[Dict[str, Any]]
    report_md: str
    # Always-on clarity reviewer's output. Orthogonal to the persona
    # pipeline — never added to all_comments, raw_reviews, or the
    # validation flow. Written to writing_clarity_review.md.
    clarity_review: Dict[str, Any]
    # User-chosen per run; clamped to [MIN, MAX] by node_select_reviewers.
    n_reviewers: int
    # Per-job overrides; preferred over env-var mutation so concurrent
    # web-UI jobs can't race each other.
    llm_provider: str
    llm_model: str
    # ISO8601 UTC timestamp captured at pipeline start. Used when
    # writing output files so each artifact carries an end-to-end
    # timing footprint alongside LLM provenance.
    launched_at: str
    # ISO8601 UTC timestamp captured by ``node_format_report`` so the
    # in-body ``Ended:`` line and the prepended provenance block agree
    # to the second. Callers read this from state after the graph runs.
    ended_at: str


def node_ingest_pdf(state: ReviewState) -> ReviewState:
    from ai_paper_review.llm.config import load_config

    logger.info("Ingesting PDF: %s", state["pdf_path"])
    cfg = load_config()
    provider = state.get("llm_provider") or cfg.review_provider

    text = extract_pdf_for_provider(state["pdf_path"], provider)
    state["paper"] = extract_paper_summary(text)
    logger.info("Extracted title: %s", state["paper"]["title"][:80])
    return state


def build_graph():
    """Build the LangGraph StateGraph; return None if langgraph isn't installed
    so the caller can fall back to :func:`run_linear`.
    """
    try:
        from langgraph.graph import StateGraph, END
    except ImportError:
        logger.warning("langgraph not installed; using linear fallback runner")
        return None

    g = StateGraph(ReviewState)
    g.add_node("ingest_pdf", node_ingest_pdf)
    g.add_node("load_db", node_load_db)
    g.add_node("select_reviewers", node_select_reviewers)
    g.add_node("run_reviewers", node_run_reviewers)
    g.add_node("run_clarity_review", node_run_clarity_review)
    g.add_node("cluster_comments", node_cluster_comments)
    g.add_node("rank_clusters", node_rank_clusters)
    g.add_node("format_report", node_format_report)

    g.set_entry_point("ingest_pdf")
    g.add_edge("ingest_pdf", "load_db")
    g.add_edge("load_db", "select_reviewers")
    # Clarity reviewer runs BEFORE the parallel persona reviewers so its
    # single sequential call seeds the provider's prompt cache with the
    # (shared system + PDF) prefix. The N persona reviewers that dispatch
    # next all hit the warm cache for the PDF portion (big tokens) and
    # only pay the un-cached tail (per-persona text). Clarity's output
    # is still orthogonal to the persona pipeline — never added to
    # all_comments / raw_reviews.
    g.add_edge("select_reviewers", "run_clarity_review")
    g.add_edge("run_clarity_review", "run_reviewers")
    g.add_edge("run_reviewers", "cluster_comments")
    g.add_edge("cluster_comments", "rank_clusters")
    g.add_edge("rank_clusters", "format_report")
    g.add_edge("format_report", END)

    return g.compile()


def run_linear(initial: ReviewState) -> ReviewState:
    """Sequential fallback runner if langgraph isn't installed."""
    state = dict(initial)
    for node in [
        node_ingest_pdf,
        node_load_db,
        node_select_reviewers,
        node_run_clarity_review,  # seeds the provider prompt cache (see build_graph)
        node_run_reviewers,
        node_cluster_comments,
        node_rank_clusters,
        node_format_report,
    ]:
        state = node(state)  # type: ignore
    return state  # type: ignore


def main():
    """``ai-paper-review-review`` — run the review pipeline on one PDF."""
    from ai_paper_review import default_db_path
    default_db = default_db_path()

    ap = argparse.ArgumentParser(
        description="Paper review system using LangGraph + 200 AI reviewers",
    )
    ap.add_argument("--pdf", required=True, help="Path to the draft PDF")
    ap.add_argument(
        "--db",
        default=str(default_db),
        help=f"Reviewer database markdown (default: bundled {default_db.name})",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Where to write the markdown report (default: <pdf_stem>_review.md next to the PDF)",
    )
    ap.add_argument(
        "--data-out",
        default=None,
        help="Where to write per-reviewer data (default: <pdf_stem>_review_data.md next to the PDF)",
    )
    ap.add_argument(
        "--similarities-out",
        default=None,
        help=(
            "Where to write the full reviewer-vs-paper similarity table "
            "(default: <pdf_stem>_selection_similarities.md next to the PDF)"
        ),
    )
    ap.add_argument(
        "--clustering-similarities-out",
        default=None,
        help=(
            "Where to write the pairwise comment-clustering similarity "
            "table (default: <pdf_stem>_clustering_similarities.md next "
            "to the PDF)"
        ),
    )
    ap.add_argument(
        "--clarity-out",
        default=None,
        help=(
            "Where to write the always-on writing-clarity reviewer "
            "output (default: <pdf_stem>_writing_clarity_review.md next "
            "to the PDF). Not merged into --data-out; never compared "
            "against human reviews during validation."
        ),
    )
    ap.add_argument(
        "--provider", default=None,
        help="Override LLM provider from config (anthropic|openai|google|xai|openai_compatible)",
    )
    ap.add_argument(
        "--model", default=None,
        help="Override LLM model from config (e.g. gpt-4o, gemini-2.5-pro, claude-sonnet-4-5-20250929)",
    )
    ap.add_argument(
        "--reviewers",
        type=int,
        default=DEFAULT_N_REVIEWERS,
        help=(
            f"Number of reviewers to select (default: {DEFAULT_N_REVIEWERS}, "
            f"recommended range: {RECOMMENDED_MIN_N_REVIEWERS}–"
            f"{RECOMMENDED_MAX_N_REVIEWERS}, hard range: "
            f"{MIN_N_REVIEWERS}..{MAX_N_REVIEWERS}). Each reviewer "
            f"emits 5–10 review comments via one LLM call, so wall-time "
            f"and cost scale linearly with this number. Below 5 loses "
            f"the cross-reviewer consensus signal that clustering relies "
            f"on; above 10 has diminishing returns."
        ),
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    pdf_path = Path(args.pdf).resolve()
    if args.out is None:
        args.out = str(pdf_path.with_name(f"{pdf_path.stem}_review.md"))
    if args.data_out is None:
        args.data_out = str(pdf_path.with_name(f"{pdf_path.stem}_review_data.md"))
    if args.similarities_out is None:
        args.similarities_out = str(
            pdf_path.with_name(f"{pdf_path.stem}_selection_similarities.md")
        )
    if args.clustering_similarities_out is None:
        args.clustering_similarities_out = str(
            pdf_path.with_name(f"{pdf_path.stem}_clustering_similarities.md")
        )
    if args.clarity_out is None:
        args.clarity_out = str(
            pdf_path.with_name(f"{pdf_path.stem}_writing_clarity_review.md")
        )

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.provider:
        os.environ["PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE"] = args.provider
    if args.model:
        os.environ["PAPER_REVIEW_REVIEW_MODEL_OVERRIDE"] = args.model

    logger.info("PDF:                    %s", args.pdf)
    logger.info("Database:               %s", args.db)
    logger.info("Report:                 %s", args.out)
    logger.info("Data:                   %s", args.data_out)
    logger.info("Selection similarities: %s", args.similarities_out)
    logger.info("Cluster similarities:   %s", args.clustering_similarities_out)
    logger.info("Clarity review:         %s", args.clarity_out)

    from ai_paper_review.provenance import format_provenance, now_iso
    from ai_paper_review.llm.config import load_config as _load_config
    launched_at = now_iso()

    initial: ReviewState = {
        "pdf_path": args.pdf,
        "db_path": args.db,
        "n_reviewers": int(args.reviewers),
        "launched_at": launched_at,
    }
    graph = build_graph()
    final = graph.invoke(initial) if graph is not None else run_linear(initial)

    _cfg = _load_config()
    _active_provider = final.get("llm_provider") or _cfg.review_provider
    _active_model = final.get("llm_model") or _cfg.review_model
    provenance = format_provenance(
        provider=_active_provider,
        model=_active_model,
        base_url=_cfg.resolve_base_url(_active_provider),
        launched_at=launched_at,
        ended_at=final.get("ended_at"),
        format_fix_retries=final.get("n_format_repairs"),
        n_reviewers_total=final.get("n_reviewers_total"),
    )

    # Provenance is prepended ONLY to the main review_report.md — the
    # other artifacts (review_data / similarities tables / clarity
    # review) are machine-readable or cross-referenced with the report
    # and don't need the same metadata banner repeated on every file.
    Path(args.out).write_text(provenance + final["report_md"])

    from .selection import format_selection_similarities_md
    Path(args.similarities_out).write_text(
        format_selection_similarities_md(
            final["paper"],
            final.get("selection_similarities", []),
            final["selected"],
            n_requested=final.get("n_reviewers", len(final["selected"])),
        )
    )

    from .clustering import format_clustering_similarities_md
    Path(args.clustering_similarities_out).write_text(
        format_clustering_similarities_md(
            final["paper"],
            final.get("all_comments", []),
            final.get("clustering_similarities", {}),
        )
    )

    clarity = final.get("clarity_review") or {}
    clarity_copy = dict(clarity)
    clarity_copy["reviewer_id"] = clarity.get("_reviewer_id", "")
    clarity_copy["persona"] = clarity.get("_persona", "")
    clarity_copy["domain"] = clarity.get("_domain", "")
    Path(args.clarity_out).write_text(review_dict_to_markdown(clarity_copy))

    raw_md_lines = [
        "# AI Review Output", "",
        f"**Title:** {final['paper']['title']}", "",
    ]
    for rv in final["raw_reviews"]:
        raw_md_lines.append("---")
        raw_md_lines.append("")
        # Prefer the authoritative ``_reviewer_id`` / ``_persona`` /
        # ``_domain`` from the DB over whatever the LLM emitted (which
        # can be blank or wrong).
        rv_copy = dict(rv)
        rv_copy["reviewer_id"] = (
            rv.get("_reviewer_id") or rv.get("reviewer_id") or ""
        )
        rv_copy["persona"] = rv.get("_persona") or rv.get("persona") or ""
        rv_copy["domain"]  = rv.get("_domain")  or rv.get("domain")  or ""
        raw_md_lines.append(review_dict_to_markdown(rv_copy))
    Path(args.data_out).write_text("\n".join(raw_md_lines))

    print(
        f"Wrote {args.out}, {args.data_out}, {args.similarities_out}, "
        f"{args.clustering_similarities_out}, and {args.clarity_out}"
    )
    print(f"Top issue: #{final['ranked'][0]['rank']} "
          f"{final['ranked'][0]['representative'].get('summary', '')}"
          if final["ranked"] else "No issues reported.")


if __name__ == "__main__":
    main()
