# Review Pipeline

The pipeline lives under `ai_paper_review.review` and is driven by the web worker on `POST /review`.

---

## 1. Overview

A matrix of domain-specialist × reviewing-persona system prompts (200 reviewers in the default Computer Architecture database: 10 domains × 20 personas). The selector picks the top-N most relevant reviewers by keyword similarity (N chosen per run — default 10, recommended range 5–10 to balance coverage against LLM cost and wall-time, hard range 1–20), each produces 5–10 structured review comments in parallel, and results are clustered and ranked by commonality × severity.

An always-on **Writing Clarity Reviewer** (`G001`) runs on every paper in addition to the top-N selection. It runs before the parallel persona reviewers so that, on providers whose client caches the (system + PDF) prefix (`anthropic_api`, `openai_api`, `google_api`), its call warms that cache and the persona calls reuse it; `claude_sdk` and the text-only providers get no such cache. It targets writing quality and internal consistency only (flow, terminology, grammar, figures, structure, agreement between the text and its algorithms, figures, tables, and equations, and the numbers inside tables and figures), produces 5–20 comments, and emits its output to a separate file. It is not part of the top-N selection, not fed into clustering, and not compared against human reviewers during validation. A failed LLM call re-runs the whole clarity reviewer up to 2 more times after a 5 s pause; if every attempt fails, or on a fatal error (auth, missing CLI, hard rate limit) or a refused or blocked reply, the run stops with an error. Output that stays empty or unparseable after the empty-output retries also stops the run with an error.

> **Why 5–10 reviewers?** Below 5 you lose the cross-reviewer consensus signal that the clustering step relies on — a single idiosyncratic concern carries the same weight as a genuine shared one. Above 10 you hit diminishing returns: the bundled personas cap how many distinct review lenses make sense, and each extra reviewer is another parallel LLM call that adds cost and latency without proportional benefit. 10 is the default because it covers the core persona dimensions (novelty, methodology, reproducibility, presentation, soundness, plus cross-disciplinary / adversarial / correctness spot-checks) with enough headroom for the clustering step to surface genuinely shared concerns; step down to 5–7 if you're cost-sensitive or on a slow free tier.

---

## 2. Pipeline diagram

Workflow-level flow. Each stage reads its input, produces its output, and hands off to the next.

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │                             INPUTS                                  │
   │   • paper.pdf              (uploaded via the web UI)                │
   │   • <reviewer_db>.md       (bundled default OR user upload)         │
   │   • N                      (integer 1–20, default 10, recommended 5–10) │
   │   • provider / model       (from config.yaml, overridable per run)  │
   └──────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 1: PDF ingestion                                            │
  │   In:  paper.pdf                                                   │
  │   Out: paper text + title / abstract (one LLM call; under 500      │
  │        extracted characters fails the run)                         │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 2: Database loading                                         │
  │   In:  <reviewer_db>.md                                            │
  │   Out: list of reviewer records (domain, persona, keywords,        │
  │        system prompt, …)                                           │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 3: Reviewer selection                                       │
  │   In:  paper title / abstract / text × reviewer keywords           │
  │   Out: top-N reviewers, persona-diversified, with similarity       │
  │        scores against the paper                                    │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 4: Writing-clarity reviewer (always on; one LLM call)       │
  │   In:  paper text                                                  │
  │   Out: one review by G001 / Writing Clarity Reviewer — same        │
  │        comment schema; writes to writing_clarity_review.md.        │
  │        Runs first to warm the (system + PDF) prompt cache for the  │
  │        persona calls (anthropic_api, openai_api, google_api).      │
  │        NOT merged into raw_reviews / all_comments, so not          │
  │        clustered and not seen by Validation.                       │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 5: Reviewer dispatching   ← parallel LLM calls, N in flight │
  │   In:  paper text + each selected reviewer's system prompt         │
  │   Out: N reviews × 5–10 structured comments each (Severity,        │
  │        Category, Section Reference, Summary, Description,          │
  │        Keywords)                                                   │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 6: Comment clustering                                       │
  │   In:  all comments flattened across reviewers                     │
  │   Out: clusters of semantically similar comments (each comment is  │
  │        compared with the first comment of each cluster, in         │
  │        selection order)                                            │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 7: Cluster ranking                                          │
  │   In:  clusters with severity metadata                             │
  │   Out: clusters ordered by commonality × severity                  │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  Stage 8: Report formatting                                        │
  │   In:  ranked clusters                                             │
  │   Out: human-readable markdown report                              │
  └────────────────────────────────┬───────────────────────────────────┘
                                   ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │                             OUTPUTS                                 │
   │   Written to runs/review_<UTC_TS>_<3hex>/ by the web worker.        │
   │                                                                     │
   │   • review_report.md            Ranked issues, human-facing.        │
   │   • review_data.md              Per-reviewer structured comments —  │
   │                                 canonical input to validation.     │
   │   • writing_clarity_review.md   Always-on clarity reviewer's        │
   │                                 output; never validated.           │
   │   • selection_similarities.md   Full reviewer-vs-paper similarity   │
   │                                 landscape; top-N are marked.       │
   │   • clustering_similarities.md  Pairwise comment similarities + the │
   │                                 clustering decisions that followed. │
   │   • _ui_state.json              Web UI result-page snapshot.        │
   │   • <paper>.pdf                 Original upload (preserved).        │
   └─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Stage-by-stage reference

| # | Stage                   | Input                                | Output                                       | LLM calls |
|---|-------------------------|--------------------------------------|----------------------------------------------|-----------|
| 1 | PDF ingestion           | paper.pdf                            | paper text + title / abstract                | **1**     |
| 2 | Database loading        | \<reviewer_db\>.md                   | list of reviewer records                     | 0         |
| 3 | Reviewer selection      | paper text × reviewer keywords       | top-N reviewers with similarity scores       | 0         |
| 4 | Writing-clarity reviewer (always on) | paper text                | 1 review by G001 (5–20 comments) → `writing_clarity_review.md` | **1** (up to 36 with repairs and re-runs) |
| 5 | Reviewer dispatching    | paper text + each reviewer's prompt  | N reviews × 5–10 comments each               | **N (parallel)** |
| 6 | Comment clustering      | all comments across reviewers        | clusters of similar comments                 | 0         |
| 7 | Cluster ranking         | clusters with severity               | ranked issue list                            | 0         |
| 8 | Report formatting       | ranked clusters                      | human-readable markdown report               | 0         |

Stages 1, 4, and 5 hit the network (Stage 1 makes one call to extract the title and abstract, falling back to a heuristic if the call fails; Stage 4 adds one extra LLM call per paper, or up to 36 when markdown repairs and re-runs are needed, and, on `anthropic_api`, `openai_api`, and `google_api`, warms the prompt cache for Stage 5). Stages 3 and 6 embed text locally (sentence-transformers if available, TF-IDF fallback). Everything else is pure string handling. If no reviewer in Stage 5 returns a usable comment, the run fails instead of writing an empty report.
