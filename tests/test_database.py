"""Reviewer-database generator (`ai-paper-review-generate-db`)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from ai_paper_review.database.generation import generate, main
from ai_paper_review.review.reviewer_db import parse_reviewer_database

DB_DIR = Path(__file__).resolve().parent.parent / "src" / "ai_paper_review" / "database"


def _config(n_domains: int = 2, n_personas: int = 2) -> dict:
    personas = [
        {"name": f"Persona {i}", "slug": f"p{i}", "focus": "f", "style": "s",
         "priorities": ["q"], "common_concerns": "c"}
        for i in range(n_personas)
    ]
    return {
        "field": "test field",
        "domains": [
            {"id": f"D{i + 1}", "name": f"Domain {i}", "keywords": ["k1", "k2"]}
            for i in range(n_domains)
        ],
        "personas": personas,
        "validation_attribution": {
            "category_vocab": ["novelty"],
            "category_to_persona": {"novelty": "Persona 0"},
            "sub_rating_to_persona": {"soundness": "Persona 0"},
        },
    }


def _set(cfg: dict, path: str, value):
    *keys, last = path.split(".")
    node = cfg
    for k in keys:
        node = node[int(k)] if isinstance(node, list) else node[k]
    node[last] = value
    return cfg


def _drop(cfg: dict, path: str):
    *keys, last = path.split(".")
    node = cfg
    for k in keys:
        node = node[int(k)] if isinstance(node, list) else node[k]
    del node[last]
    return cfg


@pytest.mark.parametrize("yaml_text, key", [
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "domains", [])), "domains", id="domains-empty"),
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "domains", None)), "domains", id="domains-null"),
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "validation_attribution", None)),
                 "validation_attribution", id="attribution-null"),
    pytest.param(lambda: yaml.safe_dump(_drop(_config(), "personas.0.style")), "style", id="persona-no-style"),
    pytest.param(lambda: yaml.safe_dump(_drop(_config(), "domains.0.name")), "name", id="domain-no-name"),
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "domains.0.keywords", None)), "keywords",
                 id="keywords-null"),
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "personas.0.priorities", None)), "priorities",
                 id="priorities-null"),
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "domains.0.keywords", [2024, "x"])),
                 "domains[0].keywords[0]", id="keywords-int-item"),
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "personas.0.priorities", ["q", 3])),
                 "personas[0].priorities[1]", id="priorities-int-item"),
    pytest.param(lambda: yaml.safe_dump(_set(_config(), "field", 2024)), "field", id="field-int"),
    pytest.param(lambda: "field: [unclosed\n", None, id="malformed-yaml"),
    pytest.param(lambda: "just a string\n", "mapping", id="scalar-yaml"),
])
def test_malformed_config_exits_cleanly(tmp_path, capsys, yaml_text, key):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml_text())
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(cfg_path), "--out", str(tmp_path / "out.md")])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert err.startswith("Error:")
    assert "Traceback" not in err
    if key:
        assert key in err


def _tables(path: Path):
    t = parse_reviewer_database(path).tables
    return t.category_vocab, t.category_to_persona, t.sub_rating_to_persona


@pytest.mark.parametrize("stem", ["comparch", "mlai"])
def test_bundled_attribution_tables_reproducible(tmp_path, stem):
    cfg = yaml.safe_load((DB_DIR / f"{stem}_reviewer_cfg.yaml").read_text())
    out = tmp_path / "db.md"
    out.write_text(generate(cfg))
    assert _tables(out) == _tables(DB_DIR / f"{stem}_reviewer_db.md")


def test_attribution_values_round_trip_special_strings(tmp_path):
    cfg = _config()
    cfg["personas"][0]["name"] = "Critic: Methods"
    cfg["validation_attribution"] = {
        "category_vocab": ["yes", "null"],
        "category_to_persona": {"no": "Critic: Methods", "on": "yes"},
        "sub_rating_to_persona": {"soundness": "Critic: Methods"},
    }
    out = tmp_path / "db.md"
    out.write_text(generate(cfg))
    vocab, c2p, s2p = _tables(out)
    assert vocab == ["yes", "null"]
    assert c2p == {"no": "Critic: Methods", "on": "yes"}
    assert s2p == {"soundness": "Critic: Methods"}


def test_more_than_999_reviewers_rejected():
    with pytest.raises(ValueError, match="999"):
        generate(_config(n_domains=100, n_personas=10))
    generate(_config(n_domains=37, n_personas=27))  # 999 is fine


def test_title_keeps_acronyms():
    cfg = _config()
    cfg["field"] = "machine learning and AI"
    assert generate(cfg).startswith("# Machine Learning And AI Reviewer Database\n")


def test_mlai_heading_matches_bundled_db():
    cfg = yaml.safe_load((DB_DIR / "mlai_reviewer_cfg.yaml").read_text())
    bundled = (DB_DIR / "mlai_reviewer_db.md").read_text()
    assert generate(cfg).splitlines()[0] == bundled.splitlines()[0]


@pytest.mark.parametrize("n_domains, n_personas", [(1, 3), (2, 3), (3, 12)])
def test_section6_sample_id_matches_persona(tmp_path, n_domains, n_personas):
    md = generate(_config(n_domains, n_personas))
    section6 = md.split("## 6. Programmatic Access", 1)[1]
    rid = re.search(r'"id": "(R\d{3})"', section6).group(1)
    domain = re.search(r'"domain": "(.+?)"', section6).group(1)
    persona = re.search(r'"persona": "(.+?)"', section6).group(1)
    out = tmp_path / "db.md"
    out.write_text(md)
    by_id = {r.id: r for r in parse_reviewer_database(out).reviewers}
    assert rid in by_id
    assert (by_id[rid].domain, by_id[rid].persona) == (domain, persona)
