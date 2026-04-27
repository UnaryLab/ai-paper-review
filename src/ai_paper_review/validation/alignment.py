"""Single-call batch alignment of human ↔ AI comments via the LLM.

The LLM receives both the human-review and AI-review comments in one
prompt and returns every pairwise similarity score. One request — no
quota-destroying N×M fan-out.

Verdict per human comment comes from its best-scoring AI pair:

* ``same``    if best similarity ≥ ``BATCH_SAME_THR``
* ``partial`` if best similarity ≥ ``BATCH_PARTIAL_THR``
* ``missed``  otherwise

The two prompts (system + user template) live as ``.md`` files in
``ai_paper_review.prompts``; the rendering of placeholders happens here
so the LLM call site stays compact.

When ``run_dir`` is given, three audit-trail markdown files are
written next to the report (``alignment_llm_analysis.md``,
``alignment_similarities.md``, ``alignment_ranking.md``) so the user
can inspect what the LLM actually said.
"""
from __future__ import annotations

import logging
import pathlib
import re
from typing import Any, Dict, List, Optional

import numpy as np

from ai_paper_review import prompts

from .constants import BATCH_PARTIAL_THR, BATCH_SAME_THR

logger = logging.getLogger("validator")


def _fmt_comments_for_prompt(
    comments: List[Dict[str, Any]],
    side: str,  # "human" | "ai"
) -> str:
    """Render a flat list of comment dicts into a compact numbered block
    the LLM can reason over. Uses each comment's ``id`` verbatim so the
    LLM echoes it back in the response and we can regex it out."""
    lines: List[str] = []
    for c in comments:
        cid = c.get("id") or c.get("comment_id") or "?"
        if side == "human":
            text = (c.get("text", "") or "").strip()
        else:
            summ = (c.get("summary", "") or "").strip()
            desc = (c.get("description", "") or c.get("text", "") or "").strip()
            text = (summ + " — " + desc) if (summ and desc) else (summ or desc)
        text = text.replace("\n", " ").strip()[:800]
        lines.append(f"- **{cid}**: {text}")
    return "\n".join(lines)


def _parse_batch_similarity_matrix(
    raw: str,
    human_ids: List[str],
    ai_ids: List[str],
):
    """Parse the LLM's batch response into an (n_human × n_ai) float
    matrix. Tolerant to several formats LLMs actually produce in the wild:

    * ``H1 | A1 | 0.85`` (pipe-separated) or comma / whitespace variants
    * Full real IDs: ``Reviewer_qFvT-C1 | R001-C1 | 0.85``
    * **Compacted IDs** — the LLM drops a shared prefix, emitting e.g.
      ``Reviewer_406A-C1 | -C8 | 0.75`` (suffix kept)
    * **Positional shorthand** — ``H1`` / ``A1`` or ``C1`` / ``-C1`` where
      the digit is a 1-based index into the block that came in the prompt

    The resolver tries, in order:

    1. exact match against the ID list
    2. suffix match (``-C8`` → the unique AI id ending with ``-C8``)
    3. ``C<n>`` / ``-C<n>`` → the n-th entry in the list (1-based)
    4. ``H<n>`` / ``A<n>`` → the n-th entry in the appropriate list

    If the exact-match pass returns zero rows we retry with the fuzzy
    resolvers and log a warning, so fresh LLM output format drift never
    silently yields an all-zero matrix.
    """
    sims = np.zeros((len(human_ids), len(ai_ids)), dtype=np.float32)
    h_idx = {h: i for i, h in enumerate(human_ids)}
    a_idx = {a: j for j, a in enumerate(ai_ids)}

    # Tokens can start with ``-`` so ``-C8`` is captured (compacted IDs).
    # The score group accepts a float 0..1 or a percent (normalized later).
    row_pat = re.compile(
        r"(?:^|\n)\s*\**\s*([A-Za-z][\w\-]*|-C\d+)\**\s*[|,\s]+\**\s*"
        r"([A-Za-z][\w\-]*|-C\d+)\**\s*[|,\s]+\**\s*"
        r"(\d*\.\d+|\d+(?:\.\d*)?)\**",
        re.MULTILINE,
    )

    _pos_hA_re = re.compile(r"^([HhAa])(\d+)$")
    _pos_cN_re = re.compile(r"^-?C(\d+)$", re.IGNORECASE)

    def _resolve(token: str, role: str, lst: List[str], direct_idx: dict):
        """``role`` is ``"h"`` (human) or ``"a"`` (ai) — used only to decide
        whether an ``H<n>``/``A<n>`` shorthand is for the correct side.
        Returns the resolved ID or ``None`` if no rule matches.
        """
        if token in direct_idx:
            return token
        # Suffix match handles `-C8` when the real id is `R001-C8`. Only
        # accept when exactly one id ends with the token, otherwise it's
        # ambiguous across reviewers that share a `-C<n>` suffix.
        suffix_matches = [x for x in lst if x.endswith(token)]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        m = _pos_cN_re.match(token)
        if m:
            n = int(m.group(1)) - 1
            if 0 <= n < len(lst):
                return lst[n]
        m = _pos_hA_re.match(token)
        if m and m.group(1).lower() == role:
            n = int(m.group(2)) - 1
            if 0 <= n < len(lst):
                return lst[n]
        return None

    def _try_parse(use_fallbacks: bool):
        local = np.zeros((len(human_ids), len(ai_ids)), dtype=np.float32)
        parsed = 0
        for m in row_pat.finditer(raw):
            hid_raw, aid_raw, val = m.group(1), m.group(2), m.group(3)
            if use_fallbacks:
                hid = _resolve(hid_raw, "h", human_ids, h_idx)
                aid = _resolve(aid_raw, "a", ai_ids, a_idx)
            else:
                hid = hid_raw if hid_raw in h_idx else None
                aid = aid_raw if aid_raw in a_idx else None
            if hid is None or aid is None:
                continue
            try:
                v = float(val)
            except ValueError:
                continue
            if v > 1.0:
                v = v / 100.0 if v <= 100.0 else 0.0
            local[h_idx[hid], a_idx[aid]] = max(0.0, min(1.0, v))
            parsed += 1
        return local, parsed

    # Fast path — exact match only.
    sims, n_parsed = _try_parse(use_fallbacks=False)
    if n_parsed > 0:
        return sims, n_parsed

    # Only run the fuzzy resolvers when the strict pass got nothing, so
    # they can't corrupt a run whose real IDs happen to collide with a
    # shorthand form.
    sims2, n2 = _try_parse(use_fallbacks=True)
    if n2 > 0:
        logger.warning(
            "Batch LLM response used compacted / positional IDs instead "
            "of the real comment IDs — recovered %d / %d lines via the "
            "fuzzy resolver (suffix match, -C<n> shorthand, H<n>/A<n>). "
            "Inspect alignment_llm_analysis.md to see what the LLM "
            "actually emitted.",
            n2, len(human_ids) * len(ai_ids),
        )
        return sims2, n2
    return sims, n_parsed


def align_comments_batch_llm(
    actual: List[Dict[str, Any]],
    ai: List[Dict[str, Any]],
    llm_client: Any,
    max_comments_per_side: int = 120,
    run_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """Align human comments to AI comments with a **single** LLM call.

    Returns the same dict shape downstream consumers (metrics,
    calibration, report) expect — see module docstring for details.
    """
    # 120 per side comfortably fits typical 100k+ context windows; bigger
    # values risk truncated responses and degraded reasoning.
    if len(actual) > max_comments_per_side:
        logger.warning(
            "Human review has %d comments; truncating to %d for batch "
            "alignment. Raise max_comments_per_side if the model supports it.",
            len(actual), max_comments_per_side,
        )
    if len(ai) > max_comments_per_side:
        logger.warning(
            "AI review has %d comments; truncating to %d for batch alignment.",
            len(ai), max_comments_per_side,
        )
    actual_slice = actual[:max_comments_per_side]
    ai_slice = ai[:max_comments_per_side]

    if not actual_slice:
        return {"hits": [], "misses": [], "false_alarms": list(ai_slice),
                "n_actual": 0, "n_ai": len(ai_slice),
                "n_strengths": 0, "llm_comparison": None,
                "aligner": "batch-llm"}
    if not ai_slice:
        return {"hits": [], "misses": list(actual_slice), "false_alarms": [],
                "n_actual": len(actual_slice), "n_ai": 0,
                "n_strengths": 0, "llm_comparison": None,
                "aligner": "batch-llm"}

    human_ids = [str(c.get("id") or c.get("comment_id") or f"H{i+1}")
                 for i, c in enumerate(actual_slice)]
    ai_ids = [str(c.get("id") or c.get("comment_id") or f"A{j+1}")
              for j, c in enumerate(ai_slice)]
    human_block = _fmt_comments_for_prompt(actual_slice, "human")
    ai_block = _fmt_comments_for_prompt(ai_slice, "ai")

    # Use real IDs as the worked example so the LLM echoes the same
    # convention. With abstract placeholders like H1/A1, the model would
    # anchor on those instead of the real shape (e.g. "Reviewer_qFvT-C1"),
    # and the parser would skip every emitted line as an unknown ID —
    # yielding an all-zero matrix despite the raw response looking fine.
    example_hid = human_ids[0]
    example_aid = ai_ids[0]

    system_prompt = prompts.load("batch_alignment_system")
    user_msg = prompts.load(
        "batch_alignment_user",
        human_block=human_block,
        ai_block=ai_block,
        pair_count=len(human_ids) * len(ai_ids),
        example_human_id=example_hid,
        example_ai_id=example_aid,
    )

    total = len(actual_slice) * len(ai_slice)
    logger.info(
        "align_comments (batch LLM): one call for %d human × %d AI = "
        "%d pairs (model=%s)",
        len(actual_slice), len(ai_slice), total,
        getattr(llm_client, "model", "?"),
    )

    # max_tokens scaled to fit 120×120 ≈ 80k tokens of similarity lines,
    # capped at a value most providers honor.
    budget = min(32000, max(4000, total * 12))
    try:
        raw = llm_client.complete(system_prompt, user_msg, max_tokens=budget)
    except Exception as e:
        raise RuntimeError(
            f"Batch alignment LLM call failed ({type(e).__name__}: {e}). "
            f"Check provider config, API quota, and network."
        ) from e

    sims, n_parsed = _parse_batch_similarity_matrix(raw, human_ids, ai_ids)
    logger.info(
        "align_comments (batch LLM): parsed %d / %d similarity lines "
        "from the response (%d chars)",
        n_parsed, total, len(raw),
    )
    if n_parsed < total * 0.5:
        logger.warning(
            "Batch LLM returned only %d / %d parseable similarity lines — "
            "the model may have truncated its response. Consider reducing "
            "max_comments_per_side or using a model with a larger context.",
            n_parsed, total,
        )

    hits: List[Dict[str, Any]] = []
    misses: List[Dict[str, Any]] = []
    matched_ai_idx: set = set()

    for hi, h in enumerate(actual_slice):
        row = sims[hi]
        order = row.argsort()[::-1]
        top_idx = int(order[0])
        top_sim = float(row[top_idx])
        supporting = []
        for j in order[1:]:
            s = float(row[int(j)])
            if s < BATCH_PARTIAL_THR:
                break
            supporting.append({"ai": ai_slice[int(j)], "sim": s})

        if top_sim >= BATCH_SAME_THR:
            verdict = "same"
        elif top_sim >= BATCH_PARTIAL_THR:
            verdict = "partial"
        else:
            verdict = "missed"

        if verdict in ("same", "partial"):
            matched_ai_idx.add(top_idx)
            for sup in supporting:
                sid = sup["ai"].get("id") or sup["ai"].get("comment_id")
                for k2, aic in enumerate(ai_slice):
                    if (aic.get("id") or aic.get("comment_id")) == sid:
                        matched_ai_idx.add(k2)
            hits.append({
                "actual": h,
                "primary_ai": ai_slice[top_idx],
                "primary_sim": top_sim,
                "supporting_ai": supporting,
                "n_supporting_reviewers": 1 + len(supporting),
                "llm_verdict": verdict,
                "llm_rationale":
                    f"batch LLM similarity = {top_sim:.2f} "
                    f"({'≥' if verdict == 'same' else '≥'} "
                    f"{BATCH_SAME_THR if verdict == 'same' else BATCH_PARTIAL_THR:.2f})",
            })
        else:
            misses.append({
                **h,
                "best_sim": top_sim,
                "llm_verdict": "missed",
                "llm_rationale":
                    f"best batch LLM similarity = {top_sim:.2f} "
                    f"< {BATCH_PARTIAL_THR:.2f}",
            })

    false_alarms: List[Dict[str, Any]] = []
    for j, c in enumerate(ai_slice):
        if j not in matched_ai_idx:
            false_alarms.append(dict(c))

    if run_dir is not None:
        try:
            _write_batch_artifacts(
                run_dir, actual_slice, ai_slice, sims,
                raw, n_parsed, getattr(llm_client, "model", "unknown"),
                user_msg=user_msg,
            )
        except Exception as e:
            logger.warning("Failed to write batch alignment artifacts to %s "
                           "(%s: %s) — continuing without.",
                           run_dir, type(e).__name__, e)

    logger.info(
        "align_comments (batch LLM): %d hits, %d misses, %d false alarms "
        "(thresholds: same≥%.2f partial≥%.2f)",
        len(hits), len(misses), len(false_alarms),
        BATCH_SAME_THR, BATCH_PARTIAL_THR,
    )
    return {
        "hits": hits,
        "misses": misses,
        "false_alarms": false_alarms,
        "n_actual": len(actual_slice),
        "n_ai": len(ai_slice),
        "n_strengths": 0,
        "aligner": "batch-llm",
        "llm_comparison": {
            "summary": (f"Batch LLM similarity over {total} pairs: "
                        f"{len(hits)} hits, {len(misses)} misses, "
                        f"{len(false_alarms)} false alarms "
                        f"({n_parsed} / {total} pairs parsed from response)."),
            "matches": [], "missed": [], "extras": [],
            "raw": raw,
            "llm_model": getattr(llm_client, "model", "unknown"),
            "n_pairs_parsed": n_parsed,
            "n_pairs_total": total,
        },
    }


def _write_batch_artifacts(
    run_dir: Any,
    actual_slice: List[Dict[str, Any]],
    ai_slice: List[Dict[str, Any]],
    sims: Any,
    raw_response: str,
    n_parsed: int,
    llm_model: str,
    user_msg: str = "",
) -> None:
    """Write three markdown artifacts to ``run_dir`` for the user to audit
    the alignment step:

    ``alignment_llm_analysis.md``
        Verbatim LLM response (and the exact prompt sent), so this one
        file is a complete audit trail for the alignment step.

    ``alignment_similarities.md``
        N × M matrix (rows = human IDs, cols = AI IDs) parsed from the
        LLM response. Each row's best cell is **bolded**. Per-human
        verdict table follows.

    ``alignment_ranking.md``
        Human comments ranked by their best-match similarity, highest
        first.

    Provenance is not prepended to these debugging artifacts — the
    validation run's metadata banner lives only on
    ``validation_report.md``.
    """
    rd = pathlib.Path(run_dir)
    rd.mkdir(parents=True, exist_ok=True)

    def _h_id(h: Dict[str, Any]) -> str:
        return str(h.get("id") or h.get("comment_id") or "?")

    def _a_id(a: Dict[str, Any]) -> str:
        return str(a.get("id") or a.get("comment_id") or "?")

    total = len(actual_slice) * len(ai_slice)

    pct = 100.0 * n_parsed / max(1, total)
    if n_parsed == total:
        status = "✓"
    elif pct >= 80.0:
        status = "⚠ partial"
    elif n_parsed > 0:
        status = "⚠ degraded"
    else:
        status = "✗ PARSE FAILED — inspect the response below; the matrix is all zeros"

    analysis_lines = [
        "# Batch LLM alignment — raw analysis",
        "",
        f"**Model:** `{llm_model}`  ",
        f"**Shape:** {len(actual_slice)} human × {len(ai_slice)} AI = "
        f"{total} pairs  ",
        f"**Parsed:** {n_parsed} / {total} similarity lines "
        f"({pct:.0f}%) {status}",
        "",
        "This is the verbatim response from the LLM when asked to produce "
        "pairwise similarity scores for every (human, AI) comment pair. "
        "The similarity matrix in `alignment_similarities.md` is parsed "
        "from the 'Similarity scores' section below; the ranking file "
        "comes from the 'Ranked human comments' section.",
        "",
        "---",
        "",
        raw_response,
    ]
    if user_msg:
        analysis_lines += [
            "",
            "---",
            "",
            "<details>",
            "<summary>Prompt sent to the LLM</summary>",
            "",
            "```",
            user_msg,
            "```",
            "",
            "</details>",
        ]
    (rd / "alignment_llm_analysis.md").write_text("\n".join(analysis_lines))

    sim_lines = [
        "# Batch LLM similarity matrix",
        "",
        f"**Model:** `{llm_model}`  ",
        f"**Shape:** {len(actual_slice)} × {len(ai_slice)} = {total} pairs  ",
        f"**Thresholds:** same ≥ {BATCH_SAME_THR:.2f}, "
        f"partial ≥ {BATCH_PARTIAL_THR:.2f}",
        "",
        "Each cell is the similarity the LLM returned for that (human, AI) "
        "pair. The best match per row is **bolded**. Scores ≥ the `same` "
        "threshold become confirmed hits; ≥ `partial` become partial hits; "
        "below both, the human comment is a miss.",
        "",
    ]
    header = "| human \\\\ AI | " + " | ".join(_a_id(a) for a in ai_slice) + " | best |"
    sep = "|" + "---|" * (len(ai_slice) + 2)
    sim_lines.append(header)
    sim_lines.append(sep)
    for hi, h in enumerate(actual_slice):
        row = sims[hi]
        best_j = int(row.argmax()) if len(row) else -1
        cells = []
        for j in range(len(ai_slice)):
            val = f"{float(row[j]):.2f}"
            cells.append(f"**{val}**" if j == best_j else val)
        best_val = f"{float(row[best_j]):.2f}" if best_j >= 0 else "n/a"
        sim_lines.append("| " + _h_id(h) + " | " + " | ".join(cells)
                         + f" | {best_val} |")
    sim_lines += ["", "## Verdict per human comment", "",
                  "| human | best AI | sim | verdict |", "|---|---|---|---|"]
    for hi, h in enumerate(actual_slice):
        row = sims[hi]
        best_j = int(row.argmax()) if len(row) else -1
        best_sim = float(row[best_j]) if best_j >= 0 else 0.0
        if best_sim >= BATCH_SAME_THR:
            verdict = "same"
        elif best_sim >= BATCH_PARTIAL_THR:
            verdict = "partial"
        else:
            verdict = "missed"
        sim_lines.append(
            f"| {_h_id(h)} | "
            f"{_a_id(ai_slice[best_j]) if best_j >= 0 else 'n/a'} | "
            f"{best_sim:.2f} | {verdict} |"
        )
    (rd / "alignment_similarities.md").write_text("\n".join(sim_lines))

    rank_entries = []
    for hi, h in enumerate(actual_slice):
        row = sims[hi]
        best_j = int(row.argmax()) if len(row) else -1
        best_sim = float(row[best_j]) if best_j >= 0 else 0.0
        rank_entries.append((best_sim, h, ai_slice[best_j] if best_j >= 0 else None))
    rank_entries.sort(key=lambda t: -t[0])

    rank_lines = [
        "# Human comments ranked by best AI similarity",
        "",
        f"**Model:** `{llm_model}`  ",
        "",
        "Human comments sorted by their best-match similarity against any "
        "AI comment, highest first. Use this to see which human concerns "
        "the AI clearly caught versus barely touched versus missed.",
        "",
        "| rank | human | best AI | sim | verdict |",
        "|---|---|---|---|---|",
    ]
    for i, (sim, h, a) in enumerate(rank_entries, start=1):
        if sim >= BATCH_SAME_THR:
            verdict = "same"
        elif sim >= BATCH_PARTIAL_THR:
            verdict = "partial"
        else:
            verdict = "missed"
        rank_lines.append(
            f"| {i} | {_h_id(h)} | {_a_id(a) if a else 'n/a'} | "
            f"{sim:.2f} | {verdict} |"
        )
    (rd / "alignment_ranking.md").write_text("\n".join(rank_lines))

    logger.info("Wrote batch alignment artifacts: %s, %s, %s",
                rd / "alignment_llm_analysis.md",
                rd / "alignment_similarities.md",
                rd / "alignment_ranking.md")


def align_comments(
    actual: List[Dict[str, Any]],
    ai: List[Dict[str, Any]],
    llm_client: Any,
    max_comments_per_side: int = 120,
    run_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """Align human comments to AI comments via a single batch LLM call.

    Thin wrapper over :func:`align_comments_batch_llm`. There is no
    embedding fallback — if the LLM call fails or returns an unusable
    response, the error surfaces so the caller can retry with a
    different provider or fix the input.
    """
    return align_comments_batch_llm(
        actual, ai, llm_client,
        max_comments_per_side=max_comments_per_side,
        run_dir=run_dir,
    )
