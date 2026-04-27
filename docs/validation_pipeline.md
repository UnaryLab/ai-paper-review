# Validation Pipeline

The pipeline lives under `ai_paper_review.validation` and is driven by the web worker on `POST /validation`, which runs conversion + alignment + calibration in one click. The web flow accepts raw text (HotCRP / OpenReview / generic) and inserts a human-review conversion stage that reshapes it into the AI-review markdown schema before alignment; inputs already in that schema skip conversion automatically.

---

## 1. Overview

Validation answers three questions for each paper:

1. **Recall.** Of the issues the human reviewer raised, how many did the AI catch?
2. **Precision.** Of the issues the AI raised, how many were genuine (i.e. also raised by the human)?
3. **Which personas are under- / over-firing?** Per-persona hit / miss / false-alarm breakdown drives a `calibration_delta.json` with actionable suggestions for the reviewer database.

Rolling the per-paper deltas up across many papers into reviewer-database tuning recommendations is a separate step — see [Aggregation](aggregation.md).

---

## 2. Pipeline diagram

Workflow-level flow. The human-review conversion stage only runs when the uploaded human review isn't already in AI-review format; markdown files in that format are detected and passed through untouched.

```
  ┌───────────────────────────────────────────────┐   ┌──────────────────────────────┐
  │  human_review.txt / .md                       │   │  review_data.md              │
  │  (raw HotCRP / OpenReview text OR already     │   │  (AI side — from review      │
  │   in AI-review-format markdown)               │   │   pipeline, or re-uploaded)  │
  └──────────────────────┬────────────────────────┘   └──────────────┬───────────────┘
                         │                                           │
                         ▼                                           │
  ┌───────────────────────────────────────────────┐                  │
  │  Stage 1: Human-review conversion             │                  │
  │  (skipped when input is already AI-format)    │                  │
  │   In:  raw human-review text                  │                  │
  │   Out: structured markdown in the AI-review   │                  │
  │        schema                                 │                  │
  │   Side outputs (diagnostic):                  │                  │
  │     • actual_raw_llm.md    (verbatim LLM)     │                  │
  │     • actual_converted.md  (parsed + cleaned) │                  │
  └──────────────────────┬────────────────────────┘                  │
                         │                                           │
                         ▼                                           │
  ┌──────────────────────────────────────────────────────────────────┐
  │  Stage 2: Comment loading                                        │
  │   In:  human-side markdown + AI-side markdown                    │
  │   Out: flat lists of comments on each side, tagged with          │
  │        reviewer_id / persona / domain                            │
  └───────────────────────┬──────────────────────────────────────────┘
                          ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Stage 3: Comment alignment                                      │
  │   In:  human comments (N) × AI comments (M)                      │
  │   Out: N × M similarity scores + per-row verdict per human       │
  │        comment (same / partial / missed; thresholds ≥0.65 /      │
  │        ≥0.35) + a batch-summary dict (llm_comparison) passed     │
  │        through to Stage 6 as the Semantic Comparison section     │
  │   Side outputs (diagnostic):                                     │
  │     • alignment_llm_analysis.md  (raw LLM response + prompt)     │
  │     • alignment_similarities.md  (parsed N × M matrix)           │
  │     • alignment_ranking.md       (human comments ranked by best  │
  │                                   AI match)                      │
  └───────────────────────┬──────────────────────────────────────────┘
                          ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Stage 4: Metric computation                                     │
  │   In:  alignment verdicts + similarity matrix                    │
  │   Out: hits / misses / false alarms, precision, recall, F1,      │
  │        severity-weighted recall                                  │
  └───────────────────────┬──────────────────────────────────────────┘
                          ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Stage 5: Calibration delta                                      │
  │   In:  alignment + AI report + reviewer DB                       │
  │   Out: per-persona hit / miss / false-alarm attribution,         │
  │        plus a list of suggestions (strengthen_persona_prompt /   │
  │        reduce_persona_noise / selection_policy_adjustment /      │
  │        sub_rating_signal / topical_gap)                          │
  └───────────────────────┬──────────────────────────────────────────┘
                          ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Stage 6: Report formatting                                      │
  │   In:  alignment + metrics + calibration delta + llm_comparison  │
  │   Out: markdown report with up to 10 sections:                   │
  │          header + paper metadata                                 │
  │          Semantic Comparison (LLM) — always present; summarises  │
  │            the batch alignment (hits/misses/false-alarm counts)  │
  │          Summary Metrics table                                   │
  │          Hits                                                    │
  │          Misses                                                  │
  │          False Alarms                                            │
  │          Per-Persona Performance table                           │
  │          Sub-Rating Signal Attribution — if low sub-ratings      │
  │            present in the human review                           │
  │          Failure Mode Breakdown                                  │
  │          Calibration Suggestions for the Reviewer Database       │
  └───────────────────────┬──────────────────────────────────────────┘
                          ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │                           OUTPUTS                                │
  │  Written to runs/validation_<UTC_TS>_<3hex>/:                    │
  │                                                                  │
  │  Primary:                                                        │
  │    • validation_report.md     Human-facing — what you read.      │
  │    • calibration_delta.json   Structured — input to aggregation. │
  │                                                                  │
  │  Diagnostic (kept for replay / debugging):                       │
  │    • actual_raw_llm.md        (if conversion ran)                │
  │    • actual_converted.md      (if conversion ran)                │
  │    • alignment_llm_analysis.md                                   │
  │    • alignment_similarities.md                                   │
  │    • alignment_ranking.md                                        │
  │    • _ui_state.json           Web UI result-page snapshot.       │
  └──────────────────────────────────────────────────────────────────┘
```

The `calibration_delta.json` output of each run is the input to the separate [Aggregation](aggregation.md) reporter, which turns many runs' worth of deltas into reviewer-database tuning recommendations.

---

## 3. Stage-by-stage reference

| # | Stage                    | Input                                 | Output                                           | LLM calls |
|---|--------------------------|---------------------------------------|--------------------------------------------------|-----------|
| 1 | Human-review conversion (optional) | raw human-review text       | structured markdown in the AI-review schema      | 1 (raises on parse failure — no retry) |
| 2 | Comment loading          | both sides' markdown                  | flat comment lists tagged with reviewer metadata | 0         |
| 3 | Comment alignment        | human × AI comment grid               | N × M similarity matrix + per-row verdicts (same / partial / missed) + `llm_comparison` summary | **1 batch** |
| 4 | Metric computation       | alignment output                      | precision / recall / F1 / severity-weighted recall | 0       |
| 5 | Calibration delta        | alignment + AI report + reviewer DB   | per-persona stats + miss / sub-rating attributions + suggestions | 0 |
| 6 | Report formatting        | alignment + metrics + calibration + llm_comparison | validation_report.md (up to 10 sections) | 0 |

Only Stages 1 and 3 hit the network. Stage 3's single-batch design is load-bearing: naively doing N × M per-pair calls would cost O(N·M) calls per paper and make validation latency-dominant. One structured call returns the whole matrix.
