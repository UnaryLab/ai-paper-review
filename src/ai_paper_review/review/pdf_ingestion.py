"""PDF ingestion + heuristic title/abstract extraction.

Two extraction backends are available:

* ``extract_pdf_text`` — pypdf, plain text. Used for every provider
  except ``copilot_sdk``.
* ``extract_pdf_markdown`` — Microsoft MarkItDown, markdown with table
  structure preserved. Required by ``copilot_sdk`` (Copilot CLI does
  not accept binary PDFs as input).

``extract_pdf_for_provider`` routes between them and falls back to
pypdf with a loud warning if MarkItDown is requested but unavailable.

Neither backend reads images, figures, or charts. The system reasons
over text and tables only — see README disclaimers for context.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict

logger = logging.getLogger("review_system")


def extract_pdf_text(pdf_path: str | Path) -> str:
    """Extract plain text from a PDF using pypdf.

    Default path for every provider except ``copilot_sdk``. Tables lose
    their shape — see ``extract_pdf_markdown`` if structure matters.
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ImportError("pypdf is required: pip install pypdf") from e

    reader = PdfReader(str(pdf_path))
    chunks = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception as e:  # pragma: no cover
            logger.warning("Failed to extract page: %s", e)
    return "\n".join(chunks)


def extract_pdf_markdown(pdf_path: str | Path) -> str:
    """Extract a PDF as Markdown using Microsoft's MarkItDown.

    Used by the ``copilot_sdk`` provider since Copilot CLI doesn't accept
    binary PDFs. Raises ImportError when ``markitdown`` is missing so the
    caller can fall back to pypdf with a clear warning.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as e:
        raise ImportError(
            "markitdown is required for the copilot_sdk provider. "
            "Install with: pip install 'markitdown[pdf]'"
        ) from e

    md = MarkItDown()
    try:
        result = md.convert(str(pdf_path))
    except Exception as e:
        raise RuntimeError(
            f"MarkItDown failed to convert {pdf_path}: {type(e).__name__}: {e}. "
            f"Falling back to pypdf would lose table structure."
        ) from e
    return result.text_content or ""


def extract_pdf_for_provider(pdf_path: str | Path, provider: str) -> str:
    """``copilot_sdk`` → MarkItDown markdown; everything else → pypdf text.
    Falls back to pypdf with a warning if MarkItDown is requested but
    not installed, so the pipeline never hard-fails on extraction.
    """
    if provider == "copilot_sdk":
        try:
            md = extract_pdf_markdown(pdf_path)
            logger.info("Extracted PDF via MarkItDown (%d chars) for copilot_sdk", len(md))
            return md
        except ImportError as e:
            logger.warning(
                "copilot_sdk requested but %s. Falling back to pypdf — "
                "table structure will be lost in the input Copilot sees.", e
            )
    return extract_pdf_text(pdf_path)


def extract_paper_summary_llm(text: str, llm_client) -> Dict[str, str]:
    """Use the LLM to extract title and abstract from the paper text.

    Sends the first ~4 000 chars to the LLM and parses a two-line
    response of the form::

        Title: <exact title>
        Abstract: <full abstract>

    Falls back to :func:`extract_paper_summary` if the call fails or
    the response cannot be parsed into both fields.
    """
    head = text[:4000]
    system = (
        "You are a research paper metadata extractor. "
        "Extract the exact title and abstract from the paper text the user provides. "
        "Reply in this exact format and nothing else:\n"
        "Title: <exact paper title>\n"
        "Abstract: <full abstract text>"
    )
    user = f"Extract the title and abstract:\n\n{head}"

    try:
        raw = llm_client.complete(system, user, max_tokens=600)
    except Exception as e:
        logger.warning(
            "LLM title/abstract extraction failed (%s: %s); falling back to heuristic.",
            type(e).__name__, e,
        )
        return extract_paper_summary(text)

    # Parse title: first line starting with "Title:"
    title = ""
    abstract = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not title and stripped.lower().startswith("title:"):
            title = stripped[len("title:"):].strip()
        elif not abstract and stripped.lower().startswith("abstract:"):
            abstract = stripped[len("abstract:"):].strip()
        elif abstract:
            # continuation lines of the abstract
            abstract += " " + stripped

    abstract = re.sub(r"\s+", " ", abstract).strip()

    if not title or not abstract:
        logger.warning(
            "LLM extraction returned unusable metadata (title=%r abstract_len=%d); "
            "falling back to heuristic.",
            title, len(abstract),
        )
        return extract_paper_summary(text)

    logger.info("LLM extracted title: %s", title[:80])
    return {"title": title, "abstract": abstract, "full_text": text}


def extract_paper_summary(text: str) -> Dict[str, str]:
    """Heuristic extraction of title + abstract — used as fallback only.

    Called when the LLM extraction in :func:`extract_paper_summary_llm`
    fails or returns an unparseable response.
    """
    head = text[:6000]
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]

    # Skip bare numbers — some PDFs emit margin line-numbers (1, 2, 3 …)
    # as the very first lines, which would otherwise become the "title".
    # Also skip fragments shorter than 8 chars (single letters, symbols).
    title = "Unknown Title"
    for ln in lines:
        if re.fullmatch(r"\d+", ln):
            continue
        if len(ln) < 8:
            continue
        title = ln
        break

    abstract = ""
    m = re.search(
        r"(?im)^\s*abstract[:\s\-]*\n?(.*?)(?=\n\s*(?:1[\.\s]+introduction|keywords|index terms|i\.\s+introduction))",
        head,
        re.DOTALL,
    )
    if m:
        abstract = re.sub(r"\s+", " ", m.group(1)).strip()
    else:
        abstract = re.sub(r"\s+", " ", head[len(title):2000]).strip()[:1500]

    return {"title": title, "abstract": abstract, "full_text": text}
