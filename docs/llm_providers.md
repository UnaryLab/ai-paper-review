# LLM providers

`ai_paper_review` supports multiple LLM providers. Pick one, fill in an API key, and everything else — the paper review pipeline and the text-to-markdown human-review converter — uses the same configuration.

## Supported providers

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

## Config file

Copy the template and edit it:

```bash
cp config.example.yaml config.yaml
```

```yaml
llm_review:                      # required
  provider: anthropic_api        # or: openai_api | google_api | xai_api | github_api |
                                 #     claude_sdk | copilot_sdk | openai_compatible_api
  model: claude-sonnet-4-6
  # base_url: http://localhost:11434/v1       # Ollama example (openai_compatible_api)
  # base_url: https://<resource>.openai.azure.com/openai/deployments/<name>  # Azure OpenAI example

  # Rate-limiting knobs — tune for your provider's quota.
  max_concurrent: 10             # max parallel LLM requests
  request_delay: 0.0             # seconds to sleep between dispatches (0 = no delay)
  max_retries: 2                 # retries on 429 / transient errors before giving up
  retry_base_delay: 5.0          # base delay for exponential backoff on retry (seconds)

# llm_validation:                # optional — inherits llm_review when absent
#   provider: openai_api
#   model: gpt-4o-mini
#   base_url: https://my-proxy.example.com/v1  # per-stage override

api_keys:
  anthropic_api: sk-ant-...      # only the one matching your provider needs filling
  openai_api:                    # leave blank if unused
  google_api:
  xai_api:
  github_api:
  openai_compatible_api:
```

Each stage (`llm_review`, `llm_validation`) has its own optional
`base_url`, so the two can point at entirely different endpoints when
needed. Omit the key to use the provider's default endpoint.

### Rate-limiting knobs

The four settings above all live inside `llm_review:`. The review stack
does the bulk of the LLM work — parallel per-reviewer calls during a
review, plus the batch-similarity alignment call in validation — so its
knobs govern throughput for both stages. Defaults are the values shown
in the code block above.

| Setting | Default | What it does |
|---|---|---|
| `max_concurrent` | `10` | Maximum number of reviewers dispatched in parallel. At `10`, all 7 default reviewers launch simultaneously; at `1`, they run strictly serially. Lower this when your provider's quota is low or if you're hitting connection-pool limits. |
| `request_delay` | `0.0` | Seconds to sleep between dispatching consecutive requests. Stays at 0 for paid plans. On strict free tiers (one request per second), set to `1.0` so parallel dispatch naturally stays under quota without forcing serial execution. |
| `max_retries` | `2` | How many times to retry a request that hit a rate-limit or transient error (HTTP 429, 5xx, connection reset) before giving up and logging the reviewer's result as failed. A single-reviewer failure doesn't abort the whole run — other reviewers still complete. |
| `retry_base_delay` | `5.0` | Base delay (seconds) for exponential backoff between retries. Attempt 1 waits this long; attempt 2 waits `2×` this; attempt 3 waits `4×`. With the defaults (`max_retries: 2`, `retry_base_delay: 5.0`), a rate-limited request waits at most 5 + 10 = 15 seconds across retries before giving up. |

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

Two reviewers in flight at once with 1-second dispatch spacing respects most free-tier RPM limits. A 30-second backoff base handles "quota exceeded, wait 30 s" responses; 3 retries means worst-case 30 + 60 + 120 = 210 s before a reviewer gives up — enough to ride out a typical per-minute window.

**Local model** (Ollama, llama.cpp, vLLM):

```yaml
llm_review:
  max_concurrent: 1
  request_delay: 0.0
  max_retries: 0
  retry_base_delay: 1.0
```

Local models usually can't handle concurrent requests efficiently (one-at-a-time GPU serialization), so run strictly serial. Skip retries — local errors are typically configuration issues, not transient, so retrying just masks them.

Knobs set in `llm_review:` are the ones the code reads; `llm_validation:` inherits them silently. If validation should use different settings (cheap validation provider with higher quota, for example), copy the four lines into `llm_validation:` as well — same keys, same defaults.

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
| `github_api` | `GITHUB_TOKEN`, then `GITHUB_PAT` |
| `openai_compatible_api` | `OPENAI_API_KEY` |

This means you can keep secrets entirely out of `config.yaml` if you prefer — set the env var in your shell, and only use `config.yaml` for provider/model selection.

## Claude Agent SDK setup

The `claude_sdk` provider uses the official [Claude Agent Python SDK](https://github.com/anthropics/claude-agent-sdk-python). It talks to [Claude Code](https://docs.claude.com/en/docs/claude-code)'s locally-installed CLI, inheriting whatever login state that CLI already has. The same login covers the CLI, the VSCode/JetBrains Claude extensions, and this provider — no API key, no billing setup inside the app.

**Zero-friction setup** (installing via `environment.yml` already covers the Python SDK):

```bash
conda env create -f environment.yml   # installs the Python SDK
conda activate ai-paper-review
# One-time: install the Claude Code CLI if you don't already have it.
# (See https://docs.claude.com/en/docs/claude-code for your platform.)
claude /login                         # opens a browser for Anthropic OAuth
```

**Manual install** (outside conda):

```bash
pip install claude-agent-sdk
claude /login
```

> The PyPI package is `claude-agent-sdk` but it imports as `from claude_agent_sdk import query, ClaudeAgentOptions`. The GitHub repo is `anthropics/claude-agent-sdk-python`; the `-python` suffix identifies the language, it's not part of the PyPI name.

**Then in `config.yaml`:**

```yaml
llm_review:
  provider: claude_sdk
  model: claude-sonnet-4-6   # any model available on your Claude plan
```

No `api_keys` entry needed.

**Routes through your Claude subscription, not the API.** `claude_sdk` uses the same request budget as Claude Code itself — your Pro/Max/Team plan covers it. Rate limits and model availability are defined by the plan, not by an API-key tier.

**How it works internally** — `ai_paper_review.llm.clients.claude.ClaudeSDKClient` wraps the SDK's async `query()` generator in a synchronous `complete()` method. Each call constructs a `ClaudeAgentOptions` with the system prompt + model, iterates the streamed messages, and concatenates the text from every block carrying a `.text` attribute. Duck-typing rather than `isinstance` checks keeps us forward-compatible across minor SDK version bumps.

**Troubleshooting** — if the provider card shows red after `claude /login`:

```bash
# Confirm the SDK is importable from the same Python you run the server with:
python -c "from claude_agent_sdk import query; print('OK')"

# Confirm the CLI is authenticated:
claude /status
```

If the SDK import fails, `pip install claude-agent-sdk` landed in a different Python environment than the web server. Activate the correct env before installing. The server logs a diagnostic line with `sys.executable` on the first failed probe — check the server log.

**Caveats:**

- `claude_sdk` is in active development — the SDK's class hierarchy may shift; this client duck-types on `.content` / `.text` attributes to stay resilient.
- Each reviewer call opens its own `asyncio.run()` (the paper review pipeline dispatches up to `max_concurrent` reviewers in parallel). The Claude Code CLI handles concurrent sessions fine in practice; if you hit throttling, lower `max_concurrent` to 1 or 2 in `config.yaml`.
- Unlike the `anthropic_api` provider (which goes to `api.anthropic.com` with a per-request bill), `claude_sdk` charges against your Claude subscription. Quota behavior mirrors the Claude Code CLI itself.

## GitHub Models setup

GitHub Models is a public catalog of LLMs hosted by GitHub (GPT-4o, Llama, Phi, Mistral, etc.). Any GitHub account can generate a PAT to call it — no Copilot subscription needed.

1. Create a [Personal Access Token](https://github.com/settings/tokens) — fine-grained, no repository access required.
2. Put it in `config.yaml`:
   ```yaml
   llm_review:
     provider: github_api
     model: gpt-4o
   api_keys:
     github_api: ghp_...
   ```
3. Browse models at <https://github.com/marketplace/models> and use the model's identifier as the `model:` value.

Base URL defaults to `https://models.github.ai/inference` — override via `base_url` in `llm_review` only if GitHub moves it.

## Copilot SDK setup

The `copilot_sdk` provider uses the official [GitHub Copilot Python SDK](https://github.com/github/copilot-sdk) (Technical Preview as of early 2026). It communicates with the bundled Copilot CLI over JSON-RPC, inheriting whatever authentication the Copilot CLI already has — no API key, no OAuth token hassle.

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

> The PyPI package name is `github-copilot-sdk` but it imports as `from copilot import CopilotClient`. This is intentional — the short import name matches the TypeScript/Go/.NET SDKs.

**Then in `config.yaml`:**

```yaml
llm_review:
  provider: copilot_sdk
  model: copilot-vscode    # informational only; SDK uses Copilot CLI's active model
```

No `api_keys` entry needed.

**Authentication priority** — the SDK checks in this order (from [the docs](https://github.com/github/copilot-sdk/blob/main/docs/auth/index.md)):

1. Explicit `githubToken` passed to the client
2. HMAC key (`CAPI_HMAC_KEY` / `COPILOT_HMAC_KEY` env vars)
3. Direct API token (`GITHUB_COPILOT_API_TOKEN`)
4. Env vars: `COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`
5. Stored OAuth credentials from `copilot login`
6. `gh auth` credentials (the `gh` fallback)

Any of steps 3–6 work. `gh auth login` is the easiest for new users because it works in one command and also gives you `gh` for other GitHub workflows.

**Troubleshooting** — if the provider card shows red after `gh auth login`:

```bash
# Verify the SDK is importable from the same Python you run the server with:
python -c "from copilot import CopilotClient; from copilot.session import PermissionHandler; print('OK')"

# Verify gh auth:
gh auth status

# Verify the Copilot CLI itself can authenticate:
python -c "from copilot import CopilotClient; import asyncio; asyncio.run(CopilotClient().start())"
```

If the SDK import fails, `pip install github-copilot-sdk` ran in a different Python environment than the web server. Activate the same env before installing. The server logs a diagnostic message with `sys.executable` on the first failed probe — check the server log.

**Conda alternative** — the SDK is also on conda-forge directly:

```bash
conda install conda-forge::github-copilot-sdk
```

**Caveats:**

- `copilot_sdk` is in Technical Preview — the API may change in breaking ways.
- Each worker thread in the pipeline opens its own `asyncio.run()`, so parallel reviewer dispatch works, but Copilot CLI may rate-limit concurrent sessions. If you hit issues, lower `max_concurrent` to 1 or 2 in `config.yaml`.
- The `model` field for this provider is informational only — the SDK uses whatever model Copilot CLI is currently configured with.
- For OpenTelemetry tracing, install with the telemetry extra: `pip install github-copilot-sdk[telemetry]`.

## Per-stage providers and models

The paper review stage and the validation stage (human-review conversion + alignment) can use different providers and models. Helpful when you want premium quality for reviewing but cheap inference for the high-volume validation comparison:

```yaml
llm_review:
  provider: anthropic_api
  model:    claude-opus-4-7              # used to run each paper reviewer

llm_validation:                          # optional — inherits llm_review when absent
  provider: openai_api                   # can even be a different provider
  model:    gpt-4o-mini                  # used by convert + align
```

When `llm_validation:` is omitted, validation uses `llm_review` for both steps. You can specify just `provider` or just `model` inside `llm_validation:` to partially override — the other field inherits from `llm_review`. The same two fields (plus `validation_base_url`) are also editable from the web UI's Model page under the "2. Validation model" section.

## CLI overrides

`ai-paper-review-review` accepts `--provider` and `--model` to override `config.yaml` for a single run:

```bash
ai-paper-review-review --pdf paper.pdf --provider openai_api --model gpt-4o
```

Under the hood it sets `PAPER_REVIEW_REVIEW_PROVIDER_OVERRIDE` and `PAPER_REVIEW_REVIEW_MODEL_OVERRIDE`, which the config loader checks before reading the YAML file. The parallel `PAPER_REVIEW_VALIDATION_*_OVERRIDE` env vars override the validation stage — set them in your shell (or via the web UI's **Model** page) before running `ai-paper-review-validate`. The aggregation CLI doesn't call any LLM, so it has no provider/model flags.

## Web UI provider picker

The web UI's **Model** page at `/model` shows all eight providers as cards in a **Provider availability** grid:

- **Green card** — the provider has a working credential source (API key, SDK installed, or local server reachable). The card shows a green dot, a badge (`key configured` / `SDK ready` / `local server`), and the credential source in muted text (e.g. `ENV: ANTHROPIC_API_KEY` or `config.yaml`).
- **Red card** — credential missing or the SDK isn't installed. The badge reads `no key` / `SDK missing` / `no endpoint`, and the muted text explains why (e.g. `export ANTHROPIC_API_KEY=... or add api_keys.anthropic_api`).

Hovering any card reveals the full credential source or remediation hint as a tooltip. Providers with a custom `base_url` (Ollama, Azure OpenAI, local proxies) display the URL inline in a `<code>` snippet on the card.

Below the grid, the **Review model** and **Validation model** sections let you apply a per-session override of provider / model / base_url. Overrides are stored as process env vars (prefixed `PAPER_REVIEW_*_OVERRIDE`) — applied immediately but not persisted to `config.yaml`, so they revert on server restart. The **Reset to config.yaml** button clears all session overrides in one click.

There is no dedicated marker for the currently-configured default on the cards themselves — the active provider is the one whose `<select>` option is pre-selected in the Review model dropdown, reflecting what `config.yaml` (or a prior session override) currently resolves to.
