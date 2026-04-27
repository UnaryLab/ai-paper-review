"""
ai_paper_review.validation
==========================

Compare an AI review against real human reviews and produce calibration
deltas that drive reviewer-database tuning. All stages below are parts
of this same validation pipeline; only the top-level ``validation``
module has a CLI.

Stages (in the order the pipeline runs them):

  - ``conversion``  — reshape raw human-review text into the AI-review
                      markdown schema the rest of the pipeline expects.
                      Auto-run by the web flow when the upload isn't
                      already in that schema; skipped otherwise.
  - ``loading``     — parse human + AI markdown into flat comment lists.
  - ``alignment``   — single-call batch LLM similarity matrix.
  - ``metrics``     — precision / recall / F1 / severity-weighted recall.
  - ``calibration`` — per-paper calibration delta builder.
  - ``reporting``   — markdown validation report formatter.

Supporting modules:

  - ``validation``  — CLI entry point (``ai-paper-review-validate``).
                      Flat — no subcommands.
  - ``constants``   — vocabularies, routing maps, similarity thresholds.
  - ``routing``     — category / sub-rating → persona.

Cross-paper aggregation (rolling many ``calibration_delta.json`` files
into recommendations) is a separate package —
:mod:`ai_paper_review.aggregation` — because it's a post-hoc reporter
that runs *on* validation outputs, not inside the pipeline itself.

Each module is the single source of truth for one concern. Import
explicitly from the relevant submodule (e.g.
``from ai_paper_review.validation.alignment import align_comments``);
this package's ``__init__`` deliberately exposes nothing.
"""
