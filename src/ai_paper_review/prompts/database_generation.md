Generate a complete reviewer-database config YAML for the research field: **[FIELD NAME]**.

Output only valid YAML. No explanation, no commentary, nothing before or after the YAML.
Do NOT wrap the output in a markdown code fence (no ```yaml or ``` delimiters).

---

The YAML must contain exactly these five top-level keys, in this order:

```
field                   # the field name, lowercase string
version                 # "1.0"
domains                 # 8–15 sub-domain entries
personas                # exactly 20 persona entries
validation_attribution  # with all 3 required sub-keys
```

---

**`domains`** — 8–15 sub-areas that partition **[FIELD NAME]**. For each entry:

```yaml
domains:
  - id: "D1"              # sequential: "D1", "D2", ... up to "D15"
    name: "<sub-domain name>"
    short: "<lowercase_slug>"   # underscores only, no spaces or hyphens
    description: "<one sentence covering what this sub-domain is about>"
    venues: "<5–8 comma-separated conference/journal abbreviations>"
    keywords:
      - "<term>"     # 20–30 specific technical terms, methods, tools, datasets, algorithms.
      - ...          # No generic words like 'research' or 'analysis'.
```

---

**`personas`** — exactly 20 reviewing personas tailored to **[FIELD NAME]**. Cover:

- The universal lenses every field needs: novelty/originality, methodology soundness, related-work coverage, empirical breadth, theoretical grounding, reproducibility, clarity/presentation.
- Field-specific lenses that matter for **[FIELD NAME]** — think about what a top-tier PC member in this field scrutinizes that reviewers in other fields would not.

For each persona:

```yaml
personas:
  - name: "<Persona Name>"          # title-cased; must be unique across all 20 personas
    slug: "<lowercase_slug>"        # unique, underscores only — e.g. "novelty_hunter"
    focus: "<one-line focus statement>"
    style: "<one sentence: how this persona approaches a paper>"
    common_concerns: "<one sentence: the issues this persona flags most often>"
    priorities:
      - "<question this reviewer always asks — phrased as a yes/no or how/are question>"
      - "<second question>"
      - "<third question>"          # 2–5 items; must be a YAML list, not a string
```

Exact field names matter — spell them exactly as shown: `name`, `slug`, `focus`, `style`, `common_concerns`, `priorities`.

---

**`validation_attribution`** — three sub-keys, all required.

Every persona `name` on the right-hand side of `category_to_persona` and `sub_rating_to_persona` must **exactly match** a `name:` value from the `personas` list above — same capitalisation, same spelling.

**`category_vocab`**: 15–25 short lowercase strings, one per reviewing concern. Each persona must map to at least one entry. Items may contain spaces (e.g. `related work`).

**`category_to_persona`**: maps a category string or near-miss synonym to the responsible persona name. All keys must be lowercase. The router substring-matches keys, so add 2–4 synonym keys per persona to absorb LLM drift (e.g. `baseline` alongside `methodology` both pointing to the same persona). Every persona must appear on the right-hand side at least once.

**`sub_rating_to_persona`**: must contain **exactly these 7 keys**, no more, no fewer:

```yaml
  sub_rating_to_persona:
    soundness:       <name of your methodology/rigor persona>
    presentation:    <name of your clarity/writing persona>
    contribution:    <name of your novelty persona>
    clarity:         <name of your clarity/writing persona>
    significance:    <name of your novelty or vision persona>
    technical:       <name of your methodology persona>
    reproducibility: <name of your reproducibility persona>
```

Full structure:

```yaml
validation_attribution:
  category_vocab:
    - <slug1>
    - ...

  category_to_persona:
    <primary key>:   <Persona Name>
    <synonym key>:   <Persona Name>
    ...

  sub_rating_to_persona:
    soundness:       <Persona Name>
    presentation:    <Persona Name>
    contribution:    <Persona Name>
    clarity:         <Persona Name>
    significance:    <Persona Name>
    technical:       <Persona Name>
    reproducibility: <Persona Name>
```

---

**Before outputting, verify:**

1. `domains` has between 8 and 15 entries; each entry has all six keys (`id`, `name`, `short`, `description`, `venues`, `keywords`); `id` values are sequential strings `"D1"`, `"D2"`, …
2. `personas` has exactly 20 entries; each entry has all six keys (`name`, `slug`, `focus`, `style`, `common_concerns`, `priorities`); `priorities` is a YAML list.
3. Every `name:` in `personas` is unique.
4. Every persona name appearing in `category_to_persona` and `sub_rating_to_persona` exactly matches a `name:` in `personas` — same capitalisation, same spelling.
5. Every persona appears at least once in `category_to_persona` (right-hand side).
6. `sub_rating_to_persona` has exactly the 7 keys listed above.
7. The output is plain YAML — no ```yaml fences, no explanatory text.
