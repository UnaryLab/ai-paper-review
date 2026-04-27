"""Cluster ranking + final markdown report.

The ranking score combines two signals:

* **Commonality** — how many distinct reviewer agents independently
  raised the same point. More reviewers agreeing = more important.
* **Importance** — average + max severity weight of the cluster
  members.

Both are multiplied; the result orders clusters from "everyone flagged
this as critical" down to "one reviewer mentioned a nit".
"""
from __future__ import annotations

from typing import List


def node_rank_clusters(state):
    """Rank clusters by commonality × importance.

    score = num_distinct_reviewers * (0.5 * avg_severity + 0.5 * max_severity)
    """
    ranked = sorted(
        state["clusters"],
        key=lambda c: -(
            c["num_distinct_reviewers"]
            * (0.5 * c["avg_severity_weight"] + 0.5 * c["max_severity_weight"])
        ),
    )
    for i, c in enumerate(ranked):
        c["rank"] = i + 1
        c["score"] = round(
            c["num_distinct_reviewers"]
            * (0.5 * c["avg_severity_weight"] + 0.5 * c["max_severity_weight"]),
            3,
        )
    state["ranked"] = ranked
    return state


def node_format_report(state):
    paper = state["paper"]
    ranked = state["ranked"]
    selected = state["selected"]
    raw = state["raw_reviews"]

    # Stash ``ended_at`` so the caller can thread the same timestamp
    # into the prepended provenance block — both displays then agree
    # to the second. Also stash the format-repair tally so the caller
    # can pass it to ``format_provenance()``. LLM provider / model /
    # timing themselves are rendered in the prepended block, not here.
    from ai_paper_review.provenance import now_iso
    state.setdefault("ended_at", now_iso())
    clarity = state.get("clarity_review") or {}
    state["n_format_repairs"] = (
        sum(1 for rv in raw if rv.get("_format_repaired"))
        + (1 if clarity.get("_format_repaired") else 0)
    )
    state["n_reviewers_total"] = len(raw) + (1 if clarity else 0)

    lines: List[str] = []
    lines.append("# Review Report\n")
    lines.append(
        "> **⚠️ Intended use:** This report is a **draft-polishing aid for papers you are\n"
        "> writing**. It is **not** a peer-review generator. Most venues have strict policies\n"
        "> against using LLMs in assigned reviews, due to concerns about bias, hallucination,\n"
        "> and the potential for compromising the integrity of the peer-review process. Please\n"
        "> use it at your own discretion, and indicate when you have used it.\n"
        ">\n"
        "> Every comment is a **suggestion to evaluate**, not a finding to accept. AI reviewers\n"
        "> hallucinate, miss context, and over-confidently flag non-issues. Expect to reject\n"
        "> roughly half of what you see. Use at your own discretion.\n"
        ">\n"
        "> **What was analyzed:** Depending on the LLM provider, either the full PDF was\n"
        "> analyzed directly, or only the **text and tables** (extracted by pypdf and\n"
        "> MarkItDown). Expect the reviews to focus on methodology description, claims,\n"
        "> experimental design, evaluation setup, and writing quality.\n\n"
    )
    lines.append(f"**Paper:** {paper['title']}\n\n")
    lines.append(f"**Abstract:** {paper['abstract'][:800]}...\n\n")
    lines.append("---\n\n## Selected Reviewers\n")
    lines.append("| ID | Domain | Persona | Selection Relevance |\n|---|---|---|---|\n")
    for r, s in selected:
        lines.append(f"| {r.id} | {r.domain} | {r.persona} | {s:.3f} |\n")

    # Per-reviewer recommendation summary
    lines.append("\n## Individual Recommendations\n")
    lines.append("| Reviewer | Recommendation | Confidence | # Comments |\n|---|---|---|---|\n")
    for rv in raw:
        lines.append(
            f"| {rv.get('_reviewer_id', '?')} | {rv.get('overall_recommendation', 'n/a')} | "
            f"{rv.get('confidence', 'n/a')} | {len(rv.get('comments', []))} |\n"
        )

    # Ranked clusters
    lines.append("\n## Ranked Review Issues (clustered across reviewers)\n")
    lines.append(
        "Clusters are ordered by **commonality × importance**. Size = how many "
        "comments, distinct reviewers = how many different reviewer agents raised it.\n\n"
    )
    for c in ranked:
        rep = c["representative"]
        sev = rep.get("severity", "minor")
        lines.append(
            f"### #{c['rank']} [{sev.upper()}] {rep.get('summary', '(no summary)')}\n"
        )
        lines.append(f"- **Score:** {c['score']} | **Cluster size:** {c['size']} | "
                     f"**Distinct reviewers:** {c['num_distinct_reviewers']} "
                     f"({', '.join(c['reviewers'])})\n")
        lines.append(f"- **Category:** {rep.get('category', 'n/a')} | "
                     f"**Section:** {rep.get('section_reference', 'general')}\n")
        lines.append(f"\n**Description:** {rep.get('description', '')}\n\n")

        if c["size"] > 1:
            lines.append("<details><summary>Other phrasings of this issue</summary>\n\n")
            for m in c["members"]:
                if m is rep:
                    continue
                lines.append(
                    f"- *[{m['_reviewer_id']} / {m['_persona']}]* "
                    f"({m.get('severity','?')}) {m.get('summary','')}\n"
                )
            lines.append("\n</details>\n\n")

    state["report_md"] = "".join(lines)
    return state
