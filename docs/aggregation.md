# Aggregation

Cross-paper aggregation of per-paper calibration deltas into tuning recommendations for the reviewer database. Lives in `ai_paper_review.aggregation`.

This is a **post-pipeline reporter**, not a pipeline stage:

- The validation pipeline runs on **one paper at a time** and writes a `calibration_delta.json` per run.
- Aggregation runs **any time after accumulation**, across **N completed validation runs**, and produces recommendations that the user applies to the reviewer-database YAML by hand.
- Nothing here mutates any config, any database file, or any validation run.

---

## 1. Why aggregation exists

A single paper's calibration delta is a noisy signal — one human reviewer's idiosyncrasies can make any persona look miscalibrated. Aggregation filters out that noise by only surfacing suggestions that **repeat** across ≥ `min_support` papers. One-offs drop out; patterns rise.

Each recurring suggestion maps to a specific knob in the reviewer-database config YAML:

| Suggestion type               | Edit it applies to                                                                 |
|-------------------------------|------------------------------------------------------------------------------------|
| `strengthen_persona_prompt`   | Add a bullet to the target persona's `priorities` list                             |
| `reduce_persona_noise`        | Tighten the target persona's `common_concerns` field                               |
| `topical_gap`                 | Add a new persona for the uncovered category                                       |
| `selection_policy_adjustment` | Raise `domain_bleed`, or require a persona-floor, so the missed persona gets selected |

---

## 2. Workflow

```
  INPUTS                                        AGGREGATION                 OUTPUT
  ───────────────────────────────               ─────────────────           ──────────────────
  validation run 1  ─▶  calibration_delta_1.json  ─┐                        cross-paper
  validation run 2  ─▶  calibration_delta_2.json  ─┤                        recommendations
  ...                                               ├─▶  group by (type,  ─▶  markdown
  validation run N  ─▶  calibration_delta_N.json  ─┤    target) across
                                                    │    N papers,           (nothing written
  min_support (int, default 2)  ───────────────────┘    filter by             to disk unless
                                                         min_support           --out is passed)
                                                              │
                                                              ▼
                                           human reads the recommendations,
                                           hand-edits the reviewer-database
                                           YAML, rebuilds the .md database,
                                           re-uploads via the Database page
                                                              │
                                                              ▼
                                              next cycle of validation runs
```

Aggregation takes no new inputs beyond what the validation pipeline already wrote to disk — you can run it as many times as you want without cost.

---

## 3. How to use it

### 3.1 From the CLI

```bash
ai-paper-review-aggregate \
    'ai-paper-review-data/runs/validation_*/calibration_delta.json' \
    --min-support 2 \
    --out recommendations.md
```

**Inputs:**
- **Positional:** one or more paths or glob patterns pointing at `calibration_delta.json` files.
- `--min-support N` — filter to suggestions recurring in ≥ N papers (default `2`; use `1` for early-corpus exploration).
- `--out PATH` — write the markdown report to this file. Omit to print to stdout.

**Output:** a markdown recommendations report. The CLI itself writes no other files and does not touch the reviewer database.

### 3.2 From the web UI

Open `http://127.0.0.1:8000/aggregation` (or click **Aggregation** in the top nav bar).

The page globs your workdir's `runs/validation_*/calibration_delta.json` files, aggregates, and renders the recommendations. A `min_support` filter (default `2`) lets you tune how many papers a suggestion must recur in before it's considered robust.

The page is a pure report. It writes nothing to disk and doesn't touch any config.

### 3.3 From Python

For scripted / notebook use:

```python
from ai_paper_review.aggregation.aggregation import (
    load_deltas,
    aggregate,
    recommendation_text,
    render_changelog,
)

deltas = load_deltas([
    "ai-paper-review-data/runs/validation_*/calibration_delta.json",
])

all_suggestions = aggregate(deltas)

# Filter to those seen in >= 2 papers.
robust = [s for s in all_suggestions if s.support >= 2]

for s in robust:
    print(f"[{s.type}] {s.target}  (support={s.support})")
    print(f"  papers: {', '.join(s.paper_ids)}")
    print(f"  →      {recommendation_text(s)}")

# Optional: render a markdown changelog you could archive.
md = render_changelog(
    [{"status": "recommended", "type": s.type, "target": s.target,
      "support": s.support, "paper_ids": s.paper_ids,
      "example_misses": s.example_misses[:3],
      "recommendation": recommendation_text(s)} for s in robust],
    all_suggestions,
    min_support=2,
)
# Path("recommendations.md").write_text(md)   # up to you to persist
```

**Inputs:**
- `load_deltas(paths)` — accepts a list of paths or glob patterns. Parse errors are logged and skipped; missing files don't abort.
- `aggregate(deltas)` — groups by `(type, target)`, returns `List[SuggestionAgg]` sorted by `support` descending.

**Outputs:**
- Each `SuggestionAgg` has: `type`, `target`, `support`, `paper_ids`, `example_misses`, `rationales`, `extra`.
- `recommendation_text(s)` — one-line actionable recommendation per aggregated suggestion.
- `render_changelog(recs, suggestions, min_support)` — full markdown report as a string. You decide whether to write it anywhere.

---

## 4. Design notes

**Reporter, not editor.** Aggregation never writes to any config or database. Rationale:

1. Picking the right words to add to a persona's `priorities:` bullet is a qualitative judgment — automated prompt-editing would produce generic, unhelpful rules.
2. Reviewer-prompt changes affect every future review on every paper; a human "diff + commit" step keeps the blast radius visible.
3. Keeping the module stateless lets you re-run it as often as you like (e.g. after every validation) without risk.

**What happens when N = 1.** The `min_support` filter clamps to `max(1, n_papers)`, so a single-paper workdir still shows all raw suggestions (with support = 1). Useful for sanity-checking the pipeline's output before accumulating a proper corpus.

**Below-threshold suggestions are still shown.** The web page keeps them in a collapsed section under the main recommendations. Near-misses let you lower `min_support` intelligently if you think a pattern is about to emerge.

---

## 5. When the recommendations are useful

- After **5–10 validation runs** on papers from the same venue/subfield — you start seeing repeats.
- **Before a major reviewer-database revision** — use the recommendations as a prioritized edit list.
- **Between release cycles** of your reviewer database, to catch drift between the personas' prompts and what humans actually flag on papers in the field.

Running aggregation on two or three papers will produce either noise (nothing above threshold) or over-confident signal (every suggestion has full support). Give it enough papers to be useful.

---

## 6. See also

- [Validation Pipeline](validation_pipeline.md) — what produces `calibration_delta.json` in the first place.
- [Validation Output Format](validation_output_format.md) — schema of the `calibration_delta.json` files that feed aggregation.
- [Database Format](database_format.md) — the config YAML aggregation's recommendations ask you to edit.
