# AI Review Output Format

This document describes the markdown format each AI reviewer produces, and the shape of the two report files the pipeline writes at the end of a run:

- `review_data.md` — structured per-reviewer output (machine-readable; the canonical artifact used by validation and re-ingestion).
- `review_report.md` — the human-facing report: selected-reviewer table, per-reviewer recommendation table, and ranked comment clusters.

**This document covers `review_data.md` — the structured one.** The aggregate `review_report.md` is prose rendered from it and has no parser requirements.

The same format is used for:

1. AI reviewer output (one review per reviewer in the selected pool).
2. Human reviews after the validation pipeline's human-review conversion stage reshapes them into this schema.
3. The canonical input to `ai-paper-review-validate`'s alignment stage.

---

## 1. File structure

A single `.md` file contains N review blocks, concatenated. Each review block starts with `# Review` and contains a header of bold-label lines followed by one or more `## Comment N` blocks.

Multi-reviewer files concatenate blocks with a `---` separator:

```markdown
# Review
**Reviewer ID:** R042
...
## Comment 1
...
## Comment 2
...

---

# Review
**Reviewer ID:** R087
...
## Comment 1
...
```

For single-reviewer files (what each reviewer produces, before aggregation), there's no separator.

---

## 2. Review header

Below the `# Review` heading, a block of bold-label lines carries reviewer and recommendation metadata. Each label appears on its own line with the format `**<Label>:** <value>`.

| Label                     | Expected | Type              | Description                                                        |
|---------------------------|----------|-------------------|--------------------------------------------------------------------|
| `Reviewer ID`             | yes      | string            | e.g. `R042`. Converted human reviews use the reviewer label from the source file (non-alphanumeric runs replaced by `_`), or `Reviewer_1`, `Reviewer_2`, etc. when none is given. |
| `Domain`                  | yes      | string            | One of the database's domain names.                                |
| `Persona`                 | yes      | string            | One of the database's persona names.                               |
| `Topic Relevance`         | no       | float in [0, 1]   | Selector's similarity score between the paper and this reviewer.   |
| `Overall Recommendation`  | yes      | enum              | `strong_accept` / `accept` / `weak_accept` / `borderline` / `weak_reject` / `reject` / `strong_reject`. |
| `Confidence`              | no       | int 1–5           | Reviewer's self-rated confidence in the review.                    |
| `Recommendation`          | no       | enum              | Alias for `Overall Recommendation` used by converted human reviews.|

"Expected" means the pipeline and downstream consumers rely on the field. The parser tolerates every one of these missing — it falls back to empty strings for text fields, `0.5` for `Topic Relevance`, `3` for `Confidence`, and empty for the recommendation — so malformed LLM output doesn't crash the pipeline. But a review missing `Reviewer ID`, `Domain`, `Persona`, or `Overall Recommendation` produces degraded downstream output: clustering still works, but the selected-reviewer table, per-reviewer recommendation table, and validation alignment results go blank or show `n/a`.

For converted human reviews, `Reviewer ID` is the sanitized source label or `Reviewer_<n>` rather than `R###`. All other labels are the same.

### Optional sections

Human-review conversions may add three optional sections between the header and the first `## Comment N` block. These are parsed if present and ignored if absent:

```markdown
## Paper Summary
<free-form prose summarizing what the paper proposes; parsed and kept,
not rendered in the report>

## Strengths
- Short bullet describing a strong point
- Another bullet
- ...

## Sub-Ratings
- novelty: 3
- clarity: 4
- experimental_rigor: 2
- ...
```

- `Paper Summary` — free-form text; parsed as a single string into `result["paper_summary"]`.
- `Strengths` — bullet list; each `- ...` line becomes an entry in `result["strengths"]`.
- `Sub-Ratings` — `- key: value` bullet list; integers are parsed as ints, other values kept as strings. Populates `result["sub_ratings"]` as a dict.

AI reviewers don't emit these sections; they exist so that human-review conversions can preserve signal that has no per-comment anchor.

---

## 3. Comment blocks

After the header, each review contains one or more comment blocks. Every comment starts with `## Comment N` (where N is 1-indexed) and carries six structured fields as bulleted lines.

```markdown
## Comment 1
- **Severity:** major
- **Category:** evaluation
- **Section Reference:** Table 3
- **Summary:** Missing baseline comparison with prior work
- **Description:** The paper reports a 1.8× speedup over the "unoptimized
  baseline" but does not compare against the best published prior method
  (Ref [12], 2023). Without this comparison the claimed contribution is
  not quantifiable. Authors should add Ref [12]'s Method-A to Table 3 on
  the same MLPerf workloads.
- **Keywords:** baseline, prior art, MLPerf, comparison
```

The six fields above (Severity, Category, Section Reference, Summary, Description, Keywords) are what every AI reviewer prompt requires. The parser also accepts an optional `Suggestion:` field. The bundled reviewer prompts do not ask for it; the fix suggestion is part of Description.

### Field-by-field

| Field               | Expected | Type              | Default if missing | Semantics                                                                  |
|---------------------|----------|-------------------|--------------------|----------------------------------------------------------------------------|
| `Severity`          | yes      | enum              | `"minor"`          | `major` / `moderate` / `minor`. Drives the importance weighting in clustering. Unknown values fall back to `minor`. |
| `Category`          | yes      | slug              | `"general"`        | Lowercase, short: `novelty` / `methodology` / `evaluation` / `reproducibility` / `clarity` / `scope` / `ethics` / `security` / etc. Used for persona-alignment analysis. |
| `Section Reference` | yes      | string            | `"general"`        | Anchor in the paper the comment refers to: section title, figure/table number, equation number, or a quoted phrase. |
| `Summary`           | yes      | one-line string   | derived            | The comment in ≤ 15 words. Drives clustering similarity. If missing, the parser derives one from the first sentence of `Description` (or, for free-form comment blocks, from the prose body). |
| `Description`       | yes      | multi-line string | derived            | Full critique plus what the authors should do about it. 2–4 sentences. Cites specific paper content. If missing but the comment block contains free-form prose, the parser uses the prose as the description. |
| `Keywords`          | yes      | comma-separated   | `[]`               | 2–5 topic tags. Used for cross-reviewer clustering.                        |
| `Suggestion`        | no       | multi-line string | (omitted)          | Optional field, parsed if present. The bundled reviewer prompts put the fix recommendation in `Description` instead. |

"Expected" means the field is part of the canonical template the bundled reviewer system prompts emit, and downstream stages assume it exists. The parser is forgiving: every field has a fallback so malformed LLM output never crashes ingestion, but a review where every comment is missing both `Summary` and `Description` is effectively empty and will trigger the retry-up-to-5-times loop in the review worker.

### Severity weights

The ranker uses these weights when scoring clusters:

| Severity   | Weight | Meaning                                     |
|------------|--------|---------------------------------------------|
| `major`    | 3.0    | Would block acceptance                      |
| `moderate` | 2.0    | Requires significant revision               |
| `minor`    | 1.0    | Improvement but not blocking                |

Cluster score = `num_distinct_reviewers × (0.5·avg_severity + 0.5·max_severity)`.

### Parsing notes

The parser is lenient about:
- Leading/trailing whitespace
- Heading depth (`## Comment 1`, `### Comment 1`, and `#### Comment 1` all work)
- Bold-label variants (`- **Label:**`, `**Label:**`, or plain `Label:`)
- Blank lines between fields

It's strict about:
- The `## Comment N` heading: the word "Comment" (any case) followed by a number, with an optional colon after the number
- Each field's label appearing verbatim (with the exception of the bold variants above)
- Multi-line `Description` text ending at the next `- **Label:**` or the next `## Comment` heading

When no `## Comment` headings are present, the parser returns zero comments. For LLM reviewer output this counts as unusable output and triggers the markdown-repair pass and retries. A `## Comment N` block with no structured fields keeps its prose as the Description, with default Severity and Category.

---

## 4. The two output artifacts

### `review_data.md`

The canonical structured output. Every review from every reviewer that ran on the paper is concatenated with `---` separators. This is what:

- The validation flow consumes as "the AI review" to compare against human reviews.
- The validation pipeline's human-review conversion stage emits when reshaping raw human reviews into this schema (driven by the web UI's validation flow).
- External tools can parse with the same lenient parser.

Schema exposed after parsing (per-review dict):

```python
{
    "reviewer_id":            "R042",
    "domain":                 "AI/ML Systems",
    "persona":                "Methodology Critic",
    "topic_relevance":        0.85,
    "overall_recommendation": "weak_reject",
    "recommendation":         "weak_reject",   # alias for overall_recommendation
    "confidence":             3,
    # present only when the review block includes the optional sections:
    # "paper_summary": "...",
    # "strengths": ["..."],
    # "sub_ratings": {"soundness": 3, ...},
    "comments": [
        {
            "comment_id":        "R042-C1",    # "<reviewer_id>-C<index>"
            "severity":          "major",
            "category":          "evaluation",
            "section_reference": "Table 3",
            "summary":           "Missing baseline comparison",
            "description":       "...",
            "text":              "...",         # alias for description; used by the validation pipeline
            "suggestion":        "",            # empty unless the block has a Suggestion field
            "keywords":          ["baseline", "prior art", "MLPerf", "comparison"],
        },
        # ...
    ],
}
```

### `review_report.md`

The human-facing report, rendered from the structured data above. Structure (in order):

1. **Provenance block**: a `<!-- provenance -->` marker, then LLM provider / model, base URL, launch and end timestamps with duration, and the format-fix retries count (`X of N reviewer(s)`), followed by a `---` rule.
2. **Disclaimer block**: intended-use warning and description of what was analyzed (full PDF or extracted text, depending on provider).
3. **Paper**: title and truncated abstract.
4. **Selected Reviewers** table: ID, Domain, Persona, Selection Relevance score for every reviewer that ran.
5. **Individual Recommendations** table: per-reviewer Overall Recommendation, Confidence, and comment count.
6. **Ranked Review Issues**: one `### #N [SEVERITY] Summary` section per cluster, ordered by commonality × importance score. Each cluster shows score, cluster size, distinct reviewer count, category, section reference, and the representative comment's description. Clusters with more than one member include a collapsible `<details>` block listing the other phrasings.

There is no separate format spec for this file — it is prose/markdown rendered by the pipeline and is not parsed by any downstream stage.

---

## 5. Producing valid output (for humans writing or converting reviews)

If you're writing a format converter or hand-editing a human review to the AI format, the key things to get right:

1. **Always include `Reviewer ID`, `Domain`, `Persona`, `Overall Recommendation`.** Missing any of these makes the reviewer opaque to the cross-paper aggregation module.
2. **Every comment needs a `Section Reference`.** It tells the reader where in the paper the comment applies. If a human review comment is genuinely un-anchored, use `Section Reference: (general)` explicitly rather than leaving the field blank.
3. **`Summary` drives clustering.** Phrase it as a single declarative sentence that another reviewer raising the same issue would plausibly echo.
4. **`Keywords` drive cross-review similarity.** Include the specific technical terms from the paper (method names, table/figure numbers, tool names). Avoid generic words like "improvement" or "unclear".

The validation pipeline's human-review conversion stage produces valid output from free-text reviews; the lenient parser handles variant formatting. You don't need to match the format byte-for-byte — just ensure the required labels appear and each comment has an anchor.
