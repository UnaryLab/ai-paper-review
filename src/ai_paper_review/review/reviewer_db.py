"""Reviewer database parser.

Parses the bundled (or user-uploaded) reviewer-database markdown into
two things:

* a list of :class:`Reviewer` records — the unit of dispatch for the
  review pipeline (one per ``#### R### — Persona`` heading);
* an :class:`AttributionTables` block — category vocab and the
  category/sub-rating → persona maps the validation calibration step
  uses to attribute missed human comments and low sub-ratings to the
  persona that should have caught them.

:func:`parse_reviewer_database` returns both wrapped in
:class:`ReviewerDatabase`. :func:`parse_reviewer_db` is a thin back-compat
wrapper that returns just the reviewers list, for the review pipeline
callers that never touch the attribution tables.

The DB format is documented in ``docs/database_format.md``; one
``#### R### — Persona`` header per reviewer, followed by metadata
``- **Field:** value`` lines and a fenced ``text`` system-prompt block,
plus a ``## … Validation Attribution Tables`` section carrying one
fenced ``yaml`` block.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("review_system")


REVIEWER_HEADER_RE = re.compile(r"^####\s+(R\d{3})\s+—\s+(.+?)\s*$", re.MULTILINE)
FIELD_RE = re.compile(r"^-\s+\*\*(.+?):\*\*\s+(.+?)\s*$", re.MULTILINE)
SYSTEM_PROMPT_RE = re.compile(
    r"-\s+\*\*System Prompt:\*\*\s*\n+```text\n(.*?)\n```",
    re.DOTALL,
)
# Locate the validation attribution-tables YAML block. The section can
# be numbered or not (``## Validation Attribution Tables`` or
# ``## 7. Validation Attribution Tables``); only the first yaml fence
# that follows is picked up.
_ATTRIBUTION_SECTION_RE = re.compile(
    r"##\s+\d*\.?\s*Validation Attribution Tables.*?```yaml\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class Reviewer:
    id: str
    persona: str
    domain: str
    focus: str
    style: str
    keywords: List[str]
    system_prompt: str

    def keyword_text(self) -> str:
        """Concatenated text used for similarity scoring against the paper."""
        return " ".join(self.keywords + [self.domain, self.persona, self.focus])


@dataclass
class AttributionTables:
    """Validation-calibration tables embedded in the reviewer database.

    Every persona string in the maps MUST match the persona name of
    some reviewer in the same DB — otherwise the calibration step will
    attribute missed comments to phantom personas that aren't in the
    selection pool. The DB loader does not enforce this; it's checked
    by :func:`validate_tables_against_reviewers` at the caller's
    discretion.
    """
    category_vocab: List[str] = field(default_factory=list)
    category_to_persona: Dict[str, str] = field(default_factory=dict)
    sub_rating_to_persona: Dict[str, str] = field(default_factory=dict)


@dataclass
class ReviewerDatabase:
    reviewers: List[Reviewer]
    tables: AttributionTables


def _parse_reviewers(text: str, path: str | Path) -> List[Reviewer]:
    header_matches = list(REVIEWER_HEADER_RE.finditer(text))
    n = len(header_matches)
    if n == 0:
        logger.warning(
            "No reviewer headers matched in %s — expected lines like "
            "'#### R001 — Persona Name'. File may be empty or malformed.",
            path,
        )
    elif n < 5:
        logger.warning(
            "Only %d reviewer header(s) matched in %s — this looks suspiciously "
            "small. Check the file is a valid reviewer database.",
            n, path,
        )
    else:
        logger.info("Parsed %d reviewer headers from %s", n, path)

    reviewers: List[Reviewer] = []
    for i, m in enumerate(header_matches):
        rid = m.group(1)
        persona = m.group(2).strip()
        start = m.end()
        end = header_matches[i + 1].start() if i + 1 < len(header_matches) else len(text)
        block = text[start:end]

        sp_match = SYSTEM_PROMPT_RE.search(block)
        if not sp_match:
            logger.warning("No system prompt for reviewer %s", rid)
            continue

        # Only scan metadata BEFORE the system prompt — the prompt itself
        # contains literal `- **Keywords:** ...` lines as LLM-output-format
        # instructions, which would otherwise clobber the real metadata.
        header_region = block[:sp_match.start()]
        fields = {k.strip(): v.strip() for k, v in FIELD_RE.findall(header_region)}

        keywords = [k.strip() for k in fields.get("Keywords", "").split(",") if k.strip()]
        reviewers.append(
            Reviewer(
                id=rid,
                persona=persona,
                domain=fields.get("Domain", ""),
                focus=fields.get("Focus", ""),
                style=fields.get("Review Style", ""),
                keywords=keywords,
                system_prompt=sp_match.group(1).strip(),
            )
        )

    return reviewers


def _parse_attribution_tables(text: str, path: str | Path) -> AttributionTables:
    """Extract the validation-calibration tables from the DB markdown.

    Returns empty tables when the block is missing — callers (validation
    calibration) then effectively skip attribution and log a warning
    rather than crashing. User-uploaded DBs without the block therefore
    still work for the review pipeline; validation against them just
    produces less useful calibration.
    """
    m = _ATTRIBUTION_SECTION_RE.search(text)
    if not m:
        logger.warning(
            "No 'Validation Attribution Tables' YAML block in %s. "
            "Validation calibration against this DB will skip miss "
            "attribution and sub-rating routing.",
            path,
        )
        return AttributionTables()

    try:
        import yaml
    except ImportError as e:  # pragma: no cover — yaml is a core dep
        raise ImportError(
            "pyyaml is required to parse the reviewer database's "
            "attribution tables."
        ) from e

    try:
        data = yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        logger.warning(
            "Failed to parse 'Validation Attribution Tables' YAML in %s "
            "(%s: %s). Falling back to empty tables.",
            path, type(e).__name__, e,
        )
        return AttributionTables()

    return AttributionTables(
        category_vocab=[str(c).strip() for c in (data.get("category_vocab") or [])],
        category_to_persona={
            str(k).strip().lower(): str(v).strip()
            for k, v in (data.get("category_to_persona") or {}).items()
        },
        sub_rating_to_persona={
            str(k).strip().lower(): str(v).strip()
            for k, v in (data.get("sub_rating_to_persona") or {}).items()
        },
    )


def parse_reviewer_database(path: str | Path) -> ReviewerDatabase:
    """Parse the markdown DB into reviewer entries + validation tables."""
    text = Path(path).read_text()
    return ReviewerDatabase(
        reviewers=_parse_reviewers(text, path),
        tables=_parse_attribution_tables(text, path),
    )


def parse_reviewer_db(path: str | Path) -> List[Reviewer]:
    """Return just the reviewer list.

    The review pipeline never touches the attribution tables, so its
    callers stay on this thinner return type. Validation-side callers
    should use :func:`parse_reviewer_database` instead.
    """
    return parse_reviewer_database(path).reviewers
