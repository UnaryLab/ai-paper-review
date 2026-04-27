# AI Review Output Format

This document describes the markdown format each AI reviewer produces, and the shape of the two report files the pipeline writes at the end of a run:

- `review_data.md` — structured per-reviewer output (machine-readable; the canonical artifact used by validation and re-ingestion).
- `review_report.md` — the human-facing aggregated report (ranked comment clusters, per-domain summary).

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
| `Reviewer ID`             | yes      | string            | e.g. `R042`. Human reviews use `H01`, `H02`, etc.                  |
| `Domain`                  | yes      | string            | One of the database's domain names.                                |
| `Persona`                 | yes      | string            | One of the database's persona names.                               |
| `Topic Relevance`         | no       | float in [0, 1]   | Selector's similarity score between the paper and this reviewer.   |
| `Overall Recommendation`  | yes      | enum              | `strong_accept` / `accept` / `weak_accept` / `borderline` / `weak_reject` / `reject` / `strong_reject`. |
| `Confidence`              | no       | int 1–5           | Reviewer's self-rated confidence in the review.                    |
| `Recommendation`          | no       | enum              | Alias for `Overall Recommendation` used by converted human reviews.|

"Expected" means the pipeline and downstream consumers rely on the field. The parser tolerates every one of these missing — it falls back to empty strings for text fields, `0.5` for `Topic Relevance`, `3` for `Confidence`, and empty for the recommendation — so malformed LLM output doesn't crash the pipeline. But a review missing `Reviewer ID`, `Domain`, `Persona`, or `Overall Recommendation` produces degraded downstream output: clustering still works, but per-domain summaries and recommendation histograms go blank.

For human reviews, `Reviewer ID` uses an `H##` prefix rather than `R###`. All other labels are the same.

### Optional sections

Human-review conversions may add three optional sections between the header and the first `## Comment N` block. These are parsed if present and ignored if absent:

```markdown
## Paper Summary
<free-form prose summarizing what the paper proposes — used as context by
the validator's batch-similarity LLM call, not rendered in the report>

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

After the header, each review contains one or more comment blocks. Every comment starts with `## Comment N` (where N is 1-indexed) and carries five structured fields as bulleted lines.

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

### Field-by-field

| Field               | Expected | Type              | Default if missing | Semantics                                                                  |
|---------------------|----------|-------------------|--------------------|----------------------------------------------------------------------------|
| `Severity`          | yes      | enum              | `"minor"`          | `major` / `moderate` / `minor`. Drives the importance weighting in clustering. Unknown values fall back to `minor`. |
| `Category`          | yes      | slug              | `"general"`        | Lowercase, short: `novelty` / `methodology` / `evaluation` / `reproducibility` / `clarity` / `scope` / `ethics` / `security` / etc. Used for persona-alignment analysis. |
| `Section Reference` | yes      | string            | `"general"`        | Anchor in the paper the comment refers to: section title, figure/table number, equation number, or a quoted phrase. A missing anchor weakens validation scoring — comments marked `general` are treated as likely templated during calibration. |
| `Summary`           | yes      | one-line string   | derived            | The comment in ≤ 15 words. Drives clustering similarity. If missing, the parser derives one from the first sentence of `Description` (or, for free-form comment blocks, from the prose body). |
| `Description`       | yes      | multi-line string | derived            | Full critique plus what the authors should do about it. 2–6 sentences. Cites specific paper content. If missing but the comment block contains free-form prose, the parser uses the prose as the description. |
| `Keywords`          | yes      | comma-separated   | `[]`               | 3–7 topic tags. Used for cross-reviewer clustering.                        |

"Expected" means the field is part of the canonical template the bundled reviewer system prompts emit, and downstream stages assume it exists. The parser is forgiving: every field has a fallback so malformed LLM output never crashes ingestion, but a review where every comment is missing both `Summary` and `Description` is effectively empty and will trigger the retry-up-to-5-times loop in the review worker.

The AI reviewer prompt previously required a separate `Suggestion` field — this has been folded into `Description`, which now covers both the concern and the recommended fix. The parser still accepts and preserves a standalone `Suggestion:` field for backward compatibility with older review files, but new reviews do not emit it.

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
- The `## Comment` prefix (lowercase works, upper-case also works, but no trailing punctuation after "Comment N")
- Each field's label appearing verbatim (with the exception of the bold variants above)
- Multi-line `Description` text ending at the next `- **Label:**` or the next `## Comment` heading

When no `## Comment` headings are present, the parser falls back to treating the entire review body as a single "prose" comment with default Severity and Category — useful for free-form human reviews.

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
    "reviewer_id": "R042",
    "domain":      "AI/ML Systems",
    "persona":     "Methodology Critic",
    "topic_relevance": 0.85,
    "overall_recommendation": "weak_reject",
    "confidence": 3,
    "comments": [
        {
            "severity":          "major",
            "category":          "evaluation",
            "section_reference": "Table 3",
            "summary":           "Missing baseline comparison",
            "description":       "...",
            "suggestion":        "...",
            "keywords":          ["baseline", "prior art", "MLPerf", "comparison"],
        },
        # ...
    ],
}
```

### `review_report.md`

The human-facing aggregate: ranked comment clusters (comments multiple reviewers raised that got grouped), per-domain summary, strengths/weaknesses called out by ≥2 reviewers, the overall recommendation histogram. Rendered from the structured data above — there's no separate format spec because this file is prose.

---

## 5. Producing valid output (for humans writing or converting reviews)

If you're writing a format converter or hand-editing a human review to the AI format, the key things to get right:

1. **Always include `Reviewer ID`, `Domain`, `Persona`, `Overall Recommendation`.** Missing any of these makes the reviewer opaque to the cross-paper aggregation module.
2. **Every comment needs a `Section Reference`.** The validator downgrades comments without anchors to "likely generic/templated" during calibration analysis. If a human review comment is genuinely un-anchored, use `Section Reference: (general)` explicitly rather than leaving the field blank.
3. **`Summary` drives clustering.** Phrase it as a single declarative sentence that another reviewer raising the same issue would plausibly echo.
4. **`Keywords` drive cross-review similarity.** Include the specific technical terms from the paper (method names, table/figure numbers, tool names). Avoid generic words like "improvement" or "unclear".

The validation pipeline's human-review conversion stage produces valid output from free-text reviews; the lenient parser handles variant formatting. You don't need to match the format byte-for-byte — just ensure the required labels appear and each comment has an anchor.
