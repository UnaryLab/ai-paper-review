"""
ai_paper_review.review
======================

End-to-end paper review pipeline:

  - ``review``         — ``ReviewState`` + LangGraph wiring + CLI
                         (``ai-paper-review-review``). Entry point.
  - ``reviewer_db``    — parse the 200-persona reviewer database.
  - ``pdf_ingestion``  — extract paper text + title/abstract from PDF.
  - ``selection``      — Embedder + top-N persona-diversified picker.
  - ``reviewer_dispatching`` — parallel LLM dispatch + retries + parse.
  - ``parsing``        — markdown ↔ dict round-trippers.
  - ``clustering``     — group cross-reviewer comments by similarity.
  - ``ranking``        — score by commonality × severity, format report.
  - ``constants``      — N range, severity weights, retry caps.

Each module is the single source of truth for one concern. Import
explicitly from the relevant submodule (e.g.
``from ai_paper_review.review.reviewer_db import Reviewer``); this
package's ``__init__`` deliberately exposes nothing — there is no
backward-compat re-export layer.

INTENDED USE
------------
This pipeline is a **draft-polishing aid for papers you are writing**. It
is designed to help authors spot early-stage weaknesses in their own
in-progress work before they submit. It is NOT a peer-review generator —
see top-level package docstring for details.
"""
