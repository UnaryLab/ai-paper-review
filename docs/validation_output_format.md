# Validation Output Format

This document describes the files a validation run produces, the alignment it performs between AI and human reviews, and the schema of the two authoritative outputs:

- `validation_report.md` — the human-facing report (metrics, hits, misses, false alarms, calibration suggestions).
- `calibration_delta.json` — the structured output consumed by the cross-paper [Aggregation](aggregation.md) module when rolling up feedback across many papers.

A validation run always writes to a single directory named `validation_<timestamp>_<hex>` inside the workdir's `runs/` folder. Every artifact below lives in that directory side-by-side — no subfolders, no database mutation. If a run errors mid-way, partial files may be present; only `_ui_state.json` signals completion.

---

## 1. What a validation run produces

A completed run writes eight files to its run directory:

| File                           | Purpose                                                                                   |
|--------------------------------|-------------------------------------------------------------------------------------------|
| `validation_report.md`         | Final human-readable report — metrics, hits/misses/false alarms, calibration suggestions. |
| `calibration_delta.json`       | Structured version of the same information; input to the cross-paper aggregation module.  |
| `actual_converted.md`          | Human review after LLM conversion to AI-review format. Skipped if the input was already in AI format. |
| `actual_raw_llm.md`            | Verbatim LLM output from the human-review conversion step (diagnostic; pre-parse). |
| `alignment_llm_analysis.md`    | Raw LLM response + prompt for the single batch-similarity call.                           |
| `alignment_similarities.md`    | N × M similarity matrix (human × AI comments) parsed from the LLM response, with per-row verdict. |
| `alignment_ranking.md`         | Human comments sorted by their best-match AI similarity, highest first.                   |
| `_ui_state.json`               | Internal state snapshot for re-rendering the result page in the web UI.                   |

Plus whichever source files the user uploaded (the human review `.txt`/`.md`, and — for upload-case AI comparisons — the AI review `.md`). The web UI's result page shows the absolute path for every file in this directory.

---

## 2. Alignment — one batch LLM call

Validation compares a human review (N comments) against an AI review (M comments) by asking a single LLM call for an **N × M similarity matrix**. This replaces what used to be pairwise LLM calls (one per cell) and makes validation cheap enough to run on every completed review.

**Inputs to the alignment call:**

- Human comments: each labeled `H1`, `H2`, … with their `summary` + `description` + `section_reference`.
- AI comments: each labeled `A1`, `A2`, … (or the reviewer-prefixed `R042-C3` form) with the same three fields.
- Instructions that define three verdicts per row:
  - **same** — best-match similarity ≥ 0.65
  - **partial** — best-match similarity ≥ 0.35
  - **missed** — best-match similarity < 0.35

**Output of the alignment call** (expected format; the parser tolerates wide drift):

```markdown
| H | A1  | A2  | A3  | ... | AM  | verdict | best_match |
|---|-----|-----|-----|-----|-----|---------|------------|
| H1 | 0.82 | 0.31 | 0.15 | ... | 0.08 | same    | A1         |
| H2 | 0.12 | 0.18 | 0.09 | ... | 0.04 | missed  | —          |
| H3 | 0.48 | 0.21 | 0.52 | ... | 0.14 | partial | A3         |
...
```

The parser has a four-tier resolver chain that accepts:
1. Exact AI-id match (`A3`, `R042-C3`)
2. Suffix match — `-C8` resolves to a unique AI id ending in `-C8`
3. Positional `C<n>` / `-C<n>` — treated as the n-th AI comment
4. Positional `H<n>` / `A<n>` in surplus — treated as the n-th row/column

If pass 1 (exact-only) parses 0 rows successfully, pass 2 enables the fuzzy fallbacks and logs a warning. Unparseable rows become `missed` with similarity 0.

### Why it's a single call

Four practical reasons:

1. **Cost scales as O(N+M) tokens, not O(N·M) calls.** A 15-comment human review vs a 40-comment AI review used to mean 600 pairwise calls.
2. **The LLM sees every AI comment when scoring each row.** It can actually say "A7 is the best match" rather than pairwise-score without context.
3. **Validation finishes in ~30–90 s** for typical review sizes instead of minutes.
4. **One file (`alignment_llm_analysis.md`) captures the full diagnostic record** — prompt + response — instead of hundreds of fragmented traces.

---

## 3. `validation_report.md` — structure

A single markdown file, rendered top-to-bottom for humans. Section order:

```markdown
# Review Validation Report

**Paper:** <title or paper_id>
**Venue:** <venue or 'n/a'>

## Semantic Comparison (LLM)          ← optional; shown only if the run
                                       produced an llm_comparison object
<natural-language summary + per-comment verdicts with ✅ / 🟡 / ⚠️ / ❌
 icons, then the LLM's view of what was missed and what was extra>

## Summary Metrics
| Metric                   | Value |
|--------------------------|-------|
| Actual human comments    |  N    |
| AI comments              |  M    |
| Hits (actual ↔ AI match) |  K    |
| Misses (no AI match)     |  N-K  |
| False alarms (no actual) |  F    |
| Recall                   | K/N   |
| Precision                | (M-F)/M |
| F1                       | ...   |
| Severity-weighted recall | ...   |

## Per-persona performance
<per-persona hit/miss counts, precision and recall per persona, median
 topic-relevance score — shows which reviewers pulled their weight on
 this paper and which missed the point>

## Sub-rating attributions        ← optional; present when the human review
                                    supplied `## Sub-Ratings` section

## Hits
<every aligned (human, AI) pair with similarity score and both summaries>

## Misses
<every human comment that had no AI match above the `partial` threshold,
 with the best-match AI comment and its similarity for context>

## False alarms
<AI comments that matched no human comment above `partial` — i.e.
 hallucinations or off-target critiques>

## Calibration suggestions
<grouped by failure mode: under-coverage (missed categories), over-firing
 (false-alarm-heavy personas), persona-floor gaps (major-severity misses
 attributable to a persona that wasn't selected), etc.>
```

All section order here matches the web UI's result-page rendering so the two stay aligned.

---

## 4. `calibration_delta.json` — schema

The structured form of the report, designed for programmatic cross-paper aggregation. Top-level keys:

```json
{
  "paper_id":           "a-stable-id-or-title-slug",
  "metrics":            { "...see below..." },
  "summary":            { "...see below..." },
  "persona_stats":      [ {"persona": "...", "recall": 0.42, ...}, ... ],
  "miss_attributions":  [ {"actual_id": "H3", "attributed_persona": "...", ...}, ... ],
  "sub_rating_attributions": [ ... ],
  "suggestions":        [ {"kind": "...", "persona": "...", "...": "..."}, ... ],
  "llm_comparison":     { "summary": "...", "matches": [...], "missed": [...], "extras": [...], "llm_model": "..." }
}
```

`llm_comparison` is `null` when the batch-similarity call didn't produce a narrative comparison (e.g. on embedding-only runs); otherwise it carries the same three icon-coded lists that appear in the report's "Semantic Comparison" section.

### `metrics` shape

Emitted verbatim by the metric-computation stage:

```json
{
  "n_actual": 15,
  "n_ai": 42,
  "n_hits": 9,
  "n_misses": 6,
  "n_false_alarms": 11,
  "recall": 0.6,
  "precision": 0.738,
  "f1": 0.662,
  "severity_weighted_recall": 0.542
}
```

All numerical fields are rounded to 3 decimal places. Counts are integers.

### `summary` shape

Aggregate rollup of the calibration findings. Fields include `n_suggestions`, `top_failure_mode`, and `strongest_persona` — the exact keys depend on the calibration stage's current version; consumers should treat `summary` as a dict of loosely-typed metadata and fall back on `persona_stats` + `suggestions` for the authoritative data.

### `suggestions` shape

Each suggestion is one of several kinds. All share:

```json
{
  "kind":    "strengthen" | "quiet" | "add_keyword" | "add_persona_floor" | ...,
  "persona": "Methodology Critic",
  "rationale": "Human reviewer flagged a missing baseline comparison ...",
  "...":     "kind-specific fields"
}
```

Kind-specific fields:

- `strengthen` — `category`, `example_misses` (list of `actual_id`): this persona repeatedly missed issues in this category; strengthen the prompt for the category.
- `quiet` — `category`, `example_false_alarms`: this persona is firing false alarms in this category; tone down the prompt's emphasis.
- `add_keyword` — `keywords` (list): the selector missed this persona on this paper despite relevance — add these keywords to the domain.
- `add_persona_floor` — `reason`: a major miss was attributable to a persona that wasn't selected; consider requiring this persona in future runs on similar papers.

The cross-paper aggregation module (`ai_paper_review.aggregation`) groups these by `(persona, kind, category)` across N papers and only emits a recommendation when the same grouping shows up in ≥ `min_support` papers (default 2). See [Aggregation](aggregation.md) for the full usage doc.

---

## 5. Re-reading a validation run

The validation run directory is self-contained and re-readable without re-running the pipeline:

- `validation_report.md` is human-readable directly.
- `calibration_delta.json` is standard JSON; parse with any tool.
- `_ui_state.json` is **internal** — the web UI uses it to re-render the result page without re-computing anything. Don't depend on its shape; it's an implementation detail and may change without notice.
- `alignment_similarities.md` is the most useful diagnostic file: it shows you exactly which (human, AI) pair got which similarity score, and lets you spot parse failures (rows marked `PARSE FAILED` or with suspicious all-zeros).

The web UI's Recent validations list and result page read the run directory directly — deleting a validation means removing the run directory, and the row disappears from the UI on the next refresh.

---

## 6. When validation retries or degrades

A few conditions cause partial output:

- **LLM returns unparseable alignment matrix.** The parser runs a second pass with fuzzy resolvers; if that also yields 0 rows, all human comments default to `missed`/similarity 0, and the report is written with a degraded-parse indicator at the top of `alignment_similarities.md` (`⚠ degraded` or `✗ PARSE FAILED`).
- **Human-review conversion LLM returns empty or 0-comment output.** The worker retries up to 5 times (same retry policy as the review stage), then gives up and writes `actual_converted.md` with whatever it got. Downstream metrics will be skewed but not crash.
- **Validation stage LLM errors (quota, rate limit).** The run's status flips to `errored` and the run directory is left partial; the `_ui_state.json` is not written, so it doesn't appear in the Recent validations result-view list (it will still show as an errored row if the in-memory job registry knows about it from this server session).

These are best diagnosed by reading `alignment_llm_analysis.md` and `actual_raw_llm.md` — the verbatim pre-parse LLM outputs. If the LLM response looks syntactically fine but the parser rejected it, the parser needs updating; if the response is empty or truncated, the provider/model is the culprit.
