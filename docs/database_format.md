# Reviewer Database Format

This document specifies the two file formats involved in a reviewer database:

1. **YAML config** — a compact description of a field's sub-domains and reviewing personas. Easy to edit by hand or produce from a script.
2. **Reviewer-database markdown** — the runtime artifact the paper-review pipeline parses. Verbose but self-contained: each reviewer is a full LLM system prompt.

Two databases are bundled by default: **Computer Architecture** and **Machine Learning & AI**. Each ships both formats:

| Database | YAML config | Runtime markdown |
|---|---|---|
| Computer Architecture | `src/ai_paper_review/database/comparch_reviewer_cfg.yaml` | `src/ai_paper_review/database/comparch_reviewer_db.md` |
| Machine Learning & AI | `src/ai_paper_review/database/mlai_reviewer_cfg.yaml` | `src/ai_paper_review/database/mlai_reviewer_db.md` |

Run `ai-paper-review-generate-db --config <cfg.yaml> --out <db.md>` to convert a YAML config into the runtime markdown. See §4 for the full step-by-step.

---

## 1. YAML config format

A reviewer-config YAML describes a single field: what sub-domains partition it, what reviewing personas apply, and what keywords/venues each sub-domain is known for.

### Top-level keys

| Key                      | Type    | Description                                                              |
|--------------------------|---------|--------------------------------------------------------------------------|
| `field`                  | string  | Umbrella discipline name. Appears in every reviewer's system prompt.     |
| `version`                | string  | Version stamp written into the reviewer-database markdown header.        |
| `domains`                | list    | Sub-domains that partition the field. Each is a mapping (see below).     |
| `personas`               | list    | Reviewing personas. Replicated in every domain. Each is a mapping (see below). Design personas that match your field's reviewing concerns — see the bundled configs for examples. |
| `validation_attribution` | mapping | Category vocab + category/sub-rating → persona routing maps used by the validation calibration step. See "Validation attribution entry" below for the three sub-keys. |

### Domain entry

Each item in the `domains:` list is a mapping with these keys:

| Key           | Type   | Description                                                                  |
|---------------|--------|------------------------------------------------------------------------------|
| `id`          | string | Short identifier, e.g. `"D1"`, `"D2"`, … Used to group reviewer IDs.         |
| `name`        | string | Human-readable sub-domain name, e.g. `"AI/ML Systems"`.                      |
| `short`       | string | Lowercase slug, e.g. `"ai_ml_systems"`. No spaces or special characters.     |
| `description` | string | One-sentence summary of what the sub-domain covers.                          |
| `keywords`    | list   | 20–30 technical terms, tools, methods, or datasets used in papers in this sub-area. Used by the reviewer selector to match papers. |
| `venues`      | string | Comma-separated list of 5–8 conferences or journals where papers in this sub-area appear. |

### Persona entry

Each item in the `personas:` list is a mapping with these keys:

| Key               | Type   | Description                                                                  |
|-------------------|--------|------------------------------------------------------------------------------|
| `name`            | string | Short display name, e.g. `"Novelty Hunter"`.                                 |
| `slug`            | string | Lowercase identifier, e.g. `"novelty"`.                                      |
| `focus`           | string | One-line focus statement: what this reviewer emphasizes.                     |
| `style`           | string | One-sentence characterization of how this reviewer approaches a paper.       |
| `priorities`      | list   | 2–5 bullet points phrased as questions the reviewer asks of every paper.     |
| `common_concerns` | string | One sentence summarizing the typical issues this persona flags.              |

### Validation attribution entry

The validation pipeline compares AI-generated reviews against real human reviews. When a human reviewer raises a concern that the AI missed, the pipeline needs to know *which AI persona should have caught it* — so it can recommend adjusting that persona's prompt or selection weight. The three keys in `validation_attribution:` control this routing. There are two distinct kinds of misses, each handled by a different key:

- **Explicit misses** — the human wrote a comment the AI didn't raise. The comment's `Category:` tag (constrained by `category_vocab`) is looked up in `category_to_persona` to identify the responsible persona.
- **Implicit misses** — the human gave a *low sub-rating* (e.g. Soundness: 2/5) without necessarily writing a detailed comment about it. The sub-rating dimension is looked up in `sub_rating_to_persona` to identify the persona that should have flagged the underlying weakness.

Every persona name on the right-hand side of either map MUST exactly match a `name:` from the `personas:` list — otherwise the calibration report will point at personas that don't exist in the DB. All three sub-keys (`category_vocab`, `category_to_persona`, `sub_rating_to_persona`) are required.

#### `category_vocab` — closed category list

A list of short lowercase strings that define the allowed category tags for a review comment. When a human review is converted to structured AI-review format, the LLM is told to assign each comment one of these strings as its `Category:` field. Any value the LLM produces that is not in this list is blanked out at parse time.

Practical guidance:
- Use 15–25 entries that cover the reviewing concerns in your `personas:` list. Each persona should correspond to at least one vocab entry.
- Keep strings short and lowercase (e.g. `novelty`, `methodology`, `data`, `ablation`) — the LLM conversion step picks from this list literally.
- If you add fuzzy synonyms in `category_to_persona`, you don't need to add them here too — `category_vocab` is the *controlled* list; `category_to_persona` handles the fuzzy matching separately.

#### `category_to_persona` — category → persona routing

A dict mapping a lowercase category string (or a near-miss keyword) to the persona name that should have raised concerns in that category. The router **substring-matches** each key against the incoming category string, so a key like `"methodology"` also matches `"methodology and experimental rigor"`. This lets a few extra fuzzy keys per persona cheaply catch LLM-produced category drift without bloating the vocab.

Practical guidance:
- Every entry in `category_vocab` should appear as a key here, mapping to the most relevant persona.
- Add 2–4 fuzzy synonym keys per persona for common misspellings or multi-word LLM outputs (e.g. `"baseline"` → `Methodology Critic` alongside `"methodology"` → `Methodology Critic`).
- Every persona should appear on the right-hand side at least once — orphan personas never receive miss attribution.

#### `sub_rating_to_persona` — sub-rating → persona routing

Handles **implicit misses**: cases where a human gave a low structured score on a dimension without writing a detailed comment about it. Sub-ratings are the numerical axes some review systems attach to papers alongside the overall score — for example, OpenReview uses **Soundness**, **Presentation**, **Contribution**, and **Confidence**.

When the pipeline encounters a low sub-rating in a human review, it looks up the dimension name here to find the persona that should have flagged the underlying weakness. For example: if a human gives Soundness: 2/5 but writes only one sentence about methodology, the pipeline records a miss against whichever persona is mapped to `soundness` — even though no explicit comment category pointed there.

This is complementary to `category_to_persona`: that key covers explicit comments the AI missed; this key covers dimensions the human scored poorly even when they didn't elaborate in prose.

Standard OpenReview sub-rating keys and their natural persona mappings:

| Sub-rating        | Typical mapping                      |
|-------------------|--------------------------------------|
| `soundness`       | methodology / rigor persona          |
| `presentation`    | clarity / writing persona            |
| `contribution`    | novelty persona                      |
| `clarity`         | clarity / writing persona            |
| `significance`    | novelty or vision persona            |
| `technical`       | methodology persona                  |
| `reproducibility` | reproducibility persona              |

The map only fires when the human review data actually contains sub-rating fields. If the target venue doesn't export sub-ratings, these entries are present but never activated.

#### Minimal example

```yaml
validation_attribution:
  category_vocab:
    - novelty
    - methodology
    - evaluation
    - reproducibility
    - clarity

  category_to_persona:
    novelty:          Novelty Hunter
    originality:      Novelty Hunter
    contribution:     Novelty Hunter
    methodology:      Methodology Critic
    baseline:         Methodology Critic
    rigor:            Methodology Critic
    evaluation:       Empirical Evaluator
    experiments:      Empirical Evaluator
    reproducibility:  Reproducibility Champion
    artifact:         Reproducibility Champion
    clarity:          Clarity & Presentation Editor
    writing:          Clarity & Presentation Editor
    presentation:     Clarity & Presentation Editor

  sub_rating_to_persona:
    soundness:       Methodology Critic
    presentation:    Clarity & Presentation Editor
    contribution:    Novelty Hunter
    clarity:         Clarity & Presentation Editor
    significance:    Novelty Hunter
    technical:       Methodology Critic
    reproducibility: Reproducibility Champion
```

See §7 of the bundled `comparch_reviewer_db.md` and `mlai_reviewer_db.md` for full 20-persona examples with field-specific vocabulary.

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

See `src/ai_paper_review/database/comparch_reviewer_cfg.yaml` and `src/ai_paper_review/database/mlai_reviewer_cfg.yaml` for full production-scale examples (10 domains × 20 field-specific personas each, all fields populated including `validation_attribution`).

### Reviewer count

The runtime reviewer count is `len(domains) × len(personas)`. With 10 domains and 20 personas you get 200 reviewers. There's no hard limit on database size; the selector picks the top N by similarity for each paper, where N is chosen by the user per run (default 10, recommended range 5–10 to balance coverage against cost, hard range 1–20). Database size mostly affects how well-targeted the selection is — a larger DB means more candidates to diversify across, not more reviewers emitted per run.

### Design guidance

- **Keep domain keywords specific.** The selector uses these to match a paper's extracted keywords against each reviewer's domain. Generic terms (e.g. "research", "analysis") dilute the matching; specific technical terms (tools, methods, algorithms, datasets) make it work well.
- **Tailor personas to your field.** The reviewing concerns that matter for computer architecture (silicon feasibility, energy, deployment, formal methods) differ from those that matter for ML/AI (data quality, benchmark contamination, ablation analysis, scaling, ethics). Copy the bundled config closest to your discipline and adapt the personas rather than using a generic set.
- **Priorities should be questions, not statements.** Each priority is prepended with "You ask:" in the generated system prompt, so phrase them as things the reviewer checks.

---

## 2. Reviewer-database markdown format

The markdown file is what the pipeline actually parses at runtime. It's organized into seven numbered sections plus per-reviewer entries. The parser cares about three things:

1. The structured per-reviewer blocks in **Section 5: Reviewer Entries**.
2. The fenced `text` code block after `- **System Prompt:**` in each reviewer.
3. The fenced `yaml` code block in **Section 7: Validation Attribution Tables** (consumed by the validation pipeline — category vocab and category/sub-rating → persona routing).

Sections 1–4 and 6 are for human readers only; you can omit or reword them freely. Section 7 carries the validation attribution tables — always present in databases generated by `ai-paper-review-generate-db` (the YAML config requires `validation_attribution`). For hand-authored databases it is technically optional: if the section is absent the review pipeline still works, but the validation pipeline will skip miss attribution with a warning.

### Overall file layout

````
# <Field-Titlecased> Reviewer Database

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
- **Background**: You have deep familiarity with DRAM, SRAM, cache, coherence, ..., and you
  track recent developments in this area.

## Review Lens (Reproducibility Champion)
- **Style**: Trust-but-verify; asks whether another group could replicate the results.
- **Core questions you always ask**:
    1. Is the evaluation setup reproducible end-to-end?
    2. Are datasets, random seeds, and hyperparameters specified?
    3. ...
- **Patterns you flag most often**: <one sentence>

## Your Task
Read the paper provided in the user message. Produce between **5 and 10 review comments** ...

## Output Format
Return your review in **markdown** using exactly this structure. ...

# Review

**Reviewer ID:** R042
**Domain:** Memory Systems
**Persona:** Reproducibility Champion
**Topic Relevance:** <float 0.0-1.0>
**Overall Recommendation:** <strong_accept | accept | ... | strong_reject>
**Confidence:** <int 1-5>

## Comment 1
- **Severity:** <major | moderate | minor>
- **Category:** <short tag, e.g. novelty, methodology, reproducibility>
- **Section Reference:** <section/figure/table or general>
- **Summary:** <one-sentence summary>
- **Description:** <2-4 sentences>
- **Keywords:** <comma-separated keywords>

## Comment 2
...

## Rules
1. Produce **between 5 and 10** comments — no more, no less.
2. Stay within your persona lens ...
...
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

The process is: produce a YAML config → run the CLI to generate the markdown → upload. The YAML can be written by hand or generated by an LLM in seconds.

### Step 1 — produce the YAML config

**Option A — LLM generation (recommended)**

Use the bundled prompt at `src/ai_paper_review/prompts/database_generation.md`. Copy the prompt, replace `[FIELD NAME]` with your discipline, and paste into any capable LLM (Claude, GPT-4, Gemini, etc.). The LLM produces a complete, ready-to-run YAML covering all five top-level keys — domains, personas, and a full `validation_attribution` block — in one shot.

Review the output before running the CLI:
- Check that `domains` covers the field's sub-areas and that each keyword list is specific (20–30 technical terms, no generic words).
- Check that `personas` are genuinely field-specific and cover both universal and field-specific reviewing lenses.
- Check that every persona name on the right-hand side of `category_to_persona` and `sub_rating_to_persona` exactly matches a `name:` in the `personas` list.

**Option B — manual editing**

Copy a bundled config and edit it by hand:

```bash
cp src/ai_paper_review/database/comparch_reviewer_cfg.yaml my_field.yaml
```

Edit:
1. `field:` — your discipline name.
2. `domains:` — replace with 8–15 sub-areas for your field. Keep the same entry shape (`id`, `name`, `short`, `description`, `keywords`, `venues`). Aim for 20–30 specific technical keywords per domain.
3. `personas:` — replace with 20 personas tailored to your field's reviewing concerns. Use the comparch and ML/AI configs as reference for the entry shape.
4. `validation_attribution:` — update `category_vocab`, `category_to_persona`, and `sub_rating_to_persona` to match your new personas. See §1.4 for the full spec.

### Step 2 — generate the reviewer-database markdown

Run the CLI. It reads your YAML and writes the full reviewer-database markdown in one step:

```bash
ai-paper-review-generate-db \
    --config my_field.yaml \
    --out    my_field.md
```

The command prints the output path, file size, and reviewer count on success. If `--out` is omitted the file is written to `./<field-slug>_reviewer_db.md` in the current directory.

### Step 3 — upload and test

Upload via the **Databases** page in the web UI. The upload handler parses the file and rejects it with a clear error if anything is malformed.

After upload, run a test review against a sample paper from the discipline to sanity-check that the selector picks sensible reviewers and that the system prompts produce useful critique. If the selector misses obvious matches, go back to step 1 and add more specific keywords to the relevant domain.

### A note on programmatic vs hand-authored databases

A database that's purely templated from a YAML config will have 10×20 = 200 nearly-identical system prompts that differ only in domain name and persona focus. That's fine for a first pass, but the bundled Computer Architecture database has hand-tuned prompts with domain-specific anchoring ("Check whether the paper quantifies SRAM vs DRAM energy trade-offs…") that a pure template can't produce. If you want that level of polish, plan to hand-edit individual reviewer blocks after generation — and remember that each reviewer block is self-contained, so editing one doesn't affect the others.
