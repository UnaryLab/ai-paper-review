# Validation Output Format

This document describes the files a validation run produces, the alignment it performs between AI and human reviews, and the schema of the two authoritative outputs:

- `validation_report.md` — the human-facing report (metrics, hits, misses, false alarms, calibration suggestions).
- `calibration_delta.json` — the structured output consumed by the cross-paper [Aggregation](aggregation.md) module when rolling up feedback across many papers.

A validation run always writes to a single directory named `validation_<timestamp>_<hex>` inside the workdir's `runs/` folder. Every artifact below lives in that directory side-by-side: no subfolders, no database mutation. If a run errors mid-way, partial files may be present; `_ui_state.json` is still written, with `"status": "error"` and the error message.

---

## 1. What a validation run produces

A completed run writes eight files to its run directory:

| File                           | Purpose                                                                                   |
|--------------------------------|-------------------------------------------------------------------------------------------|
| `validation_report.md`         | Final human-readable report — metrics, hits/misses/false alarms, calibration suggestions. |
| `calibration_delta.json`       | Structured version of the same information; input to the cross-paper aggregation module.  |
| `actual_converted.md`          | Human review after LLM conversion to AI-review format. Skipped if the input was already in AI format. |
| `actual_raw_llm.md`            | Verbatim LLM output from the human-review conversion step (diagnostic; pre-parse). |
| `alignment_llm_analysis.md`    | All chunk LLM responses + prompts for the alignment step, each labelled with its chunk range. |
| `alignment_similarities.md`    | N × M similarity matrix (human × AI comments) parsed from the LLM response, with per-row verdict. |
| `alignment_ranking.md`         | Human comments sorted by their best-match AI similarity, highest first.                   |
| `_ui_state.json`               | Internal state snapshot for re-rendering the result page in the web UI.                   |

Plus the source files the user uploaded, saved as `human__<name>` (the human review `.txt`/`.md`) and, for uploaded AI reviews, `ai__<name>` (the AI review `.md`). The web UI's result page shows the absolute path for every file in this directory.

---

## 2. Alignment — parallel chunked LLM calls

Validation compares a human review (N comments) against an AI review (M comments) by asking the LLM for an **N × M similarity matrix**. The N human comments are split into chunks of 5; each chunk is sent as a separate LLM call paired with all M AI comments, and the results are assembled into the full matrix. All chunks run in parallel, so wall-clock time is roughly that of a single call.

**Inputs to the alignment call:**

- Human comments: each labeled with its real comment ID (e.g. `Reviewer_qFvT-C1`) — the `id` field from the flattened comment list. When no ID is available the fallback `H{n}` is used.
- AI comments: each labeled with its real comment ID (e.g. `R042-C3`) from the AI review file.
- Parallel chunked prompts covering every `(human_id, ai_id)` pair (human comments split into groups of ≤5, each group paired with all AI comments), with instructions to define three verdicts per row based on the best similarity score:
  - **same** — best-match similarity ≥ 0.65
  - **partial** — best-match similarity ≥ 0.35
  - **missed** — best-match similarity < 0.35

**Output of the alignment call** (expected format; the parser tolerates wide drift):

```
## Similarity scores

<human_id> | <ai_id> | <score>
<human_id> | <ai_id> | <score>
...

## Ranked human comments

1. <human_id> — best_match=<ai_id> sim=<score>
2. <human_id> — best_match=<ai_id> sim=<score>
...
```

One pipe-separated line per `(human, AI)` pair — `N × M` lines total. The parser converts these into a float matrix; verdicts are assigned per row from each human comment's best-matching AI column.

The parser has a four-tier resolver chain that accepts:
1. Exact AI-id match (`A3`, `R042-C3`)
2. Suffix match — `-C8` resolves to a unique AI id ending in `-C8`
3. Positional `C<n>` / `-C<n>` — treated as the n-th AI comment
4. Positional `H<n>` / `A<n>` in surplus — treated as the n-th row/column

If pass 1 (exact-only) parses 0 rows successfully, pass 2 enables the fuzzy fallbacks and logs a warning. A chunk that still parses 0 rows fails the run. A chunk that parses fewer rows than it has pairs is marked `⚠ INCOMPLETE` in its `alignment_llm_analysis.md` section header, and its missing pairs score 0.

### Why chunked, not pairwise

Three practical reasons:

1. **Cost scales as O(N+M) tokens per chunk, not O(N·M) calls.** A 15-comment human review vs a 40-comment AI review would take 600 calls if each pair were scored separately.
2. **The LLM sees every AI comment when scoring each chunk.** It can reason "A7 is the best match" with full context rather than scoring in isolation.
3. **Chunks keep output-token budgets small.** A single call for a large human review can saturate per-request limits on subscription-tier providers; splitting to ≤5-human chunks avoids that. Parallel execution means wall-clock time stays comparable to a single large call.

`alignment_llm_analysis.md` captures all chunk responses and prompts in one file, with `## Chunk N / total (human rows X–Y)` section headers so each chunk is traceable.

---

## 3. `validation_report.md` — structure

A single markdown file, rendered top-to-bottom for humans. Section order:

```markdown
<!-- provenance -->
**LLM:** `<provider>` / `<model>`
**Base URL:** `<base_url or (default)>`
**Launched:** <timestamp>
**Ended:** <timestamp> (duration: <duration>)

---

# Review Validation Report

**Title:** <title or paper_id>
**Venue:** <venue or 'n/a'>

## Semantic Comparison (LLM)
<present when both sides have comments. Italic intro and the model used,
 then one **Summary:** line: a count summary of the LLM similarity matrix
 (N hits, M misses, F false alarms, number of pairs parsed).>

## Summary Metrics
| Metric                   | Value   |
|--------------------------|---------|
| Actual human comments    |  N      |
| AI comments              |  M      |
| Hits (actual ↔ AI match) |  K      |
| Misses (no AI match)     |  N-K    |
| False alarms (no actual) |  F      |
| Recall                   | K/N     |
| Precision                | (M-F)/M |
| F1                       | ...     |
| Severity-weighted recall | ...     |

## Hits — Actual Comments the AI Caught
<every aligned (human, AI) pair with similarity score and both summaries;
 supporting AI reviewers (other AI comments above partial threshold) listed>

## Misses — Actual Comments the AI Failed to Raise
<every human comment with best AI similarity below partial threshold;
 expected AI persona (from DB's category_to_persona map) annotated>

## False Alarms — AI Comments Not Raised by Any Human
<AI comments that matched no human comment above partial — capped at 20,
 with a note on how many more were omitted>

## Per-Persona Performance (Selected Reviewers)
| Reviewer ID | Persona | Comments emitted | Actual issues helped catch | False alarms | Noise ratio |
<one row per selected AI reviewer; noise ratio = false_alarms / comments_emitted>

## Sub-Rating Signal Attribution   ← conditional; only when the human review
                                     contains low sub-ratings (e.g. OpenReview
                                     Soundness / Presentation / Contribution)
| Reviewer | Sub-rating | Value | Expected persona | Persona selected? | Caught anything? | Verdict |

## Failure Mode Breakdown
- Selection failures (right persona not selected): N
- Prompt failures (persona selected but missed issue): N
- Sub-rating signals (low human sub-rating not caught by expected persona): N
- Uncovered categories: (list of category → miss count pairs)

## Calibration Suggestions for the Reviewer Database
<one ### #N [type] block per suggestion; type is one of:
 strengthen_persona_prompt / selection_policy_adjustment /
 reduce_persona_noise / sub_rating_signal / topical_gap>
```

---

## 4. `calibration_delta.json` — schema

The structured form of the report, designed for programmatic cross-paper aggregation. Top-level keys:

```json
{
  "paper_id":                "a-stable-id-or-title-slug",
  "metrics":                 { "...see metrics shape below..." },
  "summary":                 { "...see summary shape below..." },
  "persona_stats":           [ {"reviewer_id": "R042", "persona": "...", "comments_emitted": 7, ...}, ... ],
  "miss_attributions":       [ {"actual_comment": "...", "expected_persona": "...", "failure_mode": "...", ...}, ... ],
  "sub_rating_attributions": [ ... ],
  "suggestions":             [ {"type": "strengthen_persona_prompt", "target_persona": "...", ...}, ... ],
  "llm_comparison":          { "summary": "...", "matches": [], "missed": [], "extras": [], "llm_model": "..." }
}
```

`llm_comparison` is present when both the human side and the AI side have comments; otherwise it is `null` in the web output and absent from the CLI output. `matches`, `missed`, and `extras` are always empty lists; the useful content is in `summary`, which holds a one-line text description of the alignment results (hit/miss/false-alarm counts, number of chunks, and the number of similarity pairs parsed).

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

Aggregate counts of the calibration findings for this paper:

```json
{
  "selection_failures":   3,
  "prompt_failures":      2,
  "uncovered_categories": {"evaluation": 2, "reproducibility": 1},
  "sub_rating_signals":   1
}
```

- `selection_failures` — misses where the expected persona was not among the selected reviewers.
- `prompt_failures` — misses where the expected persona was selected but still missed the issue.
- `uncovered_categories` — dict of `{category: miss_count}` for categories that appear in misses.
- `sub_rating_signals` — count of low human sub-ratings whose expected AI persona either wasn't selected or didn't catch the corresponding weakness.

### `persona_stats` shape

One entry per selected AI reviewer:

```json
{
  "reviewer_id":                  "R042",
  "persona":                      "Methodology Critic",
  "comments_emitted":             7,
  "actual_comments_helped_catch": 3,
  "false_alarms":                 2,
  "noise_ratio":                  0.286
}
```

`noise_ratio` = `false_alarms / comments_emitted`; `null` when `comments_emitted` is 0.

### `miss_attributions` shape

One entry per missed human comment:

```json
{
  "actual_comment":           "The paper does not compare against ...",
  "severity":                 "major",
  "category":                 "evaluation",
  "expected_persona":         "Methodology Critic",
  "expected_persona_selected": true,
  "failure_mode":             "prompt_failure",
  "note":                     "Persona 'Methodology Critic' WAS selected but still missed ..."
}
```

`failure_mode` is one of `"selection_failure"`, `"prompt_failure"`, or `"uncategorized"` (when the category has no persona mapping in the reviewer DB). `expected_persona` is `null` for uncategorized misses.

### `suggestions` shape

Each suggestion has a `type` field plus type-specific fields:

| `type` | Key fields |
|--------|-----------|
| `strengthen_persona_prompt` | `target_persona`, `scope`, `rationale`, `example_misses` (list of comment snippets), `prompt_patch_hint` |
| `selection_policy_adjustment` | `missing_personas_in_selection` (list), `rationale`, `fix_hint` |
| `reduce_persona_noise` | `target_persona`, `noise_ratio`, `comments_emitted`, `rationale`, `prompt_patch_hint` |
| `sub_rating_signal` | `target_persona`, `sub_rating`, `support` (count), `reviewers` (list), `failure_modes` (list), `rationale`, `fix_hint` |
| `topical_gap` | `category`, `miss_count`, `expected_persona`, `rationale`, `fix_hint` |

The cross-paper aggregation module (`ai_paper_review.aggregation`) groups suggestions by `(type, target)` across N papers, where `target` is `target_persona`, `category`, or the sorted `missing_personas_in_selection` list depending on the type. Support counts distinct papers; a group becomes a recommendation when its support is ≥ `min_support` (default 2), and the report's Skipped section lists up to 20 of the groups below that. Groups past the first 20 are not printed; the **Skipped / no-op** count in the Summary covers all of them. See [Aggregation](aggregation.md) for the full usage doc.

---

## 5. Re-reading a validation run

The validation run directory is self-contained and re-readable without re-running the pipeline:

- `validation_report.md` is human-readable directly.
- `calibration_delta.json` is standard JSON; parse with any tool.
- `_ui_state.json` is **internal** — the web UI uses it to re-render the result page without re-computing anything. Don't depend on its shape; it's an implementation detail and may change without notice.
- `alignment_similarities.md` is the most useful diagnostic file: it shows you exactly which (human, AI) pair got which similarity score. Parse quality is reported in `alignment_llm_analysis.md`: its `**Parsed:**` header line gives the parsed / total count with `✓`, `⚠ partial`, or `⚠ degraded`, and incomplete chunks are marked `⚠ INCOMPLETE`.

The web UI's Recent validations list and result page read the run directory directly: deleting a validation means removing the run directory, and the row disappears from the UI on the next refresh. A validation that is still running cannot be deleted.

---

## 6. When validation retries or degrades

A few conditions cause partial output:

- **LLM returns unparseable alignment matrix.** The parser runs a second pass with fuzzy resolvers; if a chunk still yields 0 rows, the run fails. If some rows parse, missing pairs score 0, the chunk is marked `⚠ INCOMPLETE`, and the `**Parsed:**` line at the top of `alignment_llm_analysis.md` shows `⚠ partial` or `⚠ degraded`.
- **Human-review conversion LLM returns empty or 0-comment output.** The conversion module raises immediately — there is no retry loop. The web worker surfaces the error to the user; `actual_raw_llm.md` is written before the parse attempt so the verbatim model response is available for diagnosis.
- **Validation stage LLM errors (quota, rate limit).** The run's status flips to `error` and the run directory is left partial. A minimal `_ui_state.json` with `"status": "error"` and the error message is written, so the run shows as an error row in Recent validations (also after a server restart) and can be deleted from there.

These are best diagnosed by reading `alignment_llm_analysis.md` and `actual_raw_llm.md` — the verbatim pre-parse LLM outputs. If the LLM response looks syntactically fine but the parser rejected it, the parser needs updating; if the response is empty or truncated, the provider/model is the culprit.
