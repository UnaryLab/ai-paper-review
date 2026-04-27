"""LLM-only human-review conversion: markdown parsing, normalization,
and error surfacing. The heuristic extractors and format dispatcher are
gone — there is only one extraction path."""
from __future__ import annotations

import pytest

from ai_paper_review.validation import conversion as convert


LLM_MARKDOWN_OUTPUT = """# Review 1

**Reviewer ID:** Reviewer #1
**Overall Recommendation:** weak_reject
**Confidence:** 4

## Paper Summary
The paper proposes a new cache-coherent scheduler.

## Strengths
- Clean implementation
- Good writeup

## Comment 1
- **Severity:** major
- **Category:** evaluation
- **Section Reference:** Table 3
- **Summary:** Missing comparison against PriorWork from ASPLOS 2024
- **Description:** The evaluation is thin: only two workloads and no comparison against the recent PriorWork paper from ASPLOS 2024, which achieves a similar result.
- **Keywords:** baseline, prior art, comparison, evaluation

## Comment 2
- **Severity:** moderate
- **Category:** clarity
- **Section Reference:** Figure 4
- **Summary:** Figure 4 is illegible
- **Description:** Figure 4 is illegible — the axis labels overlap and the legend is missing.
- **Keywords:** figures, clarity, presentation

---

# Review 2

**Reviewer ID:** Reviewer #2
**Overall Recommendation:** weak_accept
**Confidence:** 3

## Comment 1
- **Severity:** moderate
- **Category:** novelty
- **Section Reference:** general
- **Summary:** Overstated novelty claim
- **Description:** The claim that this is the first cache-coherent design for this class of workloads is overstated; see the Xu 2022 paper.
- **Keywords:** novelty, prior art, Xu 2022
"""


def test_parses_multi_reviewer_markdown():
    """The LLM's output is parsed back into the canonical
    {actual_reviews: [...]} dict, with one entry per `# Review N` block.
    Reviewer labels and comment IDs are sanitized to [A-Za-z0-9_-]+ so
    downstream alignment-parser regexes can capture them cleanly."""
    parsed = convert.parse_llm_markdown(LLM_MARKDOWN_OUTPUT)
    reviews = parsed["actual_reviews"]
    assert len(reviews) == 2
    # "Reviewer #1" → "Reviewer_1" (no space, no '#')
    assert reviews[0]["reviewer_label"] == "Reviewer_1"
    assert reviews[1]["reviewer_label"] == "Reviewer_2"
    # Comment IDs use the sanitized label so they're regex-safe
    import re as _re
    for rv in reviews:
        for c in rv["comments"]:
            assert _re.fullmatch(r"[A-Za-z0-9_-]+", c["comment_id"]), \
                f"unsafe comment_id: {c['comment_id']!r}"
    assert len(reviews[0]["comments"]) == 2
    assert len(reviews[1]["comments"]) == 1


def test_description_copied_to_text_field():
    """The validator reads `text` on each comment; the parser must
    mirror `description → text` so alignment works downstream."""
    parsed = convert.parse_llm_markdown(LLM_MARKDOWN_OUTPUT)
    c = parsed["actual_reviews"][0]["comments"][0]
    assert "text" in c
    assert "PriorWork paper from ASPLOS 2024" in c["text"]


def test_verbatim_description_preserved_through_normalize():
    """A reviewer's exact words must survive the LLM → parse → normalize
    round-trip unchanged. The validator aligns on this text."""
    parsed = convert.parse_llm_markdown(LLM_MARKDOWN_OUTPUT)
    normalized = convert.normalize_extracted(parsed)
    text = normalized["actual_reviews"][0]["comments"][0]["text"]
    assert "only two workloads and no comparison against the recent PriorWork paper from ASPLOS 2024" in text


def test_handles_fenced_output():
    """LLMs sometimes wrap markdown in ```markdown fences even when told
    not to. The parser strips them."""
    fenced = "```markdown\n" + LLM_MARKDOWN_OUTPUT + "\n```"
    parsed = convert.parse_llm_markdown(fenced)
    assert len(parsed["actual_reviews"]) == 2


def test_normalize_fills_missing_markdown_fields():
    """When the LLM omits section_reference, summary, or keywords, normalize
    derives them from the comment text so the output markdown is complete."""
    raw = {
        "actual_reviews": [{
            "reviewer_label": "Reviewer #1",
            "comments": [{
                "severity": "major",
                "category": "evaluation",
                "text": "The evaluation in Section 4.2 is too narrow; more workloads are needed.",
                # No section_reference / summary / keywords
            }],
        }],
    }
    norm = convert.normalize_extracted(raw)
    c = norm["actual_reviews"][0]["comments"][0]
    assert c["section_reference"] == "Section 4.2"
    assert c["summary"].startswith("The evaluation in Section")
    assert c["keywords"] == []


def test_normalize_snaps_severity_to_vocab():
    """Unknown severity values default to 'moderate'."""
    raw = {
        "actual_reviews": [{
            "reviewer_label": "Reviewer #1",
            "comments": [
                {"severity": "CRITICAL", "text": "a"},  # wrong case — snap
                {"severity": "showstopper", "text": "b"},  # unknown — default
                {"severity": "minor", "text": "c"},
            ],
        }],
    }
    norm = convert.normalize_extracted(raw)
    sevs = [c["severity"] for c in norm["actual_reviews"][0]["comments"]]
    assert sevs == ["moderate", "moderate", "minor"]


def test_normalize_drops_unknown_category():
    """Unknown categories become empty strings so the serializer omits
    the Category: line instead of emitting nonsense. The allowed vocab
    now comes from the reviewer DB's Validation Attribution Tables —
    pass it explicitly to exercise enforcement."""
    raw = {
        "actual_reviews": [{
            "reviewer_label": "Reviewer #1",
            "comments": [{"severity": "moderate", "category": "frobniz", "text": "a"}],
        }],
    }
    norm = convert.normalize_extracted(
        raw, category_vocab=["evaluation", "methodology"],
    )
    assert norm["actual_reviews"][0]["comments"][0]["category"] == ""


def test_normalize_requires_actual_reviews_list():
    with pytest.raises(ValueError, match="missing 'actual_reviews' list"):
        convert.normalize_extracted({"something_else": []})


def test_legacy_heuristic_symbols_are_gone():
    """The heuristic extractors, format dispatcher, and JSON parser were
    ripped out — confirm they don't leak back in."""
    for name in [
        "_heuristic_extract", "_hotcrp_extract", "_openreview_extract",
        "_generic_extract", "_auto_detect_format", "_looks_like_hotcrp",
        "_looks_like_openreview", "SUPPORTED_FORMATS", "_FORMAT_EXTRACTORS",
        "_safe_parse_json", "_normalize_recommendation", "_infer_severity",
        "_infer_category", "_build_review", "_build_comment",
        "_split_into_reviewers", "_extract_section", "_split_concerns",
    ]:
        assert not hasattr(convert, name), (
            f"{name} is still in convert.py — should have been removed")


def test_extract_header_field_is_lenient():
    """extract_header_field must handle every realistic LLM formatting
    drift — the same failure-class the similarity-matrix parser hit
    ("LLM output looks fine, parser returns empty"). If this test fails,
    somebody tightened the header pattern and broke silent-failure
    tolerance for any downstream code that reads reviewer / persona /
    recommendation / confidence / paper metadata."""
    from ai_paper_review.review.parsing import extract_header_field

    # Positive cases — all must resolve to "R001" (or equivalent value)
    positive = [
        ("canonical",         "**Reviewer ID:** R001"),
        ("no bold",           "Reviewer ID: R001"),
        ("bold before colon", "**Reviewer ID**: R001"),
        ("single asterisk",   "*Reviewer ID:* R001"),
        ("underscore emph",   "_Reviewer ID:_ R001"),
        ("bold value",        "Reviewer ID: **R001**"),
        ("dash bullet",       "- **Reviewer ID:** R001"),
        ("plus bullet",       "+ Reviewer ID: R001"),
        ("heading promoted",  "### Reviewer ID: R001"),
        ("mixed case",        "**reviewer id:** R001"),
        ("embedded in prose", "Some preamble.\n\n**Reviewer ID:** R001\n\nMore stuff."),
    ]
    for name, line in positive:
        got = extract_header_field(line, "Reviewer ID")
        assert got == "R001", f"[{name}] expected 'R001', got {got!r} from {line!r}"

    # Negative cases — must return "" (not a false positive)
    for name, line in [
        ("label absent",        "some text without the label"),
        ("label only in prose", "**Note:** The reviewer ID is not set."),
        ("empty string",        ""),
    ]:
        got = extract_header_field(line, "Reviewer ID")
        assert got == "", f"[{name}] expected empty, got {got!r} from {line!r}"


def test_no_prompt_ever_asks_an_llm_for_json():
    """Hard-locked invariant across the whole module: no LLM prompt in
    the codebase asks for JSON output, and no public doc tells users the
    converter produces JSON. If this test fails, somebody added a prompt
    that tells an LLM to emit JSON — revert it and use markdown instead."""
    import pathlib
    import re as _re
    repo = pathlib.Path(__file__).resolve().parent.parent
    src_root = repo / "src" / "ai_paper_review"
    docs_root = repo / "docs"
    readme = repo / "README.md"

    # Patterns that would indicate "emit JSON" instructions to an LLM or
    # "converter produces JSON" claims in docs. Each entry is a regex with
    # word-boundaried `json` so substrings like `jsonify` (Flask),
    # `json.loads` (parser), or `.json` (file suffix — our calibration
    # deltas ARE JSON files, legitimately) do NOT match.
    banned_patterns = [
        r"\bemit\s+(?:strict\s+)?json\b",
        r"\breturn\s+(?:strict\s+)?json\b",
        r"\brespond\s+(?:with|in)\s+json\b",
        r"\boutput\s+(?:only\s+the\s+|strict\s+)?json\b",
        r"\bproduce\s+(?:strict\s+)?json\b",
        r"\bgenerate\s+(?:strict\s+)?json\b",
        r"\bvalid\s+json\b",
        r"\bjson\s+matching\s+this\s+schema\b",
        r"\bstructured\s+json\s+output\b",
        r"\bjson\s+format\s+specification\b",
        # Doc-level claims that convert writes JSON (it writes markdown)
        r"\btext[- ]to[- ]json\s+converter\b",
        r"\breads?\s+json\s+produced\s+by\s+`?ai[- ]paper[- ]review[- ]convert`?",
        r"\bai[- ]paper[- ]review[- ]convert[^.\n]{0,80}\bjson\b",
    ]
    compiled = [_re.compile(p, _re.IGNORECASE) for p in banned_patterns]

    targets = list(src_root.rglob("*.py"))
    targets += list(docs_root.rglob("*.md"))
    if readme.exists():
        targets.append(readme)

    offenders = []
    for f in targets:
        text = f.read_text()
        for rx in compiled:
            m = rx.search(text)
            if m:
                offenders.append(
                    f"{f.relative_to(repo)}: matches {rx.pattern!r} "
                    f"(at {m.group(0)!r})"
                )
    if offenders:
        raise AssertionError(
            "Found text asking an LLM for JSON output or claiming the "
            "converter produces JSON. Convert to markdown:\n  "
            + "\n  ".join(offenders)
        )
