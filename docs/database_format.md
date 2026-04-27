# Reviewer Database Format

This document specifies the two file formats involved in a reviewer database:

1. **YAML config** — a compact description of a field's sub-domains and reviewing personas. Easy to edit by hand or produce from a script.
2. **Reviewer-database markdown** — the runtime artifact the paper-review pipeline parses. Verbose but self-contained: each reviewer is a full LLM system prompt.

The project ships a complete example of each for the default Computer Architecture database:

- `src/ai_paper_review/data/comparch_reviewer_cfg.yaml` — the YAML source
- `src/ai_paper_review/data/comparch_reviewer_db.md` — the runtime artifact

**This project does not include tooling to convert one to the other.** The YAML is provided as a compact template for hand-editing; the markdown is what the runtime consumes. If you want both in sync for a field other than Computer Architecture, you write the conversion yourself (a few dozen lines of Python that template-fills the markdown from the YAML data).

---

## 1. YAML config format

A reviewer-config YAML describes a single field: what sub-domains partition it, what reviewing personas apply, and what keywords/venues each sub-domain is known for.

### Top-level keys

| Key                      | Type    | Required | Description                                                              |
|--------------------------|---------|----------|--------------------------------------------------------------------------|
| `field`                  | string  | yes      | Umbrella discipline name. Appears in every reviewer's system prompt.     |
| `version`                | string  | yes      | Version stamp written into the reviewer-database markdown header.        |
| `domains`                | list    | yes      | Sub-domains that partition the field. Each is a mapping (see below).     |
| `personas`               | list    | yes      | Reviewing personas. Replicated in every domain. Each is a mapping (see below). |
| `validation_attribution` | mapping | no       | Category vocab + category/sub-rating → persona routing maps used by the validation calibration step. Required if the DB will be validated against human reviews; see "Validation attribution entry" below. |

### Domain entry

Each item in the `domains:` list is a mapping with these keys:

| Key           | Type   | Required | Description                                                                  |
|---------------|--------|----------|------------------------------------------------------------------------------|
| `id`          | string | yes      | Short identifier, e.g. `"D1"`, `"D2"`, … Used to group reviewer IDs.         |
| `name`        | string | yes      | Human-readable sub-domain name, e.g. `"AI/ML Systems"`.                      |
| `short`       | string | yes      | Lowercase slug, e.g. `"ai_ml_systems"`. No spaces or special characters.     |
| `description` | string | yes      | One-sentence summary of what the sub-domain covers.                          |
| `keywords`    | list   | yes      | 20–30 technical terms, tools, methods, or datasets used in papers in this sub-area. Used by the reviewer selector to match papers. |
| `venues`      | string | yes      | Comma-separated list of 5–8 conferences or journals where papers in this sub-area appear. |

### Persona entry

Each item in the `personas:` list is a mapping with these keys:

| Key               | Type   | Required | Description                                                                  |
|-------------------|--------|----------|------------------------------------------------------------------------------|
| `name`            | string | yes      | Short display name, e.g. `"Novelty Hunter"`.                                 |
| `slug`            | string | yes      | Lowercase identifier, e.g. `"novelty"`.                                      |
| `focus`           | string | yes      | One-line focus statement: what this reviewer emphasizes.                     |
| `style`           | string | yes      | One-sentence characterization of how this reviewer approaches a paper.       |
| `priorities`      | list   | yes      | 2–5 bullet points phrased as questions the reviewer asks of every paper.     |
| `common_concerns` | string | yes      | One sentence summarizing the typical issues this persona flags.              |

### Validation attribution entry

`validation_attribution:` is a single mapping with three keys. Every persona name on the right-hand side of either map MUST match a `name:` from the `personas:` list above — otherwise the validation step will attribute missed human comments to personas that don't exist in this DB.

| Key                     | Type                | Required | Description                                                                 |
|-------------------------|---------------------|----------|-----------------------------------------------------------------------------|
| `category_vocab`        | list[str]           | yes      | Closed list of category strings the LLM human-review conversion step is told to pick from. Unknown categories are blanked out at parse time. |
| `category_to_persona`   | dict[str → str]     | yes      | Lowercase category (or near-miss keyword) → persona name. The router substring-matches any key inside an incoming category string, so a few extra fuzzy keys per persona (e.g. `baseline: Methodology Critic` alongside `methodology: Methodology Critic`) catch LLM drift cheaply. |
| `sub_rating_to_persona` | dict[str → str]     | yes      | Lowercase sub-rating name (OpenReview-style Soundness / Presentation / Contribution / …) → persona name. |

Minimal fragment:

```yaml
validation_attribution:
  category_vocab:
    - novelty
    - methodology
    - evaluation
    - reproducibility

  category_to_persona:
    novelty:          Novelty Hunter
    originality:      Novelty Hunter
    methodology:      Methodology Critic
    evaluation:       Empirical Evaluator
    reproducibility:  Reproducibility Champion

  sub_rating_to_persona:
    soundness:     Methodology Critic
    presentation:  Clarity & Presentation Editor
    contribution:  Novelty Hunter
```

See §7 of the bundled `comparch_reviewer_db.md` for the full 20-persona example.

### Minimal example

```yaml
field: "molecular biology"
version: "1.0"

domains:
  - id: D1
    name: Structural Biology
    short: struct
    description: Protein folding, cryo-EM, X-ray crystallography.
    keywords:
      - protein
      - structure
      - folding
      - cryo-EM
      - crystallography
      - refinement
      - resolution
      - Coot
      - Phenix
      # ... 20-30 entries recommended
    venues: Nature, Cell, PNAS, Structure, Acta Cryst D

  - id: D2
    name: Genomics
    short: genomics
    description: Sequencing, variant calling, and pipeline bioinformatics.
    keywords:
      - NGS
      - variant calling
      - alignment
      - BWA
      - GATK
      - VCF
      # ...
    venues: Genome Biology, Bioinformatics, Nature Genetics

personas:
  - name: Novelty Hunter
    slug: novelty
    focus: Novelty, originality, and delta over prior art
    style: Skeptical; demands crisp articulation of what is genuinely new.
    priorities:
      - Is the core idea actually new or a reskinning of prior work?
      - Are the claimed contributions explicit and verifiable?
      - Is the 'delta' over the closest 2-3 prior works quantified?
    common_concerns: Incremental contribution; missing prior-art comparison.

  - name: Methodology Critic
    slug: methodology
    focus: Experimental design, controls, statistical rigor
    style: Precise; will flag any uncontrolled comparison.
    priorities:
      - Are controls appropriate and complete?
      - Is the sample size justified?
      - Are statistical tests correctly applied?
    common_concerns: Underpowered experiments; missing controls.
```

See `src/ai_paper_review/data/comparch_reviewer_cfg.yaml` for a full, production-scale example (10 domains × 20 personas, all fields populated).

### Reviewer count

The runtime reviewer count is `len(domains) × len(personas)`. With 10 domains and 20 personas you get 200 reviewers. There's no hard limit on database size; the selector picks the top N by similarity for each paper, where N is chosen by the user per run (default 10, recommended range 5–10 to balance coverage against cost, hard range 1–20). Database size mostly affects how well-targeted the selection is — a larger DB means more candidates to diversify across, not more reviewers emitted per run.

### Design guidance

- **Keep domain keywords specific.** The selector uses these to match a paper's extracted keywords against each reviewer's domain. Generic terms (e.g. "research", "analysis") dilute the matching; specific technical terms (tools, methods, algorithms, datasets) make it work well.
- **Personas should be field-agnostic.** The 20 bundled personas (Novelty Hunter, Methodology Critic, Reproducibility Champion, Deployment Veteran, etc.) work for almost any discipline. You can reuse them verbatim for a new field and only replace `domains:`.
- **Priorities should be questions, not statements.** Each priority is prepended with "You ask:" in the generated system prompt, so phrase them as things the reviewer checks.

---

## 2. Reviewer-database markdown format

The markdown file is what the pipeline actually parses at runtime. It's organized into seven numbered sections plus per-reviewer entries. The parser cares about three things:

1. The structured per-reviewer blocks in **Section 5: Reviewer Entries**.
2. The fenced `text` code block after `- **System Prompt:**` in each reviewer.
3. The fenced `yaml` code block in **Section 7: Validation Attribution Tables** (consumed by the validation pipeline — category vocab and category/sub-rating → persona routing).

Sections 1–4 and 6 are for human readers only; you can omit or reword them freely. Section 7 is required when the DB will be used for validation; if it's omitted entirely the review pipeline still works and the validation pipeline just skips attribution with a warning.

### Overall file layout

````
# <Field-Titlecased> Paper Review System — Reviewer Database

**Version:** <version string>
**Total Reviewers:** <N>
**Domains:** <n_domains> × **Personas per Domain:** <n_personas>

---

## 1. Overview
<prose; free form>

---

## 2. Standard Reviewer Template
<prose + a fenced code block showing the template; free form>

---

## 3. Domain Index

| # | Domain | Reviewer ID Range | Keywords (short) |
|---|---|---|---|
| 1 | AI/ML Systems | R001–R020 | neural networks, deep learning, ... |
...

---

## 4. Persona Index

| # | Persona | Focus |
|---|---|---|
| 1 | Novelty Hunter | Novelty, originality, and delta over prior art |
...

---

## 5. Reviewer Entries

<-- EACH DOMAIN IS A SECTION -->

---

### Domain D1: <Name>

> <description>

**Canonical keywords:** <kw1>, <kw2>, ...

**Typical venues:** <venue1>, <venue2>, ...

<-- EACH REVIEWER IS A BLOCK -->

#### R001 — <Persona Name>

- **Domain:** <Name>
- **Persona:** <Persona Name>
- **Focus:** <focus line>
- **Review Style:** <style line>
- **Keywords:** <kw1>, <kw2>, ...
- **System Prompt:**

```text
You are **Reviewer R001**, an expert peer reviewer for <field> research, ...
<multi-line system prompt>
```

#### R002 — <Next Persona Name>
...

---

## 6. Programmatic Access
<prose describing how the file is parsed; free form>

---

## 7. Validation Attribution Tables
<prose; same YAML block shape as the bundled DB — see below>

```yaml
category_vocab:
  - <short category string>
  - ...

category_to_persona:
  <category or keyword>: <persona name matching a reviewer entry>
  ...

sub_rating_to_persona:
  <sub-rating name>: <persona name matching a reviewer entry>
  ...
```
````

### The reviewer block — parser-relevant detail

The parser matches on `####` headings that look like `R###` (three-digit ID) followed by an em-dash and a persona name:

```
#### R042 — Reproducibility Champion
```

Inside each block it extracts these fields, matching on the `- **Label:**` prefix at the start of a line:

| Line prefix          | Field name on the parsed record | Type  |
|----------------------|---------------------------------|-------|
| `- **Domain:**`      | `domain`                        | str   |
| `- **Persona:**`     | `persona`                       | str   |
| `- **Focus:**`       | `focus`                         | str   |
| `- **Review Style:**`| `style`                         | str   |
| `- **Keywords:**`    | `keywords`                      | list[str] — comma-separated |
| `- **System Prompt:**` | `system_prompt`               | str — contents of the next ```text…``` fenced block |

The parser is lenient about whitespace and blank lines between these but strict about the labels — each must appear with the exact `**Label:**` bolding and colon, on its own line starting with `- `. Missing labels don't raise an error; the corresponding `Reviewer` field is just left at its default (empty string for text fields, empty list for `Keywords`). A reviewer block missing the `- **System Prompt:**` line followed by a fenced ```text``` block is skipped entirely with a warning (no system prompt = no runnable reviewer).

### Validation attribution tables — parser-relevant detail

Section 7 of the DB markdown carries a single fenced ```yaml``` block with three keys that control the validation calibration step:

| YAML key                | Type                    | Consumer                                                           |
|-------------------------|-------------------------|--------------------------------------------------------------------|
| `category_vocab`        | list[str]               | LLM human-review conversion + post-parse normalization (unknown categories are blanked out) |
| `category_to_persona`   | dict[str → persona]     | `route_category()` — maps a missed human comment's category to the persona that should have caught it |
| `sub_rating_to_persona` | dict[str → persona]     | Low sub-rating (Soundness / Presentation / Contribution) → expected persona for attribution |

Every persona string on the right-hand side MUST match a persona name from some `#### R### — <Persona>` heading in the same DB. The parser doesn't enforce this — the calibration step just silently fails to match, and missed comments accumulate as orphan entries ("expected persona: <Phantom>, was_selected=False") in the report. If the whole section 7 block is missing, the parser returns empty maps and calibration simply skips miss / sub-rating attribution for that DB.

Lowercase is the canonical form for the keys on both maps; `category_vocab` is the only list. Additional fuzzy keys in `category_to_persona` (e.g. `"baseline": Methodology Critic` alongside `"methodology": Methodology Critic`) are supported — the router substring-matches any key against the incoming category string, so misspelled or multi-word LLM categories still route somewhere sensible.

### The system prompt

The `text` fenced block after `- **System Prompt:**` is passed verbatim as the `system_prompt` to the LLM when this reviewer runs. Its content is entirely up to you; the default database templates it as:

```text
You are **Reviewer R042**, an expert peer reviewer for computer architecture research,
specialized in **Memory Systems**. You adopt the persona of a **Reproducibility Champion**:
your reviewing lens emphasizes Reproducibility, artifact quality, and experimental transparency.

## Expertise Profile
- **Sub-area**: Memory Systems — <description>
- **Typical venues you review for**: ISCA, MICRO, HPCA, ASPLOS, SIGMETRICS, PACT
- **Core technical fluency includes**: DRAM, SRAM, cache, coherence, consistency, ...

## Your Reviewing Priorities

As a Reproducibility Champion, you focus on: Reproducibility, artifact quality,
and experimental transparency.

You ask:
    1. Is the evaluation setup reproducible end-to-end?
    2. Are datasets, random seeds, and hyperparameters specified?
    3. ...

Reviewing style: Trust-but-verify; asks whether another group could replicate the results.

## Common concerns flagged by this persona
<one sentence>

## Output Format
... <structured output instructions — see the bundled database for the full template> ...
```

The bundled database's system prompts follow a rigid template; you are not required to. What matters is that the LLM's response parses into comment records downstream (see [Review Output Format](review_output_format.md) for the expected shape).

---

## 3. Uploading and using a database

Once you have a `.md` file in the right shape:

- **Web UI**: open the **Databases** page and use the **Upload a database** form. The file is parsed on upload; malformed databases are rejected with an error explaining what went wrong.
- **Filesystem**: drop the `.md` into `{workdir}/databases/` (the path is shown on the Databases page).
- **CLI**: pass `--db path/to/my.md` to `ai-paper-review-review`.

After upload, the new database appears in the **Reviewer database** dropdown on the home page.

### Filename handling

- The **filename you upload is kept verbatim** (any directory components are stripped). It becomes the database's ID both in the home-page dropdown and in URLs — e.g. uploading `bio.md` makes it selectable as `bio.md`, viewable at `/database/bio.md/view`, and deletable via `/database/bio.md/delete`.
- The filename **must end in `.md`**. Names containing `/`, `\`, `..`, or starting with `.` are rejected.
- Uploading a file with the **same name** as an existing user database **overwrites** it (after the new file parses successfully — otherwise the existing one is left untouched).
- The reserved ID **`__default__`** always points at the bundled Computer Architecture database. You cannot upload a file named `__default__`.
- The **label** shown in the UI (dropdown text and the database table) is built as `"<title-from-first-# heading>  (<filename>)"`. So a file named `bio.md` whose first heading is `# Molecular Biology Paper Review System — Reviewer Database` appears as `"Molecular Biology Paper Review System — Reviewer Database  (bio.md)"`. If the heading can't be parsed, the filename stem is used as a fallback.
- Uploaded databases live in `{workdir}/databases/` (the path is shown on the Databases page). Dropping a valid `.md` directly into that folder outside the web UI has the same effect as uploading — it's picked up on the next page load.

---

## 4. Building a new database — step-by-step

The shortest path from "I want reviewers for discipline X" to an uploadable `.md` file. Assumes some comfort with editing a YAML file and writing a small Python or shell script.

### Step 1 — decide the matrix

Before opening an editor, answer three questions:

1. **How many sub-domains partition your field?** The bundled default uses 10 for Computer Architecture. Most disciplines fit comfortably in 5–15. Too few and the selector can't differentiate; too many and each persona gets diluted.
2. **How many reviewing personas per domain?** The bundled 20 covers almost every reviewing concern (novelty, methodology, reproducibility, clarity, scope, etc.). Reusing the bundled 20 is usually the right choice — only replace them if your discipline has genuinely different reviewing lenses.
3. **What name should each reviewer's system prompt cite as the umbrella field?** e.g. `"molecular biology"`, `"climate science"`, `"condensed matter physics"`.

### Step 2 — copy the YAML template

```bash
cp src/ai_paper_review/data/comparch_reviewer_cfg.yaml my_field.yaml
```

### Step 3 — edit the YAML

Open `my_field.yaml` and make three kinds of edits:

1. **Change the top-level `field:`** to your discipline name.
2. **Replace the `domains:` list** — keep the same shape (one mapping per sub-domain with `id`, `name`, `short`, `description`, `keywords`, `venues`), but fill in content for your discipline. Aim for 20–30 specific technical keywords per domain; generic terms like `"research"` dilute the selector.
3. **Either keep `personas:` as-is** (recommended — the 20 bundled personas work for almost every field) **or replace** them if your discipline needs different reviewing lenses.
4. **Add `validation_attribution:`** if you plan to run the validation pipeline against this DB. Copy the bundled block and adjust the category list + persona names for your field. Skipping this is fine for review-only DBs, but validation will log a warning and produce a calibration report with no miss attribution against that DB.

You can also delete the `personas:` key entirely from your config and rely on falling back to the bundled default — see §1 for partial-config semantics.

### Step 4 — convert YAML to the reviewer-database markdown

This project does not ship tooling for this conversion (by design — the generator and its LLM variants were removed to keep the surface area small). Write a small script yourself, using the format spec in §2 as the template. Roughly 50–80 lines of Python. A minimal template-based approach:

```python
import yaml
from pathlib import Path

cfg = yaml.safe_load(Path("my_field.yaml").read_text())
field = cfg["field"]
domains = cfg["domains"]
personas = cfg["personas"]
attribution = cfg.get("validation_attribution")  # optional; see §1

# System-prompt template used for every reviewer.
SYSTEM = """You are **Reviewer {rid}**, an expert peer reviewer for {field} research, \
specialized in **{domain}**.
... (see bundled comparch_reviewer_db.md for the full template)
"""

out = [f"# {field.title()} Paper Review System — Reviewer Database\n",
       f"**Version:** 1.0\n",
       f"**Total Reviewers:** {len(domains) * len(personas)}\n",
       f"**Domains:** {len(domains)} × **Personas per Domain:** {len(personas)}\n\n---\n",
       "\n## 5. Reviewer Entries\n"]
idx = 1
for d in domains:
    out.append(f"\n### Domain {d['id']}: {d['name']}\n\n")
    for p in personas:
        rid = f"R{idx:03d}"
        sp = SYSTEM.format(rid=rid, field=field, domain=d['name'])  # + persona-specific text
        out.append(f"\n#### {rid} — {p['name']}\n\n"
                   f"- **Domain:** {d['name']}\n"
                   f"- **Persona:** {p['name']}\n"
                   f"- **Focus:** {p['focus']}\n"
                   f"- **Review Style:** {p['style']}\n"
                   f"- **Keywords:** {', '.join(d['keywords'])}\n"
                   f"- **System Prompt:**\n\n```text\n{sp}\n```\n\n")
        idx += 1

# Section 7 — required if this DB will be validated against human reviews.
if attribution:
    out.append("\n---\n\n## 7. Validation Attribution Tables\n\n")
    out.append(
        "Consumed by the validation calibration step. Persona names on the "
        "right-hand side must match `####  R### — <Persona>` headings above.\n\n"
    )
    out.append("```yaml\n")
    out.append(yaml.safe_dump(attribution, sort_keys=False))
    out.append("```\n")

Path("my_field.md").write_text("".join(out))
```

Reference `src/ai_paper_review/data/comparch_reviewer_db.md` for what the expected output looks like in full. The parser is lenient about spacing but strict about the six `- **Label:**` lines and the fenced `text` code block — see §2's "Reviewer block — parser-relevant detail" table for the exact shapes. For section 7's YAML block, see the companion "Validation attribution tables — parser-relevant detail" table.

### Step 5 — upload and test

Upload via the **Databases** page in the web UI. The upload handler parses the file and rejects it with a clear error if anything's malformed.

After upload, run a test review against a sample paper from the discipline to sanity-check that the selector picks sensible reviewers and that the system prompts produce useful critique. If the selector is missing obvious matches, go back to step 3 and add more specific keywords to the relevant domain.

### A note on programmatic vs hand-authored databases

A database that's purely templated from a YAML config will have 10×20 = 200 nearly-identical system prompts that differ only in domain name and persona focus. That's fine for a first pass, but the bundled Computer Architecture database has hand-tuned prompts with domain-specific anchoring ("Check whether the paper quantifies SRAM vs DRAM energy trade-offs…") that a pure template can't produce. If you want that level of polish, plan to hand-edit individual reviewer blocks after generation — and remember that each reviewer block is self-contained, so editing one doesn't affect the others.
