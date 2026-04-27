"""
ai-paper-review-generate-db — generate a reviewer-database markdown from a YAML config.

Usage
-----
    ai-paper-review-generate-db [--config PATH] [--out PATH]

    --config PATH   YAML config file (default: bundled comparch_reviewer_cfg.yaml).
    --out    PATH   Output markdown path (default: ./<field-slug>_reviewer_db.md).

YAML format
-----------
The config must contain ``field``, ``domains``, ``personas``, and
``validation_attribution``. ``version`` defaults to "1.0" if omitted:

    field: "machine learning and AI"
    version: "1.0"
    domains:
      - id: D1
        name: Deep Learning Theory & Foundations
        ...
    personas:
      - name: Novelty Hunter
        ...
    validation_attribution:
      category_vocab: [...]
      category_to_persona: {...}
      sub_rating_to_persona: {...}

See docs/database_format.md for the full field-by-field spec.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Derive validation attribution tables from a personas list.
# Used when the config omits validation_attribution. Every persona name
# produced is guaranteed to exist in the db because it comes from the same
# personas list used to generate the reviewer entries.
# ---------------------------------------------------------------------------
_SUB_RATING_SLUG_HINTS: dict[str, list[str]] = {
    # OpenReview sub-rating → list of persona slug substrings to try in order
    "soundness":      ["methodology", "theory", "stats", "rigor"],
    "presentation":   ["clarity", "writing", "presentation"],
    "contribution":   ["novelty", "originality"],
    "clarity":        ["clarity", "writing", "presentation"],
    "significance":   ["novelty", "vision", "impact"],
    "technical":      ["methodology", "theory", "formal"],
    "reproducibility": ["reproducibility", "artifact"],
}


def _derive_attribution(personas: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a minimal validation_attribution from the personas list.

    - category_vocab: one entry per persona slug.
    - category_to_persona: slug → name, plus name-word fragments as fuzzy keys.
    - sub_rating_to_persona: heuristic match on slug substrings; falls back to
      the first persona in the list if no hint matches.
    """
    category_vocab: list[str] = []
    category_to_persona: dict[str, str] = {}

    for p in personas:
        slug: str = p.get("slug", "")
        name: str = p["name"]
        if slug:
            category_vocab.append(slug)
            category_to_persona[slug] = name
        # Also index each lowercase word of the name that is ≥4 chars and not
        # already a key, so e.g. "novelty" routes "Novelty Hunter" even if the
        # slug differs.
        for word in name.lower().split():
            word = word.strip("&,.")
            if len(word) >= 4 and word not in category_to_persona:
                category_to_persona[word] = name

    # sub_rating_to_persona — match hints against slugs, fall back to first persona.
    sub_rating_to_persona: dict[str, str] = {}
    slug_to_name = {p.get("slug", ""): p["name"] for p in personas}
    fallback = personas[0]["name"] if personas else ""
    for rating, hints in _SUB_RATING_SLUG_HINTS.items():
        matched = fallback
        for hint in hints:
            for slug, name in slug_to_name.items():
                if hint in slug:
                    matched = name
                    break
            else:
                continue
            break
        sub_rating_to_persona[rating] = matched

    return {
        "category_vocab": category_vocab,
        "category_to_persona": category_to_persona,
        "sub_rating_to_persona": sub_rating_to_persona,
    }

# ---------------------------------------------------------------------------
# System-prompt template — filled once per reviewer
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are **Reviewer {rid}**, an expert peer reviewer for {field} research, specialized in **{domain_name}**. You adopt the persona of a **{persona_name}**: your reviewing lens emphasizes {persona_focus}.

## Expertise Profile
- **Sub-area**: {domain_name} — {domain_description}
- **Typical venues you review for**: {venues}
- **Background**: You have deep familiarity with {bg_keywords}, and you track recent developments in this area.

## Review Lens ({persona_name})
- **Style**: {persona_style}
- **Core questions you always ask**:
{questions}
- **Patterns you flag most often**: {patterns}

## Your Task
Read the paper provided in the user message. Produce between **5 and 10 review comments** — prefer quality over quantity. If you only have 5 or 6 genuine concerns under your lens, stop there rather than padding to reach 10. Five sharp observations beat ten that include filler. Focus on issues that fall under your domain and your persona lens; ignore aspects outside your remit (other reviewers cover them).

Do NOT try to be balanced across topics. If one dimension of the paper is weak under your lens, it is fine and expected for multiple comments to converge on that same weakness — commonality across reviewers is a signal the system uses downstream.

## Output Format
Return your review in **markdown** using exactly this structure. Do not add any prose outside this format.

```
# Review

**Reviewer ID:** {rid}
**Domain:** {domain_name}
**Persona:** {persona_name}
**Topic Relevance:** <float 0.0-1.0>
**Overall Recommendation:** <strong_accept | accept | weak_accept | borderline | weak_reject | reject | strong_reject>
**Confidence:** <int 1-5>

## Comment 1
- **Severity:** <major | moderate | minor>
- **Category:** <short tag, e.g. novelty, methodology, evaluation, writing, security, cost>
- **Section Reference:** <paper section/figure/table if applicable, else general>
- **Summary:** <one-sentence summary of the issue>
- **Description:** <2-4 sentences explaining the concern in detail>
- **Keywords:** <comma-separated keywords>

## Comment 2
...
```

## Rules
1. Produce **between 5 and 10** comments — no more, no less.
2. Stay within your persona lens: do not write generic comments a non-specialized reviewer could write.
3. Severity is calibrated: `major` = would block acceptance; `moderate` = requires significant revision; `minor` = improvement but not blocking.
4. Use the `Keywords` field to tag each comment — these are used downstream for clustering.
5. If the paper is clearly outside your domain, set `Topic Relevance` below 0.3, still produce comments but keep them high-level and note the mismatch in the first comment.
6. Output the markdown review only. No commentary or explanation before or after.\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _bundled_config() -> dict[str, Any]:
    from importlib.resources import files
    cfg_path = Path(str(files("ai_paper_review.database").joinpath("comparch_reviewer_cfg.yaml")))
    return _load_yaml(cfg_path)


def _bg_keywords(keywords: list[str], persona_idx: int, n: int = 8) -> str:
    """Return n keywords starting at an offset that rotates with persona_idx."""
    if not keywords:
        return ""
    start = (persona_idx * 3) % len(keywords)
    selected = [keywords[(start + i) % len(keywords)] for i in range(min(n, len(keywords)))]
    return ", ".join(selected)


def _format_questions(priorities: list[str]) -> str:
    return "\n".join(f"    {i}. {q}" for i, q in enumerate(priorities[:5], 1))


def _field_slug(field: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", field.strip().lower()).strip("_")


def _render_attribution_yaml(attr: dict[str, Any]) -> str:
    lines: list[str] = ["```yaml"]

    lines.append("category_vocab:")
    for v in attr.get("category_vocab", []):
        lines.append(f"  - {v}")

    lines.append("")
    lines.append("category_to_persona:")
    prev_persona = None
    for cat, persona in attr.get("category_to_persona", {}).items():
        if prev_persona and persona != prev_persona:
            lines.append("")
        pad = max(1, 20 - len(cat))
        lines.append(f"  {cat}:{' ' * pad}{persona}")
        prev_persona = persona

    lines.append("")
    lines.append("sub_rating_to_persona:")
    for rating, persona in attr.get("sub_rating_to_persona", {}).items():
        pad = max(1, 14 - len(rating))
        lines.append(f"  {rating}:{' ' * pad}{persona}")

    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate(config: dict[str, Any]) -> str:
    """Return the full reviewer-database markdown string for the given config."""
    if "personas" not in config:
        raise ValueError(
            "Config is missing required key 'personas'. "
            "Add a personas: list to your YAML — see docs/database_format.md §1."
        )
    if "domains" not in config:
        raise ValueError(
            "Config is missing required key 'domains'. "
            "Add a domains: list to your YAML — see docs/database_format.md §1."
        )
    if "field" not in config:
        raise ValueError(
            "Config is missing required key 'field'. "
            "Add field: \"your discipline\" to your YAML — see docs/database_format.md §1."
        )
    if "validation_attribution" not in config:
        raise ValueError(
            "Config is missing required key 'validation_attribution'. "
            "Add a validation_attribution: block with category_vocab, category_to_persona, "
            "and sub_rating_to_persona — see docs/database_format.md §1.4."
        )

    field: str = config["field"]
    version: str = config.get("version", "1.0")
    domains: list[dict] = config["domains"]
    personas: list[dict] = config["personas"]
    attribution: dict = config["validation_attribution"]

    n_domains = len(domains)
    n_personas = len(personas)
    total = n_domains * n_personas

    lines: list[str] = []

    # ------------------------------------------------------------------
    # Header and sections 1–4
    # ------------------------------------------------------------------
    title = field.title() + " Reviewer Database"
    lines += [
        f"# {title}",
        "",
        f"**Version:** {version}",
        f"**Total Reviewers:** {total}",
        f"**Domains:** {n_domains} × **Personas per Domain:** {n_personas}",
        "",
        "---",
        "",
        "## 1. Overview",
        "",
        "This document is the **prompt database** for an automated peer-review system. It defines",
        f"{total} independent reviewer agents, organized as a {n_domains}-by-{n_personas} matrix:",
        "",
        f"- **{n_domains} sub-domains** of {field} (rows).",
        f"- **{n_personas} reviewing personas** per domain (columns), each capturing a distinct reviewing aspect",
        "  (novelty, methodology, reproducibility, security, cost, deployment, etc.).",
        "",
        "The file is consumed by a LangGraph program (`ai_paper_review.review`) that:",
        "",
        "1. Accepts a draft paper (PDF) as input.",
        "2. Extracts topic keywords and embeds them.",
        "3. Selects the **top-N reviewers** by topic similarity against each reviewer's",
        "   `keywords` field, where N is chosen by the user at run time (default 10,",
        "   recommended range 5–10 to balance speed and accuracy, hard range 1–20).",
        "4. Runs the N selected reviewers in parallel; each produces 5–10 structured markdown comments.",
        "5. Aggregates all comments, clusters semantically similar ones, and ranks clusters by",
        "   **(commonality across reviewers) × (severity / importance)**.",
        "6. Emits a ranked, deduplicated review report.",
        "",
        "---",
        "",
        "## 2. Standard Reviewer Template",
        "",
        "Every reviewer entry below follows the same standard template:",
        "",
        "```",
        "### R### — <Persona Name>",
        "- **Domain:**       <one of the sub-domains>",
        "- **Persona:**      <one of the reviewing aspects>",
        "- **Focus:**        <one-line focus statement>",
        "- **Review Style:** <how this reviewer approaches critique>",
        "- **Keywords:**     <comma-separated topic tags for similarity matching>",
        "- **System Prompt:**",
        "  <multi-line LLM system prompt; fully self-contained, produces structured markdown output matching the AI-review format (see docs/review_output_format.md)>",
        "```",
        "",
        "The **Keywords** list is the anchor used by the LangGraph selector to match reviewers",
        "to an incoming paper. The **System Prompt** is the full instruction given to the",
        "underlying LLM when that reviewer is invoked.",
        "",
        "---",
        "",
        "## 3. Domain Index",
        "",
        "| # | Domain | Reviewer ID Range | Keywords (short) |",
        "|---|---|---|---|",
    ]

    for di, d in enumerate(domains):
        start = di * n_personas + 1
        end = start + n_personas - 1
        kws = d.get("keywords", [])
        short_kws = ", ".join(kws[:6]) + (", ..." if len(kws) > 6 else "")
        lines.append(f"| {di+1} | {d['name']} | R{start:03d}–R{end:03d} | {short_kws} |")

    lines += [
        "",
        "---",
        "",
        "## 4. Persona Index",
        "",
        "All personas are replicated in every domain. This guarantees that each sub-area is",
        "represented by the full spectrum of reviewing concerns.",
        "",
        "| # | Persona | Focus |",
        "|---|---|---|",
    ]
    for pi, p in enumerate(personas):
        lines.append(f"| {pi+1} | {p['name']} | {p['focus']} |")

    lines += [
        "",
        "---",
        "",
        "## 5. Reviewer Entries",
        "",
        "---",
    ]

    # ------------------------------------------------------------------
    # Section 5 — reviewer entries
    # ------------------------------------------------------------------
    for di, domain in enumerate(domains):
        start_rid = di * n_personas + 1
        kws_list: list[str] = domain.get("keywords", [])
        kws_str = ", ".join(kws_list)

        lines += [
            "",
            f"### Domain {domain.get('id', f'D{di+1}')}: {domain['name']}",
            "",
            f"> {domain.get('description', '')}",
            "",
            f"**Canonical keywords:** {kws_str}",
            "",
            f"**Typical venues:** {domain.get('venues', '')}",
            "",
        ]

        for pi, persona in enumerate(personas):
            rid = f"R{start_rid + pi:03d}"
            prompt = _SYSTEM_PROMPT.format(
                rid=rid,
                field=field,
                domain_name=domain["name"],
                domain_description=domain.get("description", ""),
                venues=domain.get("venues", ""),
                bg_keywords=_bg_keywords(kws_list, pi),
                persona_name=persona["name"],
                persona_focus=persona["focus"],
                persona_style=persona["style"],
                questions=_format_questions(persona.get("priorities", [])),
                patterns=persona.get("common_concerns", ""),
            )
            lines += [
                f"#### {rid} — {persona['name']}",
                "",
                f"- **Domain:** {domain['name']}",
                f"- **Persona:** {persona['name']}",
                f"- **Focus:** {persona['focus']}",
                f"- **Review Style:** {persona['style']}",
                f"- **Keywords:** {kws_str}",
                "- **System Prompt:**",
                "",
                "```text",
                prompt,
                "```",
                "",
            ]

    # ------------------------------------------------------------------
    # Section 6 — programmatic access
    # ------------------------------------------------------------------
    sample_d = domains[min(1, len(domains) - 1)]
    sample_p = personas[min(9, len(personas) - 1)]
    sample_kws_repr = str(sample_d.get("keywords", [])[:3] + ["..."]).replace("'", '"')

    lines += [
        "---",
        "",
        "## 6. Programmatic Access",
        "",
        "The LangGraph driver loads this markdown and parses each `#### R### — <Persona>` block.",
        "Each block yields a record with the following fields usable in code:",
        "",
        "```python",
        "{",
        f'  "id": "R{n_personas + 1:03d}",',
        f'  "domain": "{sample_d["name"]}",',
        f'  "persona": "{sample_p["name"]}",',
        f'  "focus": "{sample_p["focus"]}",',
        f'  "style": "{sample_p["style"]}",',
        f'  "keywords": {sample_kws_repr},',
        '  "system_prompt": "You are Reviewer R... "',
        "}",
        "```",
        "",
        "See `ai_paper_review.review` for the parsing and orchestration code.",
        "",
        "---",
        "",
        "## 7. Validation Attribution Tables",
        "",
        "Consumed by the validation calibration step (`ai_paper_review.validation.calibration`) to map comment categories and low sub-ratings back to the AI persona that should have caught them. Every persona string on the right-hand side MUST match a persona name from the `#### R### — <Persona>` headings in section 5 above — edit the two together or the calibration will point at personas that don't exist in this DB.",
        "",
        "- `category_vocab` — the closed list of category strings the LLM conversion step is told to pick from when structuring a human review. Values not in this list are blanked out at normalization time.",
        "- `category_to_persona` — lowercase category (or near-miss keyword) → persona. The routing helper fuzzy-matches keys inside category strings, so `\"methodology\"` also routes via `\"methodology and experimental rigor\"`.",
        "- `sub_rating_to_persona` — lowercase sub-rating name (OpenReview-style: Soundness, Presentation, Contribution, …) → persona.",
        "",
        _render_attribution_yaml(attribution),
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ai-paper-review-generate-db",
        description="Generate a reviewer-database markdown from a YAML config.",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="YAML config file (default: bundled comparch_reviewer_cfg.yaml).",
    )
    parser.add_argument(
        "--out", metavar="PATH",
        help="Output markdown path (default: ./<field-slug>_reviewer_db.md).",
    )
    args = parser.parse_args(argv)

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Error: config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        config = _load_yaml(config_path)
    else:
        config = _bundled_config()

    try:
        md = generate(config)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = Path(f"{_field_slug(config['field'])}_reviewer_db.md")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    n_reviewers = len(re.findall(r"^#### R\d+", md, re.MULTILINE))
    print(f"Written {out_path}  ({out_path.stat().st_size // 1024} KB, {n_reviewers} reviewers)")


if __name__ == "__main__":
    main()
