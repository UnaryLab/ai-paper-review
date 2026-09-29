"""Review pipeline: reviewer dispatch, clarity reviewer, graph state, ingestion."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import ai_paper_review
from ai_paper_review.llm.clients.base import FatalLLMError
from ai_paper_review.review import clarity as _clarity
from ai_paper_review.review import clustering as _clustering
from ai_paper_review.review import pdf_ingestion as _pdf
from ai_paper_review.review import review as _review
from ai_paper_review.review import reviewer_dispatching as _rd
from ai_paper_review.review.parsing import parse_review_markdown
from ai_paper_review.review.reviewer_db import Reviewer, parse_reviewer_db
from ai_paper_review.review.selection import select_reviewers


def _reviewer(rid: str = "R001") -> Reviewer:
    return Reviewer(
        id=rid, persona=f"P-{rid}", domain="Testing", focus="",
        style="", keywords=[], system_prompt="You are a test reviewer.",
    )


def _good_review(rid: str = "R001") -> str:
    return (
        "# Review\n\n## Comment 1\n"
        "- **Severity:** minor\n"
        f"- **Summary:** issue from {rid}\n"
        "- **Description:** details\n"
    )


class _FakeCfg:
    review_provider = "openai_compatible_api"
    max_retries = 0
    review_max_tokens = 16000

    def __init__(self, delay: float = 0.0, max_concurrent: int = 4):
        self._delay = delay
        self.max_concurrent = max_concurrent

    def set_review_llm(self, provider, model):
        pass

    def resolve_base_url_for_stage(self, stage):
        return None

    def request_delay_for(self, provider):
        return self._delay


class _NoopLLM:
    model = "fake"

    def complete(self, system, user, max_tokens=4000, pdf_path=None):
        raise AssertionError("not called when _run_single_reviewer is stubbed")


def _patch_dispatch(monkeypatch, llm=None, delay=0.0, max_concurrent=4,
                    run_single=None):
    llm = llm or _NoopLLM()
    monkeypatch.setattr(_rd, "load_config",
                        lambda: _FakeCfg(delay, max_concurrent))
    monkeypatch.setattr(_rd, "make_client", lambda cfg, use_case=None: llm)
    monkeypatch.setattr(_rd, "provider_supports_pdf", lambda p, u: False)
    if run_single is not None:
        monkeypatch.setattr(_rd, "_run_single_reviewer", run_single)
    return llm


def _state(n: int):
    return {
        "selected": [(_reviewer(f"R{i + 1:03d}"), 1.0) for i in range(n)],
        "paper": {"title": "T", "abstract": "A", "full_text": "F"},
    }


def _review_dict(r, comments):
    return {"_reviewer_id": r.id, "_persona": r.persona,
            "_domain": r.domain, "comments": comments}


# ---------------------------------------------------------------------------
# blank comments and all-empty runs
# ---------------------------------------------------------------------------

def test_run_reviewers_drops_blank_comments(monkeypatch):
    def run_single(r, paper, llm, pdf_path=None, max_tokens=None):
        if r.id == "R001":
            return _review_dict(r, [{"summary": "", "description": ""}])
        return _review_dict(r, [{"summary": "real", "description": "d"}])

    _patch_dispatch(monkeypatch, run_single=run_single)
    out = _rd.node_run_reviewers(_state(2))
    assert [c["summary"] for c in out["all_comments"]] == ["real"]


def test_run_reviewers_raises_when_no_comment_left(monkeypatch):
    def run_single(r, paper, llm, pdf_path=None, max_tokens=None):
        return _review_dict(r, [{"summary": "", "description": ""}])

    _patch_dispatch(monkeypatch, run_single=run_single)
    with pytest.raises(RuntimeError, match="No usable comments"):
        _rd.node_run_reviewers(_state(3))


# ---------------------------------------------------------------------------
# comment order follows selection order, not completion order
# ---------------------------------------------------------------------------

def test_run_reviewers_orders_comments_by_selection(monkeypatch):
    def run_single(r, paper, llm, pdf_path=None, max_tokens=None):
        if r.id == "R001":
            time.sleep(0.3)  # finishes last
        return _review_dict(r, [{"summary": r.id, "description": "d"}])

    _patch_dispatch(monkeypatch, max_concurrent=3, run_single=run_single)
    out = _rd.node_run_reviewers(_state(3))
    assert [c["_reviewer_id"] for c in out["all_comments"]] == \
        ["R001", "R002", "R003"]
    assert [rv["_reviewer_id"] for rv in out["raw_reviews"]] == \
        ["R001", "R002", "R003"]


# ---------------------------------------------------------------------------
# a fatal error stops dispatch of the remaining reviewers
# ---------------------------------------------------------------------------

def test_run_reviewers_stops_dispatch_on_fatal_error(monkeypatch):
    calls = []

    def run_single(r, paper, llm, pdf_path=None, max_tokens=None):
        calls.append(r.id)
        if r.id == "R001":
            raise FatalLLMError("usage limit")
        return _review_dict(r, [{"summary": "s", "description": "d"}])

    _patch_dispatch(monkeypatch, delay=0.1, max_concurrent=6,
                    run_single=run_single)
    with pytest.raises(FatalLLMError):
        _rd.node_run_reviewers(_state(6))
    assert len(calls) <= 2


# ---------------------------------------------------------------------------
# uploaded files are cleaned up after the reviewer pool
# ---------------------------------------------------------------------------

class _CleanupLLM(_NoopLLM):
    def __init__(self):
        self.cleanups = 0

    def cleanup_uploaded_files(self):
        self.cleanups += 1


def test_run_reviewers_cleans_up_uploaded_files(monkeypatch):
    def run_single(r, paper, llm, pdf_path=None, max_tokens=None):
        return _review_dict(r, [{"summary": "s", "description": "d"}])

    llm = _patch_dispatch(monkeypatch, llm=_CleanupLLM(), run_single=run_single)
    _rd.node_run_reviewers(_state(2))
    assert llm.cleanups == 1


def test_run_reviewers_cleans_up_on_fatal_error(monkeypatch):
    def run_single(r, paper, llm, pdf_path=None, max_tokens=None):
        raise FatalLLMError("auth failed")

    llm = _patch_dispatch(monkeypatch, llm=_CleanupLLM(), run_single=run_single)
    with pytest.raises(FatalLLMError):
        _rd.node_run_reviewers(_state(2))
    assert llm.cleanups == 1


def test_clarity_node_cleans_up_on_success_and_error(monkeypatch):
    llm = _CleanupLLM()
    monkeypatch.setattr(_clarity, "load_config", lambda: _FakeCfg())
    monkeypatch.setattr(_clarity, "make_client", lambda cfg, use_case=None: llm)
    monkeypatch.setattr(_clarity, "provider_supports_pdf", lambda p, u: True)
    monkeypatch.setattr(_clarity, "run_clarity_review",
                        lambda paper, llm, pdf_path=None, max_tokens=None: {"comments": []})
    _clarity.node_run_clarity_review({"paper": {}, "pdf_path": "p.pdf"})
    assert llm.cleanups == 1

    def fail(paper, llm, pdf_path=None, max_tokens=None):
        raise FatalLLMError("auth failed")

    monkeypatch.setattr(_clarity, "run_clarity_review", fail)
    with pytest.raises(FatalLLMError):
        _clarity.node_run_clarity_review({"paper": {}, "pdf_path": "p.pdf"})
    assert llm.cleanups == 2


# ---------------------------------------------------------------------------
# markdown repair sees the whole raw output and names the fields
# ---------------------------------------------------------------------------

_FIELD_LABELS = ["Severity", "Category", "Section Reference",
                 "Summary", "Description", "Keywords"]


class _RepairLLM:
    """First call returns long unparseable text; the repair call returns a
    valid review. Records the repair prompt."""
    model = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens=4000, pdf_path=None):
        self.calls.append(user)
        if len(self.calls) == 1:
            return "x" * 9000 + " TAIL-MARKER"
        return _good_review()


def test_reviewer_repair_prompt_keeps_full_output_and_labels():
    llm = _RepairLLM()
    out = _rd._call_and_parse(_reviewer(), "u", llm)
    assert out["_format_repaired"] is True
    repair = llm.calls[1]
    assert "TAIL-MARKER" in repair
    for label in _FIELD_LABELS:
        assert label in repair


def test_clarity_repair_prompt_keeps_full_output():
    llm = _RepairLLM()
    out = _clarity._call_and_parse("u", llm)
    assert out["_format_repaired"] is True
    assert "TAIL-MARKER" in llm.calls[1]


# ---------------------------------------------------------------------------
# reviewer calls leave room for reasoning tokens
# ---------------------------------------------------------------------------

def _reasoning_openai_client():
    """Real OpenAIClient over a fake SDK that spends 6000 reasoning
    tokens before writing ~1000 tokens of review text."""
    from ai_paper_review.llm.clients.openai import OpenAIClient

    def create(**kw):
        budget = kw.get("max_tokens") or kw.get("max_completion_tokens")
        ok = budget >= 6000 + 1000
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop" if ok else "length",
            message=SimpleNamespace(content=_good_review() if ok else "",
                                    refusal=None),
        )])

    client = object.__new__(OpenAIClient)
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client._base_url = None
    client._provider = "openai_compatible_api"
    client.model = "reasoning-test"
    return client


def test_reviewer_budget_covers_reasoning_tokens():
    paper = {"title": "T", "abstract": "A", "full_text": "F"}
    out = _rd._run_single_reviewer(_reviewer(), paper,
                                   _reasoning_openai_client())
    assert "error" not in out
    assert out["comments"][0]["summary"] == "issue from R001"


def test_clarity_budget_covers_reasoning_tokens():
    paper = {"title": "T", "abstract": "A", "full_text": "F"}
    out = _clarity.run_clarity_review(paper, llm=_reasoning_openai_client())
    assert out["comments"][0]["summary"] == "issue from R001"


# ---------------------------------------------------------------------------
# LangGraph keeps the provenance counters
# ---------------------------------------------------------------------------

def test_graph_keeps_format_repair_counters(monkeypatch):
    def passthrough(state):
        return state

    def format_report(state):
        state["report_md"] = "r"
        state["n_format_repairs"] = 2
        state["n_reviewers_total"] = 3
        return state

    for name in ("node_ingest_pdf", "node_load_db", "node_select_reviewers",
                 "node_run_reviewers", "node_run_clarity_review",
                 "node_cluster_comments", "node_rank_clusters"):
        monkeypatch.setattr(_review, name, passthrough)
    monkeypatch.setattr(_review, "node_format_report", format_report)

    graph = _review.build_graph()
    assert graph is not None
    final = graph.invoke({"pdf_path": "p.pdf"})
    assert final.get("n_format_repairs") == 2
    assert final.get("n_reviewers_total") == 3


# ---------------------------------------------------------------------------
# image-only PDF stops at ingestion
# ---------------------------------------------------------------------------

def test_ingest_rejects_image_only_pdf(monkeypatch):
    from ai_paper_review.llm import config as _config
    from ai_paper_review.llm import factory as _factory

    summaries = []
    monkeypatch.setattr(_config, "load_config", lambda: _FakeCfg())
    monkeypatch.setattr(_factory, "make_client",
                        lambda cfg, use_case=None: _NoopLLM())
    monkeypatch.setattr(_review, "extract_pdf_for_provider",
                        lambda path, provider: "  \n Page 1 \n ")
    monkeypatch.setattr(
        _review, "extract_paper_summary_llm",
        lambda text, llm: summaries.append(text) or {"title": "", "abstract": ""},
    )
    with pytest.raises(ValueError, match="image-only"):
        _review.node_ingest_pdf({"pdf_path": "scan.pdf"})
    assert summaries == []


def test_comment_field_empty_value_does_not_take_next_line():
    text = (
        "# Review\n\n## Comment 1\n"
        "- **Severity:** major\n"
        "- **Summary:**\n"
        "- **Description:** Real description here.\n"
        "\n## Comment 2\n"
        "- **Severity:**\n"
        "- **Summary:**\n"
        "- **Description:**\n"
        "- **Keywords:**\n"
    )
    c1, c2 = parse_review_markdown(text)["comments"]
    assert c1["description"] == "Real description here."
    assert c1["summary"] == "Real description here."  # derived, not the next line
    assert c2["summary"] == "" and c2["description"] == ""
    assert c2["keywords"] == []


def test_bundled_db_system_prompts_keep_template_and_rules():
    db_dir = Path(ai_paper_review.__file__).parent / "database"
    for name in ("comparch_reviewer_db.md", "mlai_reviewer_db.md"):
        reviewers = parse_reviewer_db(db_dir / name)
        assert len(reviewers) == 200, name
        for r in reviewers:
            sp = r.system_prompt
            assert "**Reviewer ID:** " + r.id in sp, (name, r.id)
            assert "## Rules" in sp, (name, r.id)
            assert sp.endswith("No commentary or explanation before or after."), (name, r.id)
            assert "####" not in sp and "\n---" not in sp, (name, r.id)
            assert "```yaml" not in sp and "```python" not in sp, (name, r.id)
        assert reviewers[-1].id == "R200"


def _sel_reviewer(rid, domain):
    return Reviewer(id=rid, persona=f"P-{rid}", domain=domain, focus="",
                    style="", keywords=[], system_prompt="x")


def test_domain_bleed_soft_cap_picks_close_cross_domain_candidate():
    a = [_sel_reviewer(f"A{i}", "A") for i in range(1, 5)]
    b = _sel_reviewer("B1", "B")
    # k=4, bleed=0.25 -> domain cap 3; B1 is within 0.25 of A4.
    sims = list(zip(a, [0.90, 0.89, 0.88, 0.87])) + [(b, 0.80)]
    got = select_reviewers({}, a + [b], k=4, domain_bleed=0.25, selection_similarities=sims)
    assert [r.id for r, _ in got] == ["A1", "A2", "A3", "B1"]
    # B1 too far below A4: the in-domain candidate wins.
    sims = list(zip(a, [0.90, 0.89, 0.88, 0.87])) + [(b, 0.50)]
    got = select_reviewers({}, a + [b], k=4, domain_bleed=0.25, selection_similarities=sims)
    assert [r.id for r, _ in got] == ["A1", "A2", "A3", "A4"]


def test_clustering_similarities_md_describes_first_comment_rule():
    md = _clustering.format_clustering_similarities_md(
        {"title": "T"},
        {"threshold": 0.55, "backend": "tfidf", "labels": ["R1.c1", "R2.c1"],
         "matrix": [[1.0, 0.6], [0.6, 1.0]], "cluster_of": [0, 0]},
    )
    assert "single-link" not in md and "chain" not in md
    assert "first comment" in md


def test_clustering_empty_tfidf_vocab_gives_singleton_clusters(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # force TF-IDF
    monkeypatch.delenv("CLUSTER_THRESHOLD", raising=False)
    comments = [
        {"_reviewer_id": "R001", "summary": "", "description": "", "keywords": []},
        {"_reviewer_id": "R002", "summary": "the", "description": "", "keywords": []},
    ]
    state = _clustering.node_cluster_comments({"all_comments": comments})
    assert len(state["clusters"]) == 2


def test_clustering_bad_threshold_env_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("CLUSTER_THRESHOLD", "not-a-number")
    with caplog.at_level(logging.WARNING, logger="review_system"):
        state = _clustering.node_cluster_comments({"all_comments": []})
    assert state["clustering_similarities"]["threshold"] == 0.55
    assert "CLUSTER_THRESHOLD" in caplog.text


def test_review_header_confidence_and_default_reviewer_id():
    text = (
        "# Review\n\n**Confidence:** 4/5\n\n## Comment 1\n"
        "- **Summary:** s\n- **Description:** d\n"
        "**Persona:** leaked from a comment\n"
    )
    parsed = parse_review_markdown(text)
    assert parsed["confidence"] == 4
    assert parsed["persona"] == ""
    assert parsed["reviewer_id"] == ""
    assert parsed["comments"][0]["comment_id"] == "R000-C1"


def test_llm_summary_parses_bold_labels_and_next_line_abstract():
    raw = "**Title:** My Paper\n**Abstract:**\nFirst line.\nSecond line."
    llm = SimpleNamespace(complete=lambda system, user, max_tokens=0: raw)
    got = _pdf.extract_paper_summary_llm("paper body " * 50, llm)
    assert got["title"] == "My Paper"
    assert got["abstract"] == "First line. Second line."


def test_heuristic_abstract_starts_after_title_position():
    text = "1\n2\n3\nA Great Paper Title Here\nSome body text follows the title line."
    got = _pdf.extract_paper_summary(text)
    assert got["title"] == "A Great Paper Title Here"
    assert got["abstract"].startswith("Some body text")


def test_db_prompt_fence_followed_by_note_line_still_parses(tmp_path):
    db = tmp_path / "db.md"
    db.write_text(
        "#### R001 — Tester\n"
        "- **Domain:** Testing\n"
        "- **System Prompt:**\n\n"
        "```text\nYou are a tester.\n```markdown\n# Review\n```\nEnd.\n```\n"
        "Note: hand-written comment after the prompt.\n"
    )
    (r,) = parse_reviewer_db(db)
    assert r.system_prompt == "You are a tester.\n```markdown\n# Review\n```\nEnd."


_NESTED_PROMPT = (
    "You are a tester.\n"
    "```markdown\n## Comment 1\n- **Summary:**\n```\n"
    "## Rules\nOutput the review only."
)
_NESTED_REVIEWER = (
    "- **Domain:** Testing\n"
    "- **System Prompt:**\n\n"
    "```text\n" + _NESTED_PROMPT + "\n```\n"
    "Note: reviewed by hand\n"
)


def test_db_prompt_with_rules_after_nested_fence_and_note_line(tmp_path):
    db = tmp_path / "db.md"
    db.write_text("#### R001 — Tester\n" + _NESTED_REVIEWER)
    (r,) = parse_reviewer_db(db)
    assert r.system_prompt == _NESTED_PROMPT


def test_db_last_reviewer_prompt_stops_before_trailing_sections(tmp_path):
    db = tmp_path / "db.md"
    db.write_text(
        "#### R001 — First\n" + _NESTED_REVIEWER + "\n---\n\n"
        "#### R002 — Last\n" + _NESTED_REVIEWER + "\n---\n\n"
        "## 6. Sample\n\n```python\nprint('sample')\n```\n\n"
        "## 7. Validation Attribution Tables\n\n"
        "```yaml\ncategory_vocab: [clarity]\n```\n"
    )
    first, last = parse_reviewer_db(db)
    assert first.system_prompt == _NESTED_PROMPT
    assert last.system_prompt == _NESTED_PROMPT


def test_db_last_reviewer_with_unclosed_fence_stops_at_rule_line(tmp_path):
    db = tmp_path / "db.md"
    db.write_text(
        "#### R001 — First\n" + _NESTED_REVIEWER + "\n---\n\n"
        "#### R002 — Last\n"
        "- **Domain:** Testing\n"
        "- **System Prompt:**\n\n"
        "```text\nYou are a tester.\n```markdown\n# Review\n```\n"
        "\n---\n\n"
        "## 6. Sample\n\n```python\nprint('sample')\n```\n\n"
        "## 7. Validation Attribution Tables\n\n"
        "```yaml\ncategory_vocab: [clarity]\n```\n"
    )
    _first, last = parse_reviewer_db(db)
    assert "## 6" not in last.system_prompt
    assert "## 7" not in last.system_prompt


_TRAILING_SECTIONS = "\n---\n\n## 6. Sample\n\n```python\nprint('sample')\n```\n"


def test_db_prompt_keeps_rule_line_after_nested_fence(tmp_path):
    prompt = (
        "You are a tester.\n"
        "```markdown\n# Review\n```\n"
        "---\n"
        "## Rules\nOutput the review only."
    )
    db = tmp_path / "db.md"
    db.write_text(
        "#### R001 — Tester\n"
        "- **Domain:** Testing\n"
        "- **System Prompt:**\n\n"
        "```text\n" + prompt + "\n```\n" + _TRAILING_SECTIONS
    )
    (r,) = parse_reviewer_db(db)
    assert r.system_prompt == prompt
    assert r.domain == "Testing"


def test_db_prompt_keeps_rule_line_without_nested_fence(tmp_path):
    prompt = "You are a tester.\n---\n## Rules\nOutput the review only."
    db = tmp_path / "db.md"
    db.write_text(
        "#### R001 — Tester\n"
        "- **Domain:** Testing\n"
        "- **System Prompt:**\n\n"
        "```text\n" + prompt + "\n```\n" + _TRAILING_SECTIONS
    )
    (r,) = parse_reviewer_db(db)
    assert r.system_prompt == prompt
    assert r.domain == "Testing"


def test_review_nodes_use_submit_time_llm_config(monkeypatch):
    from ai_paper_review.llm.config import LLMConfig

    cfg = LLMConfig(review_provider="openai_compatible_api", review_model="m",
                    review_base_url="http://submit:8000/v1", max_concurrent=1)

    def no_load():
        raise AssertionError("load_config called despite state['llm_config']")

    seen = []

    def make(c, use_case=None):
        seen.append((c, c.resolve_base_url_for_stage("review")))
        return _NoopLLM()

    monkeypatch.setattr(_rd, "load_config", no_load)
    monkeypatch.setattr(_rd, "make_client", make)
    monkeypatch.setattr(_rd, "_run_single_reviewer",
                        lambda r, paper, llm, pdf_path=None, max_tokens=None: _review_dict(r, []))
    monkeypatch.setattr(_clarity, "load_config", no_load)
    monkeypatch.setattr(_clarity, "make_client", make)
    monkeypatch.setattr(_clarity, "run_clarity_review",
                        lambda paper, llm, pdf_path=None, max_tokens=None: {"comments": []})
    state = dict(_state(1), llm_config=cfg,
                 llm_provider="openai_compatible_api", llm_model="m")
    with pytest.raises(RuntimeError):  # every reviewer returned no comments
        _rd.node_run_reviewers(state)
    _clarity.node_run_clarity_review(state)
    assert seen == [(cfg, "http://submit:8000/v1")] * 2
    assert "llm_config" in _review.ReviewState.__annotations__


def test_ingest_pdf_uses_submit_time_llm_config(monkeypatch):
    from ai_paper_review.llm import config as _llm_config
    from ai_paper_review.llm import factory as _llm_factory
    from ai_paper_review.llm.config import LLMConfig

    cfg = LLMConfig(review_provider="openai_compatible_api", review_model="m",
                    review_base_url="http://submit:8000/v1", max_concurrent=1)

    def no_load():
        raise AssertionError("load_config called despite state['llm_config']")

    seen = []

    def make(c, use_case=None):
        seen.append((c, c.resolve_base_url_for_stage("review")))
        return _NoopLLM()

    monkeypatch.setattr(_llm_config, "load_config", no_load)
    monkeypatch.setattr(_llm_factory, "make_client", make)
    monkeypatch.setattr(_review, "extract_pdf_for_provider",
                        lambda pdf_path, provider: "paper body " * 100)
    monkeypatch.setattr(_review, "extract_paper_summary_llm",
                        lambda text, llm: {"title": "T", "abstract": "A"})
    state = {"pdf_path": "paper.pdf", "llm_config": cfg,
             "llm_provider": "openai_compatible_api", "llm_model": "m"}
    assert _review.node_ingest_pdf(state)["paper"]["title"] == "T"
    assert seen == [(cfg, "http://submit:8000/v1")]


def test_report_abstract_ellipsis_only_when_cut():
    from ai_paper_review.review.ranking import node_format_report

    def abstract_line(abstract):
        state = {"paper": {"title": "T", "abstract": abstract},
                 "ranked": [], "selected": [], "raw_reviews": []}
        md = node_format_report(state)["report_md"]
        return next(l for l in md.splitlines() if l.startswith("**Abstract:**"))

    assert abstract_line("short abstract") == "**Abstract:** short abstract"
    assert abstract_line("a" * 900) == "**Abstract:** " + "a" * 800 + "..."


def test_cli_review_data_has_paper_id(monkeypatch, tmp_path):
    from ai_paper_review import provenance as _prov
    from ai_paper_review.llm import config as _llm_config
    from ai_paper_review.llm.config import LLMConfig

    pdf = tmp_path / "paper.pdf"
    final = {"paper": {"title": "T", "abstract": "A"}, "report_md": "r",
             "selected": [], "raw_reviews": [], "ranked": []}
    monkeypatch.setattr(_review, "build_graph", lambda: None)
    monkeypatch.setattr(_review, "run_linear", lambda initial: final)
    monkeypatch.setattr(_prov, "now_iso", lambda: "2026-04-30T10:30:00+00:00")
    monkeypatch.setattr(_llm_config, "load_config", lambda: LLMConfig(
        review_provider="openai_api", review_model="org/gpt-x"))
    monkeypatch.setattr(sys, "argv", ["paper-review", "--pdf", str(pdf)])
    _review.main()
    data = (tmp_path / "paper_review_data.md").read_text().splitlines()
    assert data[2] == "**Paper ID:** paper-openai_api-org-gpt-x-20260430-103000"
    assert data[3] == "**Title:** T"
