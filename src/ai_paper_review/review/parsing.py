"""Markdown parsing + serialization for reviewer output.

Used in three places:

* parsing the LLM's per-reviewer markdown response
  (:func:`parse_review_markdown`, :func:`_parse_llm_output`),
* parsing a multi-reviewer file (used by validation to read
  human-review and AI-review markdowns),
* serializing back to the canonical format (:func:`review_dict_to_markdown`).

All parsers are intentionally forgiving — LLMs drift on bold markers,
heading levels, and field punctuation, and a strict parser would silently
drop everything it can't recognize. :func:`extract_header_field` accepts
the variants we've seen in the wild.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

from .constants import SEVERITY_WEIGHT


def extract_header_field(text: str, label: str) -> str:
    """Find a ``Label: value`` line in ``text`` and return ``value``.

    Mirrors the forgiving parse philosophy used by the batch-similarity
    parser: LLMs drift on formatting (bold markers, bullet prefixes,
    heading hashes), and a strict parser silently drops everything it
    can't recognize — which ends up as an empty header field and corrupt
    downstream state. This helper accepts every common variant, in order
    of preference:

    * ``**Label:** value``               (canonical — what the prompt asks for)
    * ``**Label**: value``               (bold before colon)
    * ``- **Label:** value``             (bulleted; bullets are equivalent to plain)
    * ``*Label:* value``                 (italic instead of bold)
    * ``Label: value``                   (no emphasis at all)
    * ``### Label: value``               (LLM promoted it to a heading)
    * ``Label: **value**``               (value itself is bolded — markers stripped)

    Case-insensitive on the label. Returns a stripped value without
    surrounding ``**`` / ``*`` markers, or ``""`` if the label isn't
    found anywhere.
    """
    if not text or not label:
        return ""
    esc = re.escape(label)
    pattern = re.compile(
        r"^[ \t]*(?:[-*+][ \t]+|#{1,6}[ \t]+)?"   # bullet or heading prefix
        r"(?:\*\*|__|\*|_)?"                       # opening emphasis
        + esc +
        r"(?:\*\*|__|\*|_)?"                       # closing emphasis before colon
        r"[ \t]*:[ \t]*"                           # colon
        r"(?:\*\*|__|\*|_)?"                       # opening emphasis on value
        r"(.+?)"                                   # value
        r"(?:\*\*|__|\*|_)?"                       # closing emphasis on value
        r"[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return ""
    return m.group(1).strip().strip("*").strip("_").strip()


def parse_review_markdown(text: str) -> Dict[str, Any]:
    """Parse a reviewer's markdown output into the same dict structure
    that the pipeline expects internally.

    Expected format::

        # Review
        **Reviewer ID:** R048
        **Domain:** AI/ML Hardware Accelerators
        **Persona:** Performance Specialist
        **Topic Relevance:** 0.85
        **Overall Recommendation:** weak_reject
        **Confidence:** 3

        ## Comment 1
        - **Severity:** major
        - **Category:** evaluation
        - **Section Reference:** Table 3
        - **Summary:** Missing baseline comparison
        - **Description:** The paper does not compare ...
        - **Suggestion:** Add comparisons against ...
        - **Keywords:** baseline, comparison

        ## Comment 2
        ...
    """
    result: Dict[str, Any] = {}

    def _extract_field(label: str) -> str:
        return extract_header_field(text, label)

    result["reviewer_id"] = _extract_field("Reviewer ID")
    result["domain"] = _extract_field("Domain")
    result["persona"] = _extract_field("Persona")

    tr = _extract_field("Topic Relevance")
    try:
        result["topic_relevance"] = float(tr)
    except (ValueError, TypeError):
        result["topic_relevance"] = 0.5

    result["overall_recommendation"] = _extract_field("Overall Recommendation").lower().replace(" ", "_")
    # Human reviews use "Recommendation" without "Overall".
    if not result["overall_recommendation"]:
        result["overall_recommendation"] = _extract_field("Recommendation").lower().replace(" ", "_")
    result["recommendation"] = result["overall_recommendation"]

    conf = _extract_field("Confidence")
    try:
        result["confidence"] = int(re.sub(r"[^\d]", "", conf or "3") or "3")
    except (ValueError, TypeError):
        result["confidence"] = 3

    summary_match = re.search(
        r"##\s+Paper\s+Summary\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    if summary_match:
        result["paper_summary"] = summary_match.group(1).strip()

    strengths_match = re.search(
        r"##\s+Strengths\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    if strengths_match:
        result["strengths"] = [
            line.lstrip("- ").strip()
            for line in strengths_match.group(1).strip().splitlines()
            if line.strip().startswith("-")
        ]

    subratings_match = re.search(
        r"##\s+Sub-Ratings\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    if subratings_match:
        sub = {}
        for line in subratings_match.group(1).strip().splitlines():
            line = line.lstrip("- ").strip()
            if ":" in line:
                k, v = line.split(":", 1)
                try:
                    sub[k.strip().lower()] = int(v.strip())
                except ValueError:
                    sub[k.strip().lower()] = v.strip()
        if sub:
            result["sub_ratings"] = sub

    # Accept 2–4 hash levels so we tolerate LLMs that pick the wrong level.
    comment_blocks = re.split(r"#{2,4}\s+Comment\s+\d+\s*:?\s*", text, flags=re.IGNORECASE)
    comment_blocks = comment_blocks[1:] if len(comment_blocks) > 1 else []

    _FIELD_LABELS = ["Severity", "Category", "Section Reference",
                     "Summary", "Description", "Suggestion", "Keywords"]

    comments = []
    rid = result.get("reviewer_id", "R000")
    for idx, block in enumerate(comment_blocks, start=1):
        # Trim anything after the next heading if the split regex captured
        # into an adjacent section (e.g. "## Sub-Ratings" after the last
        # comment).
        next_section = re.search(r"\n#{1,4}\s+\w", block)
        if next_section:
            block = block[:next_section.start()]

        def _field(label: str) -> str:
            """Extract a field, tolerating LLM formatting drift on
            bold/dash/colon placement; values may span multiple lines and
            terminate at the next field marker or heading."""
            other_labels = "|".join(re.escape(lab) for lab in _FIELD_LABELS if lab != label)
            terminator = (
                r"(?="
                r"\n[ \t]*-[ \t]*\*\*[A-Za-z]"      # "- **AnyLabel"
                r"|\n[ \t]*\*\*[A-Z]"                # "**AnyLabel"  (no dash)
                r"|\n[ \t]*-?\s*(?:" + other_labels + r")\s*\**\s*:"  # plain "Label:" or "- Label:"
                r"|\n#{1,4}\s"                       # any heading
                r"|\Z"
                r")"
            )
            pat = re.compile(
                r"(?:^|\n)[ \t]*-?[ \t]*\**\s*"
                + re.escape(label)
                + r"\s*\**\s*:\s*(?:\**\s*)?(.+?)"
                + terminator,
                re.IGNORECASE | re.DOTALL,
            )
            m = pat.search(block)
            if not m:
                return ""
            val = m.group(1).strip()
            val = val.strip("*").strip()
            return val

        kw_raw = _field("Keywords")
        keywords = [k.strip().strip('"').strip("'") for k in kw_raw.split(",") if k.strip()]
        description = _field("Description")
        summary = _field("Summary")
        suggestion = _field("Suggestion")

        # Fallback for free-form prose under "## Comment N" with no structured
        # fields: use the whole block as the description so clustering still
        # has something to work with.
        if not summary and not description:
            prose = block.strip()
            prose_lines = [
                ln for ln in prose.splitlines()
                if not re.match(
                    r"^\s*-?\s*\**\s*(" + "|".join(_FIELD_LABELS) + r")\s*\**\s*:",
                    ln, re.IGNORECASE,
                )
            ]
            prose = "\n".join(prose_lines).strip()
            if prose:
                description = prose
                summary = prose.split(". ")[0][:140]

        if description and not summary:
            summary = description.split(". ")[0][:140]

        sev_raw = (_field("Severity") or "").lower().strip()
        sev = sev_raw if sev_raw in SEVERITY_WEIGHT else "minor"

        comments.append({
            "comment_id": f"{rid}-C{idx}",
            "severity": sev,
            "category": _field("Category").lower() or "general",
            "section_reference": _field("Section Reference") or "general",
            "summary": summary,
            "description": description,
            "text": description,  # validator's human-review side reads this name
            "suggestion": suggestion,
            "keywords": keywords,
        })

    result["comments"] = comments
    return result


def review_dict_to_markdown(d: Dict[str, Any]) -> str:
    """Serialize a review dict to the standard markdown format.

    Handles both AI reviewer output and converted human reviews. Optional
    fields (paper_summary, strengths, sub_ratings) are included if present.
    """
    lines = ["# Review", ""]

    for label, key in [
        ("Reviewer ID", "reviewer_id"),
        ("Domain", "domain"),
        ("Persona", "persona"),
        ("Topic Relevance", "topic_relevance"),
        ("Overall Recommendation", "overall_recommendation"),
        ("Recommendation", "recommendation"),
        ("Confidence", "confidence"),
    ]:
        val = d.get(key)
        if val is not None and val != "":
            lines.append(f"**{label}:** {val}")
    lines.append("")

    if d.get("paper_summary"):
        lines += ["## Paper Summary", d["paper_summary"], ""]

    if d.get("strengths"):
        lines.append("## Strengths")
        for s in d["strengths"]:
            lines.append(f"- {s}")
        lines.append("")

    if d.get("sub_ratings"):
        lines.append("## Sub-Ratings")
        for k, v in d["sub_ratings"].items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    for i, c in enumerate(d.get("comments", []), start=1):
        lines.append(f"## Comment {i}")
        # Human reviews use 'text'; AI reviews use 'description'.
        desc = c.get("description") or c.get("text", "")
        for label, key, fallback in [
            ("Severity", "severity", None),
            ("Category", "category", None),
            ("Section Reference", "section_reference", None),
            ("Summary", "summary", None),
            ("Description", None, desc),
            ("Suggestion", "suggestion", None),
        ]:
            val = fallback if fallback is not None else c.get(key, "")
            if val:
                lines.append(f"- **{label}:** {val}")
        kw = c.get("keywords", [])
        if kw:
            kw_str = ", ".join(kw) if isinstance(kw, list) else kw
            lines.append(f"- **Keywords:** {kw_str}")
        lines.append("")

    return "\n".join(lines)


def _parse_llm_output(text: str) -> Dict[str, Any]:
    """Parse LLM reviewer output as markdown (## Comment headings)."""
    if not text or not text.strip():
        raise ValueError(
            "LLM returned an empty response (0 chars). This is not a parse error — "
            "the model produced no output. For copilot_sdk: check premium quota, "
            "`gh auth status`, and Copilot subscription/org policy. For API providers: "
            "check rate limits and any content-filter refusals."
        )
    parsed = parse_review_markdown(text)
    if parsed.get("comments"):
        return parsed
    raise ValueError(
        f"No ## Comment headings found in LLM output ({len(text)} chars). "
        f"The model returned text but not in the expected markdown format. "
        f"First 300 chars: {text[:300]!r}"
    )


def parse_multi_review_markdown(text: str) -> Dict[str, Any]:
    """Parse a markdown file containing paper metadata + multiple reviews.

    Format produced by ``ai_paper_review.validation.conversion.llm_extract``::

        # Converted Reviews
        **Paper ID:** my-paper
        **Title:** Some Title
        ...
        ---
        # Review 1
        **Recommendation:** accept
        ## Comment 1
        ...
        ---
        # Review 2
        ...

    Returns a dict with this structure::

        {"paper_id": ..., "title": ..., "actual_reviews": [...]}
    """
    result: Dict[str, Any] = {}

    header_end = text.find("\n---")
    header = text[:header_end] if header_end >= 0 else text[:500]

    def _meta(label: str) -> str:
        return extract_header_field(header, label)

    result["paper_id"] = _meta("Paper ID")
    result["title"] = _meta("Title")
    result["venue"] = _meta("Venue")
    result["pdf_path"] = _meta("PDF Path")
    result["source_format"] = _meta("Source Format")
    result["notes"] = _meta("Notes")

    review_blocks = re.split(r"(?:^|\n)(?:---\s*\n)?#\s+Review\s*\d*\s*\n", text, flags=re.IGNORECASE)
    review_blocks = [b for b in review_blocks[1:] if b.strip()]

    reviews = []
    for block in review_blocks:
        # Prepend so parse_review_markdown's split sees a top-level header.
        rv = parse_review_markdown("# Review\n" + block)
        reviews.append(rv)

    result["actual_reviews"] = reviews
    return result


def load_reviews_file(path: str) -> Dict[str, Any]:
    """Load a reviews markdown file."""
    return parse_multi_review_markdown(Path(path).read_text())
