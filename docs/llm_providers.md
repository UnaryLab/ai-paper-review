# LLM providers

`ai_paper_review` supports multiple LLM providers. Pick one, fill in an API key, and everything else (the paper review pipeline and the text-to-markdown human-review converter) uses the same configuration.

## Supported providers

Each provider has a different setup flow: API key, PAT, SDK install, or local `base_url`. Canonical provider names use a suffix so the kind is visible at a glance: **`*_api`** for HTTP-based providers that take an API key or PAT, **`*_sdk`** for locally-installed SDKs that inherit a CLI's login. The config column is what you paste into `provider:`; the setup column is what you do once to unlock it. The **PDF input** column shows whether the paper PDF reaches the model as-is or is converted to text first.

| Provider | Config value | PDF input | Setup flow |
|---|---|---|---|
| Anthropic Claude | `anthropic_api` | Direct | Create an API key at <https://console.anthropic.com/> → set `api_keys.anthropic_api` in `config.yaml` or export `ANTHROPIC_API_KEY`. |
| OpenAI GPT | `openai_api` | Direct (OpenAI endpoint only) | Create an API key at <https://platform.openai.com/api-keys> → `api_keys.openai_api` or `OPENAI_API_KEY`. **Azure OpenAI:** set `base_url: https://<resource>.openai.azure.com/openai/v1/` and `model: <deployment name>` under `llm_review`, and put the Azure key in `api_keys.openai_api`. |
| Google Gemini | `google_api` | Direct | Create an API key at <https://aistudio.google.com/apikey> → `api_keys.google_api` or `GEMINI_API_KEY` (falls back to `GOOGLE_API_KEY`). |
| xAI Grok | `xai_api` | Direct (agentic models, e.g. grok-4.20, grok-4.5) | Create an API key at <https://console.x.ai/> → `api_keys.xai_api` or `XAI_API_KEY`. Base URL is hardcoded to `https://api.x.ai/v1`. |
| Claude Agent SDK | `claude_sdk` | Direct | `pip install claude-agent-sdk` (already in `environment.yml`), then `claude auth login` once via the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (check with `claude auth status`). **No API key needed**: the SDK uses the CLI's login (shared with VSCode/JetBrains Claude extensions) and runs on your Claude Pro/Max/Team subscription. See [API key or subscription](#api-key-or-subscription). |
| GitHub Copilot SDK | `copilot_sdk` | Text | `pip install github-copilot-sdk` (already in `environment.yml`), then `gh auth login` once. **No API key needed**: the SDK inherits the Copilot CLI's local auth. Works alongside VSCode Copilot. |
| OpenAI-compatible | `openai_compatible_api` | Text | Point at any OpenAI-protocol endpoint via `base_url` under `llm_review` (e.g. Ollama `http://localhost:11434/v1`, vLLM / llama.cpp, Together, Groq, DeepSeek, Fireworks, Azure-style proxies). API key is **optional** when the base_url host is local or on a private network (`localhost`, `host.docker.internal`, a single-label name such as `ollama`, `*.local`, `*.lan`, or a loopback / private / CGNAT IP such as `127.0.0.1`, `192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`, `100.64-127.x.x`); otherwise set `api_keys.openai_compatible_api`. `OPENAI_API_KEY` is used only when `base_url` is unset or its host is `openai.com` or a `*.openai.com` subdomain. |

## Config file

Copy the template and edit it:

```bash
cp config.example.yaml config.yaml
```

```yaml
llm_review:                      # required
  provider: anthropic_api        # or: openai_api | google_api | xai_api |
                                 #     claude_sdk | copilot_sdk | openai_compatible_api
  model: claude-sonnet-5-5
  # base_url: http://localhost:11434/v1       # Ollama example (openai_compatible_api)
  # base_url: https://<resource>.openai.azure.com/openai/v1/  # Azure OpenAI example (model: deployment name)

  # Rate-limiting knobs: tune for your provider's quota.
  max_concurrent: 10             # max parallel LLM requests (review and validation)
  request_delay: 0.0             # seconds between request starts (0 = no delay; claude_sdk: at least 1)
  max_retries: 2                 # retries on 429 / transient errors before giving up
  retry_base_delay: 5.0          # base delay for exponential backoff on retry (seconds)

  max_tokens: 16000              # output-token budget per reviewer, clarity, and markdown-repair call

# llm_validation:                # optional: inherits llm_review when absent
#   provider: openai_api
#   model: gpt-4o-mini
#   base_url: https://my-proxy.example.com/v1  # per-stage override

api_keys:
  anthropic_api: sk-ant-...      # only the one matching your provider needs filling
  openai_api:                    # leave blank if unused
  google_api:
  xai_api:
  openai_compatible_api:
```

Each stage (`llm_review`, `llm_validation`) has its own optional
`base_url`, so the two can point at entirely different endpoints when
needed. Omit the key to use the provider's default endpoint.

### Rate-limiting knobs

The four settings above live only inside `llm_review:` and apply to
both stages: the parallel reviewer calls during a review, and the
parallel alignment chunk calls during validation (web UI and
`ai-paper-review-validate`). Defaults are the values shown in the code
block above; a key left empty (e.g. `request_delay:` with no value) also
uses the default.

| Setting | Default | What it does |
|---|---|---|
| `max_concurrent` | `10` | Maximum number of LLM requests in flight at once: reviewers during a review, alignment chunks during validation. At `10`, all 10 default reviewers launch simultaneously; at `1`, they run strictly serially. Lower this when your provider's quota is low or if you're hitting connection-pool limits. |
| `request_delay` | `0.0` | Seconds between starting consecutive requests, for reviewer dispatch and for validation alignment chunks. A stage on `claude_sdk` uses at least `1.0`, whatever this value is. Stays at 0 for paid API plans. On strict free tiers (one request per second), set to `1.0` so parallel dispatch stays under quota without forcing serial execution. |
| `max_retries` | `2` | How many times to retry a request that hit a rate-limit or transient error (HTTP 429, 529, 5xx; the anthropic and openai SDKs also retry connection errors on their own) before giving up and logging the reviewer's result as failed. A single persona-reviewer failure doesn't abort the whole run; other reviewers still complete. If every reviewer fails, or none returns a usable comment, the run stops with an error. The clarity reviewer is re-run up to 2 more times after a failed call, and the run stops with an error if every attempt fails or if it still returns no valid comments after its empty-output retries. A refused or blocked reply, or one that used up its output budget with no text, is never retried. |
| `retry_base_delay` | `5.0` | Base delay (seconds) for exponential backoff between retries. Attempt 1 waits this long; attempt 2 waits `2×` this; attempt 3 waits `4×`. With the defaults (`max_retries: 2`, `retry_base_delay: 5.0`), a rate-limited request waits at most 5 + 10 = 15 seconds across retries before giving up. |

### Output-token budget

`max_tokens` also lives inside `llm_review:`. A key left empty uses the
default.

| Setting | Default | What it does |
|---|---|---|
| `max_tokens` | `16000` | Output-token budget requested by each reviewer, clarity, and markdown-repair call. Reasoning models count reasoning tokens against this budget. Some servers reject a request whose `max_tokens` exceeds the model's output cap, so a model with a smaller cap needs a smaller value, e.g. `4096` for claude-3-haiku, `8192` for claude-3-5-sonnet and gemini-2.0-flash. On vLLM, prompt tokens plus `max_tokens` must fit in `max_model_len`. |

### Suggested presets

**Paid plan, high-quota provider** (Anthropic Tier 3/4, OpenAI Tier 3+):

```yaml
llm_review:
  max_concurrent: 10
  request_delay: 0.0
  max_retries: 2
  retry_base_delay: 5.0
```

The defaults. Parallel dispatch, no sleep between requests, short retries since rate limits are rare.

**Free tier** (Anthropic free, OpenAI tier 0, Gemini free):

```yaml
llm_review:
  max_concurrent: 2
  request_delay: 1.0
  max_retries: 3
  retry_base_delay: 30.0
```

Two reviewers in flight at once with 1-second dispatch spacing respects most free-tier RPM limits. A 30-second backoff base handles "quota exceeded, wait 30 s" responses; 3 retries means worst-case 30 + 60 + 120 = 210 s before a reviewer gives up, enough to ride out a typical per-minute window.

**Local model** (Ollama, llama.cpp, vLLM):

```yaml
llm_review:
  max_concurrent: 1
  request_delay: 0.0
  max_retries: 0
  retry_base_delay: 1.0
```

Local models usually can't handle concurrent requests efficiently (one-at-a-time GPU serialization), so run strictly serial. Skip retries: local errors are typically configuration issues, not transient, so retrying just masks them.

The code reads these knobs only from `llm_review:`; the same keys under `llm_validation:` are ignored, so both stages share one set of values.

## Config lookup order

1. Path in the `PAPER_REVIEW_CONFIG` environment variable (if set)
2. `./config.yaml` in the current working directory
3. `config.yaml` next to the installed `ai_paper_review.llm` module
4. If none of the above: the file is skipped and env-var fallbacks are used

## Environment-variable fallbacks

For any provider whose `api_keys.<name>` entry is blank (or missing), the system checks these environment variables:

| Provider | Env var(s) |
|---|---|
| `anthropic_api` | `ANTHROPIC_API_KEY` |
| `openai_api` | `OPENAI_API_KEY` |
| `google_api` | `GEMINI_API_KEY`, then `GOOGLE_API_KEY` |
| `xai_api` | `XAI_API_KEY` |
| `openai_compatible_api` | `OPENAI_API_KEY`, only when `base_url` is unset or its host is `openai.com` or a `*.openai.com` subdomain; other hosts need `api_keys.openai_compatible_api` |

This means you can keep secrets entirely out of `config.yaml` if you prefer: set the env var in your shell, and only use `config.yaml` for provider/model selection.

## Claude Agent SDK setup

The `claude_sdk` provider uses the official [Claude Agent Python SDK](https://github.com/anthropics/claude-agent-sdk-python). It runs [Claude Code](https://docs.claude.com/en/docs/claude-code)'s CLI and uses that CLI's login. The same login covers the CLI, the VSCode/JetBrains Claude extensions, and this provider: no API key, no billing setup inside the app.

### API key or subscription

Claude models are reachable in two ways. Each stage (`llm_review`, `llm_validation`) picks one explicitly with its `provider:` value; the app never switches between them on its own.

| | `anthropic_api` | `claude_sdk` |
|---|---|---|
| Credential | `api_keys.anthropic_api` or `ANTHROPIC_API_KEY` | Claude subscription login in the Claude Code CLI (`claude auth login`) |
| Billing | Per-request API billing | Your Claude Pro/Max/Team plan |
| Ready when | A key is found | The SDK is installed, the CLI is found, and `claude auth status` shows logged in |
| Prompt caching | The `(system + PDF)` prefix is cached across reviewers | None: the CLI reads the PDF with its Read tool after the user message |
| `max_tokens` | Enforced; reviewer, clarity, and markdown-repair calls request `llm_review.max_tokens` from `config.yaml` (default 16000) | Not enforced; the CLI's own output limit applies |
| Rate limits | API-key tier; HTTP 429 and 5xx are retried | Plan usage limit; a rejection that resets within 5 minutes is retried, a later one stops the run |

Both can run in one process, for example review on `claude_sdk` and validation on `anthropic_api`. An exported `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is hidden from the `claude_sdk` CLI subprocess only (set to empty in its environment), so `claude_sdk` always uses the subscription login while `anthropic_api` still reads the key.

### Install and log in

With conda (`environment.yml` already installs the Python SDK):

```bash
conda env create -f environment.yml   # installs the Python SDK
conda activate ai-paper-review
# One-time: install the Claude Code CLI if you don't already have it.
# (See https://docs.claude.com/en/docs/claude-code for your platform.)
claude auth login                     # opens a browser to sign in
claude auth status                    # check the login
```

Without conda:

```bash
pip install claude-agent-sdk
claude auth login
```

> The PyPI package is `claude-agent-sdk` but it imports as `from claude_agent_sdk import query, ClaudeAgentOptions`. The GitHub repo is `anthropics/claude-agent-sdk-python`; the `-python` suffix names the language and is not part of the PyPI name.

Then in `config.yaml`:

```yaml
llm_review:
  provider: claude_sdk
  model: claude-sonnet-5-5   # any model available on your Claude plan
```

No `api_keys` entry needed.

### How calls run

`ai_paper_review.llm.clients.claude.ClaudeSDKClient` wraps the SDK's async `query()` generator in a synchronous `complete()` method and returns the assistant text written after the last tool call. Each call:

- runs in a fresh temporary directory, removed after the call, with no user or project settings loaded (`setting_sources` is empty), so no `CLAUDE.md`, hooks, or project settings reach the review;
- saves no session transcript under `~/.claude/projects` (`--no-session-persistence`);
- without a PDF, gets no tools and runs a single turn;
- with a PDF attached, copies it into the temporary directory and gets only the Read tool, for up to 10 turns (Read returns at most 20 PDF pages per call); in `dontAsk` permission mode, reads inside that directory are approved and any other request is denied;
- does not enforce `max_tokens`.

A plan rate-limit rejection is handled by its reset time: if overage usage is allowed, the call continues; if the limit resets within 5 minutes, the client waits until the reset and the call is then retried like an HTTP 429; otherwise the run stops (see below). A CLI `rate_limit` or `server_error` message is retried; any other CLI error, or a run that ends in error (for example `error_max_turns`), fails that call without a retry.

A stage on `claude_sdk` starts consecutive requests at least 1 s apart, even when `request_delay` is lower.

Three failures stop the run with an error instead of being retried:

- a usage-limit rejection from your plan with no overage allowed and a reset more than 5 minutes away, or unknown (the error names the reset time);
- the CLI reports it is not logged in or has a billing problem (the error names `claude auth status` and `claude auth login`);
- the CLI is not found (the error says to install Claude Code, then run `claude auth login`).

### Troubleshooting

If the provider card shows red after `claude auth login`:

```bash
# Confirm the SDK is importable from the same Python you run the server with:
conda run -n ai-paper-review python -c "from claude_agent_sdk import query; print('OK')"

# Confirm the CLI is logged in:
claude auth status
```

If the SDK import fails, `pip install claude-agent-sdk` landed in a different Python environment than the web server; activate the correct env before installing. The probe checks the CLI the SDK runs, found with the SDK's own lookup (its bundled binary, then `claude` on `PATH`, then the usual install locations). The server logs each distinct reason the probe fails once; check the server log. A failed probe is checked again after 30 s, so after a later `claude auth login` the card turns green without a restart.

**Caveats:**

- The client reads `.content` / `.text` attributes rather than checking SDK classes, so minor SDK version changes do not break it.
- Each reviewer call opens its own `asyncio.run()`; the review pipeline runs up to `max_concurrent` reviewers at once. If your plan throttles concurrent sessions, lower `max_concurrent` to 1 or 2.
- Unlike `anthropic_api` (per-request billing at `api.anthropic.com`), `claude_sdk` uses your Claude subscription, with the same usage limits as the Claude Code CLI.

## Copilot SDK setup

The `copilot_sdk` provider uses the official [GitHub Copilot Python SDK](https://github.com/github/copilot-sdk) (Technical Preview as of early 2026). It communicates with the bundled Copilot CLI over JSON-RPC, inheriting whatever authentication the Copilot CLI already has: no API key, no OAuth token hassle.

**Zero-friction setup** (if you installed via `environment.yml`, everything below is already installed):

```bash
conda env create -f environment.yml   # installs the Python SDK and gh CLI
conda activate ai-paper-review
gh auth login                          # one-time: authenticate with GitHub (select "GitHub.com",
                                       # then pick web browser or token)
```

That's it. The Copilot SDK will pick up your `gh` credentials automatically on its next call. The web UI card flips to green after restart.

**If you're not using `environment.yml`**, install the pieces manually:

```bash
# GitHub CLI (for auth)
conda install -c conda-forge gh -y

# Or, if not on conda:
# macOS:    brew install gh
# Linux:    see https://github.com/cli/cli/blob/trunk/docs/install_linux.md
# Windows:  winget install GitHub.cli

# Python SDK
pip install github-copilot-sdk

# Then authenticate
gh auth login    # or: copilot login (interactive OAuth from the bundled Copilot CLI)
```

> The PyPI package name is `github-copilot-sdk` but it imports as `from copilot import CopilotClient`. This is intentional: the short import name matches the TypeScript/Go/.NET SDKs.

**Then in `config.yaml`:**

```yaml
llm_review:
  provider: copilot_sdk
  model: gpt-5             # Copilot model id, passed to the Copilot session
```

No `api_keys` entry needed.

**Authentication priority**: the SDK checks in this order (from [the docs](https://github.com/github/copilot-sdk/blob/main/docs/auth/index.md)):

1. Explicit `githubToken` passed to the client
2. HMAC key (`CAPI_HMAC_KEY` / `COPILOT_HMAC_KEY` env vars)
3. Direct API token (`GITHUB_COPILOT_API_TOKEN`)
4. Env vars: `COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`
5. Stored OAuth credentials from `copilot login`
6. `gh auth` credentials (the `gh` fallback)

Any of steps 3 to 6 work. `gh auth login` is the easiest for new users because it works in one command and also gives you `gh` for other GitHub workflows.

**Troubleshooting**: if the provider card shows red after `gh auth login`:

```bash
# Verify the SDK is importable from the same Python you run the server with:
python -c "from copilot import CopilotClient; from copilot.session import PermissionRequestResult; print('OK')"

# Verify gh auth:
gh auth status

# Verify the Copilot CLI itself can authenticate:
python -c "from copilot import CopilotClient; import asyncio; asyncio.run(CopilotClient().start())"
```

If the SDK import fails, `pip install github-copilot-sdk` ran in a different Python environment than the web server. Activate the same env before installing. The server logs a diagnostic message with `sys.executable` on the first failed probe: check the server log.

**Conda alternative**: the SDK is also on conda-forge directly:

```bash
conda install conda-forge::github-copilot-sdk
```

**Caveats:**

- `copilot_sdk` is in Technical Preview: the API may change in breaking ways.
- Each worker thread in the pipeline opens its own `asyncio.run()`, so parallel reviewer dispatch works, but Copilot CLI may rate-limit concurrent sessions. If you hit issues, lower `max_concurrent` to 1 or 2 in `config.yaml`.
- The `model` field is passed to the Copilot session, so it must be a model id your Copilot plan offers.
- Each call runs in a fresh empty temporary directory with no built-in tools and every permission request denied, so the paper text cannot trigger file or shell actions and no repo instruction files (`.github/copilot-instructions.md`, `AGENTS.md`) are loaded. The system prompt is appended to Copilot's own system prompt, which keeps its safety section.
- For OpenTelemetry tracing, install with the telemetry extra: `pip install github-copilot-sdk[telemetry]`.

## Per-stage providers and models

The paper review stage and the validation stage (human-review conversion + alignment) can use different providers and models. Helpful when you want premium quality for reviewing but cheap inference for the high-volume validation comparison:

```yaml
llm_review:
  provider: anthropic_api
  model:    claude-opus-5-5              # used to run each paper reviewer

llm_validation:                          # optional: inherits llm_review when absent
  provider: openai_api                   # can even be a different provider
  model:    gpt-4o-mini                  # used by convert + align
```

When `llm_validation:` is omitted, validation uses `llm_review` for both steps. You can specify just `provider` or just `model` inside `llm_validation:` to partially override; the other field inherits from `llm_review`. The same two fields (plus `validation_base_url`) are also editable from the web UI's Model page under the "2. Validation model" section.

## CLI overrides

`ai-paper-review-review` accepts `--provider` and `--model` to override `config.yaml` for a single run:

```bash
ai-paper-review-review --pdf paper.pdf --provider openai_api --model gpt-4o
```

Under the hood it sets `PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE` and `PAPER_REVIEW_REVIEW_MODEL_OVERRIDE`, which the config loader checks before reading the YAML file. A provider override that differs from `llm_review.provider` does not use `llm_review.base_url`, since that URL belongs to the configured provider; set `PAPER_REVIEW_REVIEW_BASE_URL_OVERRIDE` to give the new provider a base_url. The parallel `PAPER_REVIEW_VALIDATION_*_OVERRIDE` env vars override the validation stage: set them in your shell (or via the web UI's **Model** page) before running `ai-paper-review-validate`. The aggregation CLI doesn't call any LLM, so it has no provider/model flags.

## Web UI provider picker

The web UI's **Model** page at `/model` shows all seven providers as cards in a **Provider availability** grid:

- **Green card**: `config.yaml` exists and the provider has a working credential source (API key, SDK installed, or a local / private-network `base_url` for `openai_compatible_api`; the server is not contacted). The card shows a green dot, a badge (`key configured` / `SDK ready` / `local server`), and the credential source in muted text (`config.yaml`, `env var`, `Copilot CLI auth`, `Claude Code CLI auth`, or `local server at <base_url>`).
- **Red card**: `config.yaml` is missing, the credential is missing, or the SDK isn't installed (for `claude_sdk`, also when the CLI is not found or `claude auth status` does not show logged in). The badge reads `no key` / `SDK missing` / `no endpoint`, and the muted text explains why (e.g. `set api_keys.anthropic_api in config.yaml, or export ANTHROPIC_API_KEY`).

Hovering any card reveals the full credential source or remediation hint as a tooltip. Providers with a `base_url` (Ollama, Azure OpenAI, local proxies, and xAI's fixed endpoint) display the URL inline in a `<code>` snippet on the card.

Below the grid, the **Review model** and **Validation model** sections let you apply a per-session override of provider / model / base_url. Overrides are stored as process env vars (prefixed `PAPER_REVIEW_*_OVERRIDE`), applied immediately but not persisted to `config.yaml`, so they revert on server restart. The **Reset to config.yaml** button clears all session overrides in one click.

There is no dedicated marker for the currently-configured default on the cards themselves; the active provider is the one whose `<select>` option is pre-selected in the Review model dropdown, reflecting what `config.yaml` (or a prior session override) currently resolves to.
