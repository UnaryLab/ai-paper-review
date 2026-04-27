"""Bundled docs browser: ``/docs`` index + ``/docs/<slug>`` rendered pages.

Uses Python-Markdown when available; falls back to a minimal built-in
renderer covering the subset our bundled docs actually use (headings,
fences, tables, lists, blockquotes, hr, inline patterns).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from flask import abort, flash, redirect, render_template, url_for

from .app import DOCS_ROOT, app, logger


# Whitelist of (slug, path-relative-to-DOCS_ROOT, title, teaser). Kept
# explicit (not glob-based) so a new doc can't slip into the UI before
# it's deliberately added.
_DOCS_MANIFEST = [
    ("readme",              "README.md",
     "README",
     "Project overview, quick start, install, workflow diagrams."),
    ("llm_providers",       "docs/llm_providers.md",
     "LLM Providers",
     "Supported providers (Anthropic, OpenAI, Gemini, xAI, GitHub, Copilot SDK, OpenAI-compatible)."),
    ("database-format",     "docs/database_format.md",
     "Database Format",
     "Reviewer-database YAML and markdown formats; build-a-DB walkthrough."),
    ("review-pipeline",     "docs/review_pipeline.md",
     "Review Pipeline",
     "End-to-end review pipeline — inputs, outputs, stage diagram."),
    ("review-output-format", "docs/review_output_format.md",
     "Review Output Format",
     "Per-review markdown format — headers, comment blocks, parsing rules."),
    ("validation-pipeline", "docs/validation_pipeline.md",
     "Validation Pipeline",
     "End-to-end validation pipeline — conversion, alignment, calibration, reporting."),
    ("validation-output-format", "docs/validation_output_format.md",
     "Validation Output Format",
     "Validation run artifacts, alignment semantics, calibration_delta.json schema."),
    ("aggregation",         "docs/aggregation.md",
     "Aggregation",
     "Cross-paper aggregation of calibration deltas into reviewer-database tuning recommendations."),
]


def _available_docs() -> List[Dict[str, Any]]:
    """Manifest entries whose on-disk file exists; empty when running
    from an installed wheel without the source tree."""
    if DOCS_ROOT is None:
        return []
    out: List[Dict[str, Any]] = []
    for slug, rel, title, teaser in _DOCS_MANIFEST:
        p = DOCS_ROOT / rel
        if not p.exists():
            continue
        out.append({"slug": slug, "title": title, "teaser": teaser, "path": p})
    return out


def _render_markdown_to_html(md_text: str) -> str:
    """Prefer Python-Markdown; fall back to a minimal hand-rolled
    renderer if that import fails (partial installs or minimal envs)."""
    try:
        import markdown
    except ImportError:
        logger.info("python-markdown not installed; using fallback renderer. "
                    "Run `pip install markdown>=3.4` for richer rendering.")
        return _render_markdown_fallback(md_text)
    return markdown.markdown(
        md_text,
        extensions=["fenced_code", "tables", "toc", "sane_lists"],
        output_format="html5",
    )


def _render_markdown_fallback(md: str) -> str:
    """Minimal markdown → HTML for the bundled docs when python-markdown
    isn't installed. Covers the subset our docs actually use: ATX
    headings, fenced code blocks, tables, inline code/bold/italic,
    bulleted/numbered lists, blockquotes, hr, links, paragraphs.
    HTML-escape-safe: text is escaped before inline patterns run.
    """
    import html as _html

    lines = md.split("\n")
    out: List[str] = []
    i = 0
    in_list = None  # 'ul' | 'ol' | None

    def _close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    def _inline(s: str) -> str:
        s = _html.escape(s)
        # Inline code first so **/* inside it isn't processed.
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        # Skip italic when * is adjacent to alphanumerics on one side
        # only — avoids mangling filenames that contain asterisks.
        s = re.sub(r"(?<!\w)\*([^*\s][^*]*?)\*(?!\w)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                   r'<a href="\2">\1</a>', s)
        return s

    while i < len(lines):
        ln = lines[i]
        stripped = ln.rstrip()

        m = re.match(r"^```(\w*)\s*$", stripped)
        if m:
            _close_list()
            lang = m.group(1)
            i += 1
            code_lines: List[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            cls = f' class="lang-{lang}"' if lang else ""
            code_body = _html.escape("\n".join(code_lines))
            out.append(f"<pre><code{cls}>{code_body}</code></pre>")
            continue

        if re.match(r"^---+\s*$", stripped):
            _close_list()
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        if m:
            _close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        if "|" in stripped and i + 1 < len(lines) and re.match(
                r"^\s*\|?[\s\-:|]+\|?\s*$", lines[i + 1]):
            _close_list()
            def _cells(row: str) -> List[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            header = _cells(stripped)
            i += 2  # skip separator
            out.append("<table><thead><tr>")
            for h in header:
                out.append(f"<th>{_inline(h)}</th>")
            out.append("</tr></thead><tbody>")
            while i < len(lines) and "|" in lines[i].strip():
                row = _cells(lines[i])
                out.append("<tr>")
                for c in row:
                    out.append(f"<td>{_inline(c)}</td>")
                out.append("</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        m = re.match(r"^>\s?(.*)$", stripped)
        if m:
            _close_list()
            quote_lines = [m.group(1)]
            i += 1
            while i < len(lines):
                mm = re.match(r"^>\s?(.*)$", lines[i].rstrip())
                if not mm:
                    break
                quote_lines.append(mm.group(1))
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(quote_lines))}</blockquote>")
            continue

        m_ul = re.match(r"^(\s*)[-*]\s+(.*)$", ln)
        m_ol = re.match(r"^(\s*)(\d+)\.\s+(.*)$", ln)
        if m_ul:
            if in_list != "ul":
                _close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{_inline(m_ul.group(2))}</li>")
            i += 1
            continue
        if m_ol:
            if in_list != "ol":
                _close_list()
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{_inline(m_ol.group(3))}</li>")
            i += 1
            continue

        if not stripped:
            _close_list()
            i += 1
            continue

        _close_list()
        para: List[str] = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].rstrip()
            if not nxt:
                break
            if re.match(r"^(#{1,6}\s|```|>\s|---+\s*$|[-*]\s+|\d+\.\s+)", nxt):
                break
            if "|" in nxt and i + 1 < len(lines) and re.match(
                    r"^\s*\|?[\s\-:|]+\|?\s*$", lines[i + 1]):
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    _close_list()
    return "\n".join(out)


def _rewrite_doc_links(html: str, current_doc_path: Path) -> str:
    """Rewrite internal ``.md`` links in a rendered doc to ``/docs/<slug>``.

    The raw markdown cross-references files relatively (e.g.
    ``docs/database_format.md`` from README), which would otherwise
    render as broken hrefs against the browser's URL. Anchor fragments
    are preserved; external links and unknown targets are left alone
    (so unknown targets stay visibly broken rather than misrouting).
    """
    if DOCS_ROOT is None:
        return html

    path_to_slug = {}
    for slug, rel, _title, _teaser in _DOCS_MANIFEST:
        p = (DOCS_ROOT / rel).resolve()
        path_to_slug[p] = slug

    doc_dir = current_doc_path.parent

    def _sub(m: "re.Match[str]") -> str:
        prefix, href, suffix = m.group(1), m.group(2), m.group(3)
        if href.startswith(("http://", "https://", "mailto:", "#", "/")):
            return m.group(0)
        if "#" in href:
            path_part, anchor = href.split("#", 1)
            anchor = "#" + anchor
        else:
            path_part, anchor = href, ""
        if not path_part.endswith(".md"):
            return m.group(0)
        try:
            resolved = (doc_dir / path_part).resolve()
        except Exception:
            return m.group(0)
        slug = path_to_slug.get(resolved)
        if slug is None:
            return m.group(0)
        new_href = url_for("docs_view", slug=slug) + anchor
        return f'{prefix}{new_href}{suffix}'

    return re.sub(r'(<a\s[^>]*href=")([^"]+)(")', _sub, html)


@app.get("/docs")
def docs_index():
    return render_template(
        "docs_index.html",
        docs=_available_docs(),
        source_available=DOCS_ROOT is not None,
    )


@app.get("/docs/<slug>")
def docs_view(slug: str):
    for entry in _available_docs():
        if entry["slug"] == slug:
            try:
                html = _render_markdown_to_html(entry["path"].read_text())
                html = _rewrite_doc_links(html, entry["path"])
            except Exception as e:
                logger.exception("Failed to render %s", entry["path"])
                flash(f"Could not render {slug!r}: {e}")
                return redirect(url_for("docs_index"))
            return render_template(
                "docs_view.html",
                docs=_available_docs(),
                current_slug=slug,
                title=entry["title"],
                source_path=str(entry["path"].relative_to(DOCS_ROOT)),
                content_html=html,
            )
    abort(404)
