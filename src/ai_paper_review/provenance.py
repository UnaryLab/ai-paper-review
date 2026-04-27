"""Provenance header for pipeline markdown outputs.

Every ``.md`` file the review / validation / aggregation pipelines
write starts with a short block identifying

* **LLM provider / model / base URL** — lets a reader tell which model
  produced the content when comparing runs across providers.
* **Launch time + end time + duration** — makes it possible to line up
  a report with whatever external logs (rate-limit dashboards, API
  billing) cover the same window.

Aggregation is a reporter stage — no LLM is called — so its provenance
block has ``provider=None`` and renders "not applicable (reporter)".
The timestamps still apply.

Callers build the block at write-time via :func:`format_provenance`;
:func:`now_iso` is a small helper for capturing UTC timestamps in a
format consistent with the ``created_at`` fields in the web job
registries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def now_iso() -> str:
    """Return the current UTC time as ISO8601 with seconds precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def humanize_duration(seconds: float) -> str:
    """Render a seconds count as a short human-readable duration.

    Shared with the validation reporter's body so the duration string
    in the prepended block matches the duration string in the report
    body exactly.
    """
    if seconds < 1.0:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {int(sec)}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {int(minutes)}m"


def format_provenance(
    *,
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    launched_at: str,
    ended_at: Optional[str] = None,
    format_fix_retries: Optional[int] = None,
    n_reviewers_total: Optional[int] = None,
) -> str:
    """Return a markdown header block to prepend to a pipeline output file.

    ``provider=None`` means the stage isn't LLM-backed (e.g. aggregation);
    the LLM line renders "not applicable (reporter stage)" but the
    timestamps still show. ``base_url=None`` renders "(default)" so
    readers can tell a managed-API run apart from an Ollama / Azure /
    OpenAI-compatible run.

    ``ended_at`` defaults to :func:`now_iso` so writers can simply pass
    the launch timestamp they captured at the start of the run.

    ``format_fix_retries`` + ``n_reviewers_total`` are optional and only
    apply to the review stage: when passed, a trailing
    ``**Format-fix retries:** X of N reviewer(s)`` line is appended so
    the operational signal (how many reviewers needed a markdown-repair
    pass) lives with the rest of the run metadata rather than inline in
    the report body. Validation / aggregation pass neither and the line
    is omitted.
    """
    if ended_at is None:
        ended_at = now_iso()

    try:
        t0 = datetime.fromisoformat(launched_at.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        duration = humanize_duration((t1 - t0).total_seconds())
    except (ValueError, TypeError):
        duration = "?"

    if provider:
        llm_line = f"**LLM:** `{provider}` / `{model}`"
    else:
        llm_line = "**LLM:** not applicable (reporter stage)"

    base_url_line = f"**Base URL:** `{base_url or '(default)'}`"

    # Build labelled lines in order; add two-space trailing markdown line
    # breaks to all but the last so they render as a stacked metadata
    # block rather than one wrapped paragraph.
    labeled = [
        llm_line,
        base_url_line,
        f"**Launched:** {launched_at}",
        f"**Ended:** {ended_at} (duration: {duration})",
    ]
    if format_fix_retries is not None and n_reviewers_total is not None:
        labeled.append(
            f"**Format-fix retries:** {format_fix_retries} of "
            f"{n_reviewers_total} reviewer(s)"
        )

    with_breaks = [
        line + ("  " if i < len(labeled) - 1 else "")
        for i, line in enumerate(labeled)
    ]
    return (
        "<!-- provenance -->\n"
        + "\n".join(with_breaks)
        + "\n\n---\n\n"
    )
