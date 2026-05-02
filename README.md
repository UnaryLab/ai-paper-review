# AI Paper Review

Get multiple expert perspectives on your research paper in a few minutes. Upload a PDF, pick how many reviewers from a pool of AI personas should examine it (default 10, **recommended 5–10** for a good balance of speed and accuracy; hard range 1–20), each selected reviewer produces 5–10 structured review comments in parallel, and the results are clustered and ranked so the issues multiple reviewers raise float to the top.

Two reviewer databases are bundled by default: **Computer Architecture** and **Machine Learning & AI** (200 reviewers each: 10 sub-domains × 20 field-specific personas). The reviewer database is a swappable input: you can build one for any research field and upload it through the web UI — see [Bring your own reviewer database](#bring-your-own-reviewer-database) below and [Database Format](docs/database_format.md) for the format spec.

> ## ⚠️ Intended use — please read
>
> **Intended use.** This tool is a **draft-polishing aid for papers you are writing**. It is **not a peer-review generator**. Most venues have strict policies against using LLMs in assigned reviews, due to concerns about bias, hallucination, and the potential for compromising the integrity of the peer-review process. Please use it at your own discretion, and indicate when you have used it.
>
> **Scope.** The system takes in the PDF directly. Depending on the LLM provider, it either analyzes the **full PDF** directly or focuses on the **text and tables** only (extracted by pypdf and MarkItDown). Expect the reviews to focus on methodology description, claims, experimental design, evaluation setup, and writing quality.
>
> **Quality.** Every comment this system produces is a **suggestion to evaluate, not a finding to accept**. AI reviewers hallucinate, miss context, and over-confidently flag non-issues. Expect to reject roughly half of what you see. Alignment verdicts in the validation flow are heuristic: LLMs can match surface wording while missing intent, and miss real matches that are phrased differently. **Treat all output as a signal, not ground truth, and use it at your own discretion.**

---

## Citation
If you use this tool in your paper drafting, please cite:

```
@article{ai-paper-review,
  author = {Di Wu},
  title = {Can AI Review Improve Paper Drafting? An Empirical Study on 20 Computer Architecture Submissions},
  journal = {arXiv preprint},
  year = {2026}
}
```

---

## Quick start

```bash
# 1. Install
git clone <this-repo> ai_paper_review
cd ai_paper_review
conda env create -f environment.yml            # installs Python deps + LLM SDKs + gh CLI + ai-paper-review in developer mode
conda activate ai-paper-review

# 2. Configure your LLM provider
cp config.example.yaml config.yaml            # always required — then edit provider + credentials

# Option A: Claude Agent SDK (Claude Code / Claude Pro/Max/Team — no API key needed)
claude /login                                  # one-time login via the Claude Code CLI
# set `provider: claude_sdk` in config.yaml

# Option B: GitHub Copilot (no API key needed)
gh auth login                                  # one-time GitHub auth
# set `provider: copilot_sdk` in config.yaml

# Option C: API-key providers (Anthropic / OpenAI / Google / xAI / GitHub Models)
# set provider + paste your API key in config.yaml

# 3. Launch the web UI to make the life easy
ai-paper-review-web
```

Open **http://127.0.0.1:8000**. The home page shows a provider picker (green = ready to use, red = missing credentials or SDK) and an upload box. Drop a PDF, wait 1–5 minutes, and you'll get a ranked list of issues with links to drill into each cluster.

Prefer the command line? Jump to [Using the CLI](#using-the-cli).

---

## Install

The supported install is conda — `environment.yml` asks for Python 3.11 or newer plus the `gh` GitHub CLI for Copilot SDK auth. `ai-paper-review` is installed directly in developer mode. A developer install is included during creating the conda env.

```bash
conda env create -f environment.yml         # one time — installs Python, LLM SDKs, and gh
conda activate ai-paper-review
```

You can also instal from PyPI, which does not render valid `Docs` page on the web UI.

```bash
pip install ai-paper-review                 # PyPI install
```

After install, five console scripts are on your `$PATH`:

| Command | Purpose |
|---|---|
| `ai-paper-review-web` | Launch the Flask web UI (button-driven flow) |
| `ai-paper-review-review` | Review a PDF from the CLI |
| `ai-paper-review-validate` | Compare AI review vs human review and emit per-paper calibration delta |
| `ai-paper-review-aggregate` | Roll up N calibration deltas into cross-paper tuning recommendations |
| `ai-paper-review-generate-db` | Generate a reviewer-database markdown from a YAML config |

---

## Configure your LLM

Copy the template and edit two things:

```bash
cp config.example.yaml config.yaml
```

```yaml
llm_review:
  provider: anthropic_api        # or: openai_api | google_api | xai_api | github_api |
                                 #     claude_sdk | copilot_sdk | openai_compatible_api
  model: claude-sonnet-4-6

# llm_validation:                # optional — inherits llm_review when absent
#   provider: openai_api
#   model: gpt-4o-mini

api_keys:
  anthropic_api: sk-ant-...      # fill in the one that matches your provider
```

### Supported providers

Each provider has a different setup flow — API key, PAT, SDK install, or local `base_url`. Canonical provider names use a suffix so the kind is visible at a glance: **`*_api`** for HTTP-based providers that take an API key or PAT, **`*_sdk`** for locally-installed SDKs that inherit a CLI's login. The config column is what you paste into `provider:`; the setup column is what you do once to unlock it. The **PDF input** column shows whether the paper PDF reaches the model as-is or is converted to text first.

| Provider | Config value | PDF input | Setup flow |
|---|---|---|---|
| Anthropic Claude | `anthropic_api` | Direct | Create an API key at <https://console.anthropic.com/> → set `api_keys.anthropic_api` in `config.yaml` or export `ANTHROPIC_API_KEY`. |
| OpenAI GPT | `openai_api` | Direct (OpenAI endpoint only) | Create an API key at <https://platform.openai.com/api-keys> → `api_keys.openai_api` or `OPENAI_API_KEY`. **Azure OpenAI:** also set `base_url: https://<resource>.openai.azure.com/openai/deployments/<deployment>` under `llm_review`. |
| Google Gemini | `google_api` | Direct | Create an API key at <https://aistudio.google.com/apikey> → `api_keys.google_api` or `GEMINI_API_KEY` (falls back to `GOOGLE_API_KEY`). |
| xAI Grok | `xai_api` | Direct (grok-4-class models) | Create an API key at <https://console.x.ai/> → `api_keys.xai_api` or `XAI_API_KEY`. Base URL is hardcoded to `https://api.x.ai/v1`. |
| GitHub Models | `github_api` | Text | Create a **fine-grained** GitHub Personal Access Token at <https://github.com/settings/tokens> (no repo scope needed) → `api_keys.github_api` or `GITHUB_TOKEN` (falls back to `GITHUB_PAT`). Browse the catalog at <https://github.com/marketplace/models>. |
| Claude Agent SDK | `claude_sdk` | Direct | `pip install claude-agent-sdk` (already in `environment.yml`), then `claude /login` once via the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code). **No API key needed** — the SDK inherits the CLI's login (shared with VSCode/JetBrains Claude extensions). Routes through your Claude Pro/Max/Team subscription. |
| GitHub Copilot SDK | `copilot_sdk` | Text | `pip install github-copilot-sdk` (already in `environment.yml`), then `gh auth login` once. **No API key needed** — the SDK inherits the Copilot CLI's local auth. Works alongside VSCode Copilot. |
| OpenAI-compatible | `openai_compatible_api` | Text | Point at any OpenAI-protocol endpoint via `base_url` under `llm_review` (e.g. Ollama `http://localhost:11434/v1`, vLLM / llama.cpp, Together, Groq, DeepSeek, Fireworks, Azure-style proxies). API key is **optional** when the base_url looks local; otherwise use `api_keys.openai_compatible_api` or `OPENAI_API_KEY`. |

Full setup details, env-var precedence, rate-limiting presets, and per-stage provider split: [LLM providers](docs/llm_providers.md).

---

## Using the web UI

Launch with `ai-paper-review-web` and open **http://127.0.0.1:8000**. The server writes uploads and run outputs to `./ai-paper-review-data/` in the directory you launched it from (override with `PAPER_REVIEW_WORKDIR=/path/to/data`). The top nav exposes the seven pages below.

### Model — set your LLM provider

Open **Model** first. The page shows all eight providers as cards (green = ready; red = missing credentials or SDK). Below the grid, the **Review model** and **Validation model** sections let you pick the active provider, model, and optional base URL per stage — applied immediately for this session (env-var overrides) and cleared on server restart. For permanent defaults, edit `config.yaml` directly.

### Review — review a paper

1. Pick a **reviewer database** (bundled default, or a `.md` you uploaded on the Database page).
2. Pick the **number of reviewers** to run (default 10; the input is auto-bounded to the smaller of the per-run hard cap and the selected database's size, with an inline error if you exceed it).
3. Upload the **PDF**.
4. The status page polls until the review finishes (1–5 min), then redirects to the result page, which shows:
   - Selected reviewers + their topic-relevance scores.
   - A **Writing clarity review** section — always-on `G001` reviewer, writing-quality only, never clustered or compared to human reviews.
   - **Ranked issues** (major / moderate / minor) grouped by cross-reviewer clustering, each expandable to show every reviewer who raised it.
   - Downloads: `review_report.md`, `review_data.md`, `writing_clarity_review.md`, and the two similarity-matrix artifacts (`selection_similarities.md`, `clustering_similarities.md`).

### Validation — compare AI vs human reviews

1. Upload the human review. Raw text (HotCRP / OpenReview / generic) or markdown both work — an LLM reshapes it into the AI-review schema automatically. Files already in that schema are passed through untouched.
2. Pick the AI side: either a prior review from the dropdown (auto-populated from past runs on this server) or upload a `review_data.md`.
3. Click **Run validation**. The status page polls until the single batch-similarity LLM call and alignment finish (~30–90 s), then redirects to the result page.
4. The result page shows summary metrics (recall / precision / F1 / severity-weighted recall), per-persona performance, hits / misses / false alarms, and per-paper calibration suggestions.

### Aggregation — cross-paper tuning recommendations

After several validations accumulate in the workdir, open **Aggregation**. It globs every completed validation run's `calibration_delta.json`, groups the suggestions by `(type, target)`, and renders the ones that repeat across ≥ `min_support` papers (default 2) as actionable tuning recommendations for the reviewer database. A small form lets you tune `min_support` live. Reporter only — nothing is written to disk from this page.

### Database — browse / upload reviewer databases

Filter by domain or persona, search by keyword, and click into any reviewer to see the full system prompt. The same page has the upload form for dropping in a custom `.md` for a different research field; the **Build a new database** walkthrough spells out the YAML template + LLM-expansion recipe, including the list of 20 canonical persona names Validation's calibration attribution looks for.

---

## Using the CLI

Three console scripts, all flat (no subcommand layer). They read provider/model defaults from `config.yaml` unless overridden. Only `ai-paper-review-review` exposes `--provider` / `--model` flags; `ai-paper-review-validate` picks up `PAPER_REVIEW_VALIDATION_*_OVERRIDE` env vars (set by the web UI's Model page or by hand); `ai-paper-review-aggregate` makes no LLM calls at all.

### Review a paper — `ai-paper-review-review`

```bash
ai-paper-review-review --pdf paper_draft.pdf
```

Writes five files next to the PDF:

| File | Content |
|---|---|
| `paper_draft_review.md`                   | Ranked review report (human-readable). |
| `paper_draft_review_data.md`              | Per-reviewer structured comments — the canonical input to Validation. |
| `paper_draft_writing_clarity_review.md`   | Always-on `G001` writing-clarity reviewer's output. Never enters Validation. |
| `paper_draft_selection_similarities.md`   | Full reviewer-vs-paper similarity landscape; top-N are marked. |
| `paper_draft_clustering_similarities.md`  | Pairwise comment similarity + clustering decisions (near-threshold pair list + full matrix). |

Flags (full list via `--help`):

```bash
ai-paper-review-review \
    --pdf paper_draft.pdf \
    --db comparch_reviewer_db.md \        # defaults to the bundled computer_architecture DB
    --reviewers 7 \                    # N (default 10; hard range 1–20)
    --provider openai_api --model gpt-4o \ # per-run overrides, else config.yaml
    --out review_report.md \                    # default: <pdf_stem>_review.md
    --data-out review_data.md \                 # default: <pdf_stem>_review_data.md
    --clarity-out clarity.md \                  # default: <pdf_stem>_writing_clarity_review.md
    --similarities-out selection_sims.md \      # default: <pdf_stem>_selection_similarities.md
    --clustering-similarities-out clustering_sims.md  # default: <pdf_stem>_clustering_similarities.md
```

### Validate AI vs human review — `ai-paper-review-validate`

The CLI validator expects the human review to already be in AI-review-format markdown. The easiest way is the web UI's **Validation** page — it accepts raw text and runs conversion → alignment → calibration in one click.

```bash
ai-paper-review-validate \
    --actual my_paper_actual.md \
    --ai-review paper_draft_review_data.md \
    --out my_validation.md \            # default: <actual>_validation.md
    --calibration-out my_calibration.json   # default: <actual>_calibration.json
```

Writes five files into the same directory as `--out`:

| File | Content |
|---|---|
| `<actual>_validation.md` | Validation report — miss analysis, metrics, calibration suggestions (human-readable). |
| `<actual>_calibration.json` | Per-paper calibration delta JSON — input to `ai-paper-review-aggregate`. |
| `alignment_llm_analysis.md` | Verbatim LLM prompt + response for the alignment step — full audit trail. |
| `alignment_similarities.md` | N × M human-vs-AI comment similarity matrix; best match per human comment bolded. |
| `alignment_ranking.md` | Human comments ranked by best-match similarity score, highest first. |

Full schema: [Validation Output Format](docs/validation_output_format.md). No `--provider` / `--model` flags — set the validation-stage LLM in `config.yaml` or via `PAPER_REVIEW_VALIDATION_PROVIDER_OVERRIDE` / `PAPER_REVIEW_VALIDATION_MODEL_OVERRIDE`.

### Cross-paper aggregation — `ai-paper-review-aggregate`

After several validation runs accumulate, roll up their calibration deltas into reviewer-database tuning recommendations:

```bash
ai-paper-review-aggregate \
    'ai-paper-review-data/runs/validation_*/calibration_delta.json' \
    --min-support 2 \
    --out recommendations.md    # default: stdout if --out omitted
```

Reporter only — it doesn't modify any config or database file; it prints suggestions that repeat across ≥ `min_support` papers. See [Aggregation](docs/aggregation.md) for the full design notes.

---

## How it works

Three stages, each a separate surface. The review pipeline produces structured critique of one paper; the validation pipeline compares that critique to a real human review and records a calibration delta; aggregation — a post-pipeline reporter — rolls up many deltas into tuning recommendations for the reviewer database.

```
  INPUTS                      STAGE                             OUTPUTS
  ────────────────────        ──────────────────────            ─────────────────────────────
  paper.pdf               ──▶ [1] Review pipeline          ──▶  review_report.md
  comparch_reviewer_db.md        (ingest → select N        ──▶  review_data.md
  N (1–20, default 10)         reviewers → clarity         ──▶  writing_clarity_review.md
  provider / model             reviewer → dispatch in      ──▶  selection_similarities.md
                               parallel → cluster → rank)  ──▶  clustering_similarities.md
                                       │
                                       ▼
  human_review.txt/md     ──▶ [2] Validation pipeline      ──▶  validation_report.md
  review_data.md              (convert → align → metrics   ──▶  calibration_delta.json
   (from stage 1)              → calibration → report)
                                       │
                                       ▼
  N × calibration_delta.json  ──▶ [3] Aggregation (reporter) ──▶ cross-paper recommendations
  (from many runs of stage 2)     (group by type/target,         (markdown; hand-applied to
                                   filter by min_support)         the reviewer-database YAML)
```

Each box maps to a dedicated doc with the stage-by-stage breakdown, diagram, and I/O schema:

- [Review Pipeline](docs/review_pipeline.md)
- [Validation Pipeline](docs/validation_pipeline.md)
- [Aggregation](docs/aggregation.md)

For format specs, provider handling, and reviewer-database details:

- [LLM Providers](docs/llm_providers.md) — LLM provider support and configuration
- [Database Format](docs/database_format.md) — reviewer-database YAML and markdown formats
- [Review Output Format](docs/review_output_format.md) — per-review markdown format
- [Validation Output Format](docs/validation_output_format.md) — validation run artifacts, alignment semantics, `calibration_delta.json` schema

---

## Customization

The project is designed so the four most-likely-to-tune surfaces — rate limits, the reviewer database, LLM providers, and prompts — can each be changed without touching Python, or with a minimal drop-in.

### Tuning knobs

Runtime behavior is tuned through a small set of knobs. The first group lives in `config.yaml` under `llm_review:`; the second group is set per-run via env vars or CLI flags.

| Knob | Where | Default | What it does |
|---|---|---|---|
| `max_concurrent` | `config.yaml` | `10` | Max parallel LLM requests during reviewer dispatch. Lower on strict free tiers. |
| `request_delay` | `config.yaml` | `0.0` | Seconds between dispatching consecutive requests. Set to ~1 s on free tiers hitting RPM limits. |
| `max_retries` | `config.yaml` | `2` | Retries on HTTP 429 / 5xx before a reviewer is logged as failed. |
| `retry_base_delay` | `config.yaml` | `5.0` | Base seconds for exponential backoff on retries (attempt 1 waits base, attempt 2 waits `2×`, etc.). |
| `CLUSTER_THRESHOLD` | env var | `0.55` | Cosine-similarity threshold for merging two review comments into one cluster. `0.65` = stricter. |
| `domain_bleed` | `select_reviewers()` arg | `0.15` | How far outside the top domain the selector may reach to pick a persona-diverse Nth reviewer. |
| `n_reviewers` | per-run form / CLI flag | `10` | Top-N reviewers to dispatch; recommended 5–10, hard range 1–20. Auto-capped at the database's size. |

[Suggested presets](docs/llm_providers.md) for paid-plan / free-tier / local-model configs live in the LLM providers doc.

### Bring your own reviewer database

Two databases are bundled — **Computer Architecture** and **Machine Learning & AI** — each as a YAML config and a generated 200-reviewer markdown. For any other field:

1. **Generate a config YAML** — use the prompt at `src/ai_paper_review/prompts/database_generation.md`: replace `[FIELD NAME]`, paste into any capable LLM, and get a complete YAML in one shot. Or copy one of the bundled `*_reviewer_cfg.yaml` files and edit it manually.
2. **Generate the database** — run `ai-paper-review-generate-db --config my_field_cfg.yaml --out my_field_db.md`.
3. **Upload it** — drop the `.md` on the **Database** page; the server parses it on upload and rejects malformed files with a clear error.

See [Database Format](docs/database_format.md) for the full YAML and markdown spec.

### Tune LLM prompts

Every prompt the system sends is a standalone `.md` file in [`src/ai_paper_review/prompts/`](src/ai_paper_review/prompts/). Edit the file; no Python change required. Placeholders use `{name}` syntax (Python `str.format`) and are documented in each file.

| Prompt file | Used by |
|---|---|
| `writing_clarity_system.md` | Always-on `G001` writing-clarity reviewer. |
| `human_review_extraction_system.md` | Validation Stage 1 — reshape raw human-review text into AI-review markdown. |
| `markdown_repair_system.md` + `markdown_repair_user.md` | Repair retry when a reviewer's (or the clarity reviewer's) first LLM output fails to parse. |
| `batch_alignment_system.md` + `batch_alignment_user.md` | Validation Stage 3 — the single batch-similarity LLM call that produces the N × M matrix. |
| `database_generation.md` | LLM prompt for generating a new reviewer-database YAML config for any field. Replace `[FIELD NAME]` and paste into any LLM. |

The persona reviewers' system prompts live inside the reviewer-database `.md` (one per `#### R###` block), not in `prompts/` — that way a new reviewer database can ship an entirely different set of persona voices.

### Swap or add an LLM provider

All supported providers share a one-method protocol — `complete(system, user, max_tokens) → str`. The contract is in [`llm/clients/base.py`](src/ai_paper_review/llm/clients/base.py); each existing provider is one file in [`llm/clients/`](src/ai_paper_review/llm/clients/) with lazy SDK import.

To add a provider: drop a new `llm/clients/<name>.py` implementing the protocol, register it in the `_PROVIDER_CLASS` dict in [`llm/factory.py`](src/ai_paper_review/llm/factory.py), and (optionally) add env-var fallback entries in [`llm/config.py`](src/ai_paper_review/llm/config.py)'s `_ENV_FALLBACK` / `_DEFAULT_BASE_URLS`. Add the provider's name to `SUPPORTED_PROVIDERS` in the same file. Once registered, it's selectable from `config.yaml` like any other provider — the rest of the pipeline is provider-agnostic.

---

## Troubleshooting

**"No API key found for provider ..."** — Either add it to `config.yaml` under `api_keys.<provider>`, or export the matching env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`). The provider shown on the home page is the *active* one — switch providers in the picker before uploading.

**Web UI home page shows all providers red** — `config.yaml` has no keys and no matching env vars are exported. Fix one, restart the server.

**Review takes >10 minutes** — Reviewers dispatch with no delay by default between each (free-tier-safe default). If you have a paid plan, set `request_delay: 0` in `config.yaml` for faster runs. If you're still hitting rate limits on a free tier, raise `retry_base_delay` to 90–120 seconds.

**Clustering merges issues that should stay separate** — Raise `CLUSTER_THRESHOLD` (default `0.55`) with the env var. `0.65` is a reasonable stricter setting.

**Selector keeps missing a persona you need** — Raise `domain_bleed` above `0.15`, or edit that persona's keywords in your reviewer-database config (see [Database Format](docs/database_format.md)) and re-upload the rebuilt `.md` via the web UI.

**sentence-transformers download fails in a sandbox** — The code auto-falls back to TF-IDF and logs a warning. Quality is slightly lower but functional.

---

## Repo layout

```
ai-paper-review/
├── README.md
├── pyproject.toml                       # declares CLI entry points + deps
├── environment.yml                      # conda env (conda for python+gh, pip -e . for the rest)
├── config.example.yaml                  # copy to config.yaml
│
├── docs/
│   ├── llm_providers.md                 # LLM setup detail
│   ├── database_format.md               # reviewer-database YAML/markdown formats
│   ├── review_pipeline.md               # review pipeline — stages, inputs, outputs, diagram
│   ├── review_output_format.md          # per-review markdown schema
│   ├── validation_pipeline.md           # validation pipeline — stages, inputs, outputs, diagram
│   ├── validation_output_format.md      # validation stage output & calibration_delta schema
│   └── aggregation.md                   # cross-paper aggregation of calibration deltas (post-pipeline reporter)
│
├── src/ai_paper_review/
│   ├── __init__.py                      # ``default_db_path``; package __init__s expose nothing else
│   ├── provenance.py                    # run-ID generation + provenance banner writer
│   │
│   ├── llm/                             # provider-agnostic LLM wrapper
│   │   ├── __init__.py
│   │   ├── __main__.py                  # `python -m ai_paper_review.llm` → resolved-config dump
│   │   ├── config.py                    # ``LLMConfig`` + ``load_config`` (YAML + env overrides)
│   │   ├── factory.py                   # ``make_client`` (config → ready LLMClient)
│   │   ├── retrying.py                  # ``RetryClient`` (rate-limit backoff)
│   │   ├── probing.py                   # ``probe_providers``, ``describe_config`` (UI helpers)
│   │   ├── utils.py                     # ``env_vars_for``, ``is_local_provider``
│   │   └── clients/                     # one file per provider, lazy SDK import
│   │       ├── base.py                  # ``LLMClient`` Protocol
│   │       ├── anthropic.py             # anthropic_api
│   │       ├── openai.py                # openai_api, also serves github_api / openai_compatible_api
│   │       ├── google.py                # google_api
│   │       ├── xai.py                   # xai_api (Responses API + /v1/files for PDFs)
│   │       ├── claude.py                # claude_sdk (Claude Code CLI)
│   │       └── copilot.py               # copilot_sdk (local async session)
│   │
│   ├── review/                          # review pipeline (`ai-paper-review-review`)
│   │   ├── __init__.py
│   │   ├── review.py                    # ``ReviewState``, LangGraph wiring + CLI ``main()``
│   │   ├── reviewer_db.py               # ``Reviewer`` dataclass + DB parser
│   │   ├── pdf_ingestion.py             # PDF text extraction (pypdf / MarkItDown)
│   │   ├── selection.py                 # Embedder + persona-diversified top-N picker
│   │   ├── reviewer_dispatching.py      # parallel LLM dispatch + retries
│   │   ├── clarity.py                   # always-on writing-clarity reviewer (G001)
│   │   ├── parsing.py                   # markdown ↔ dict round-trippers
│   │   ├── clustering.py                # cross-reviewer comment clustering
│   │   ├── ranking.py                   # cluster ranking + report formatter
│   │   └── constants.py                 # N range, severity weights, retry caps
│   │
│   ├── validation/                      # validation pipeline (`ai-paper-review-validate`)
│   │   ├── __init__.py
│   │   ├── validation.py                # CLI ``main()`` — orchestrates all stages below
│   │   ├── conversion.py                # reshape raw human reviews into AI-review markdown
│   │   ├── loading.py                   # flatten human + AI markdown files into comment lists
│   │   ├── alignment.py                 # batch LLM similarity matrix + diagnostic artifact writer
│   │   ├── metrics.py                   # precision / recall / F1
│   │   ├── calibration.py               # per-paper calibration delta builder
│   │   ├── reporting.py                 # markdown validation report
│   │   ├── routing.py                   # category / sub-rating → persona (from DB attribution tables)
│   │   └── constants.py                 # recommendation / severity vocabularies + batch-similarity thresholds
│   │
│   ├── aggregation/                     # cross-paper aggregation (`ai-paper-review-aggregate`)
│   │   ├── __init__.py
│   │   └── aggregation.py               # aggregate N calibration_delta.json files into tuning recommendations
│   │
│   ├── prompts/                         # externalized LLM prompts, one .md per prompt
│   │   ├── __init__.py                  # ``prompts.load(name, **kwargs)`` helper
│   │   ├── shared_reviewer_system.md    # LLM ``system`` arg shared across all N persona reviewers + clarity
│   │   │                                  (identical across calls → provider prompt cache reuses the
│   │   │                                   (system + PDF) prefix across all parallel reviewer calls)
│   │   ├── writing_clarity_system.md    # clarity reviewer's role/scope, loaded into the user message
│   │   ├── human_review_extraction_system.md  # convert raw human review text → AI-review markdown
│   │   ├── markdown_repair_system.md    # fix malformed AI-review markdown
│   │   ├── markdown_repair_user.md
│   │   ├── batch_alignment_system.md    # batch similarity matrix prompt (validation stage)
│   │   ├── batch_alignment_user.md
│   │   └── database_generation.md       # LLM prompt for generating a new reviewer-database cfg YAML
│   │
│   ├── database/                        # bundled databases + generation CLI
│   │   ├── generation.py                # ``ai-paper-review-generate-db`` — YAML config → reviewer DB markdown
│   │   ├── comparch_reviewer_cfg.yaml   # YAML source — Computer Architecture (bundled default)
│   │   ├── comparch_reviewer_db.md      # 200 reviewer prompts — Computer Architecture (bundled default)
│   │   ├── mlai_reviewer_cfg.yaml       # YAML source — Machine Learning & AI (bundled default)
│   │   └── mlai_reviewer_db.md          # 200 reviewer prompts — Machine Learning & AI (bundled default)
│   │
│   └── web/                             # Flask UI (`ai-paper-review-web`), one module per route group
│       ├── __init__.py
│       ├── app.py                       # Flask ``app`` instance, paths, context processor, ``main()``
│       ├── jobs.py                      # in-memory JOBS / VALIDATE_JOBS state, rehydrate, run-id helpers
│       ├── review.py                    # /review routes + review-pipeline worker thread
│       ├── validation.py                # /validation routes + validation-pipeline worker thread
│       ├── aggregation.py               # /aggregation page (cross-paper aggregation surface)
│       ├── databases.py                 # /database routes (list / upload / view / delete)
│       ├── model.py                     # /model page (provider availability + session overrides)
│       ├── docs.py                      # /docs browser (markdown rendering of docs/)
│       ├── run_files.py                 # enumerate artifacts in a run directory for the result page
│       ├── templates/                   # Jinja2 HTML templates, one per page
│       └── static/style.css
│
└── tests/
    ├── conftest.py                      # shared fixtures (mock LLM client, tmp paths)
    ├── fixtures/                        # sample actual.md + ai.md for validation tests
    ├── test_llm.py                      # LLM config loading + provider probing
    ├── test_convert.py                  # human-review extraction + markdown repair
    ├── test_validate.py                 # alignment, metrics, calibration, reporting
    ├── test_provenance.py               # provenance banner generation
    └── test_web.py                      # Flask route smoke tests
```

Each pipeline package's ``__init__.py`` is intentionally empty — every name is reached via its explicit submodule path (e.g. ``from ai_paper_review.review.reviewer_db import Reviewer``). LLM prompts live in ``prompts/`` so editing them is a single ``.md`` change with no Python touched.

Runtime dirs (auto-created, git-ignored): `ai-paper-review-data/{uploads,runs,databases}/`.
