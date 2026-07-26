# Configuration

!!! tip "In a hurry? You can skip this page"
    Most people configure Arbor exactly once: run `arbor setup`, pick a model, done.
    Everything below is for when you want to **change the model**, **set a time/cost
    budget**, **add human oversight**, or **target a specific domain**. Come back when you
    need it.

This page is written for someone who has never used Arbor. It answers three questions, in
order:

1. **What can I configure**, and which settings actually matter?
2. **How do I set each one from the command line?**
3. **When two settings disagree, which wins?**

## Pick your path

=== "Just trying it out"

    Run `arbor setup` to choose a model, then `arbor` to start. You do **not** need a
    config file or any flags. Read [The settings that matter most](#the-settings-that-matter-most)
    if you're curious, and ignore the rest.

=== "Running a real study"

    Do `arbor setup`, then skim [What you can configure](#what-you-can-configure) and
    [Set a budget](#budgets-and-timeouts). A `--max-cycles` flag and the right model
    are usually all you need.

=== "Repeating the same benchmark"

    Put durable settings in a **project config file** so every run is identical, or capture
    a whole domain in a [Plugin](plugins.md). See
    [Per-project settings](#3-per-project-a-config-file).

## What you can configure

Settings fall into four tiers, from "almost everyone touches this" to "advanced".

| Tier | Setting | What it controls | Why you'd change it |
| --- | --- | --- | --- |
| **Essential** | `provider`, `model`, `api_key`, `base_url` | Which LLM Arbor uses and how to reach it | You must pick a model and supply a key once. |
| **Important** | `max_cycles` | How many experiments before Arbor stops and writes the report | The main time/cost knob. Higher = longer, deeper search. |
| **Important** | `reasoning_effort` | How hard the model thinks per step (`low`/`medium`/`high`) | Trade speed/cost for depth. |
| **Important** | `max_turns`, `timeout:` | Hard safety caps on a single experiment | Stop runaway cost on long jobs. |
| **Optional** | `interaction_mode` | How much you steer the run (auto vs. approve ideas) | You want a human in the loop. See [Interaction Modes](interaction-modes.md). |
| **Optional** | `webui_port` / `--no-webui` | The read-only browser monitor | Watch progress live, or turn it off. |
| **Advanced** | `plugin`, `plugin_profile` | Retarget Arbor to a domain (eval rules, protected files, budget bundle) | You run the same kind of benchmark often. See [Plugins](plugins.md). |
| **Advanced** | skills | Sharpen *how* the agent reasons at a step | You want better ideation/analysis. See [Skills](skills.md). |

### The settings that matter most

If you only ever touch three things, make them these:

- **`model`** — quality and cost come mostly from here.
- **`max_cycles`** — how long and deep the study runs.
- **`interaction_mode`** — whether you watch (`auto`) or approve each idea (`review`).

Everything else has a sensible default.

## How to set it — from the command line

There are five places a setting can come from. Listed the way you'll actually reach for
them:

1. **`arbor setup`** — a one-time wizard that saves your model globally. *Most people only
   ever use this.*
2. **`arbor config`** — view or edit that global file later.
3. **A project config file** — durable settings that travel with one project.
4. **CLI flags** — one-off overrides for a single run.
5. **In-chat slash commands** — pick a plugin or skill for this run, no files needed.

### 1. Your model: `arbor setup`

The fastest way to get configured. It asks four questions and writes
`~/.arbor/config.yaml`:

```console
$ arbor setup
arbor setup — let's configure your model (one time).

API type (anthropic/openai/litellm): anthropic
Base URL (local proxy / vLLM, blank for the official API):
Model: claude-sonnet-4-5
API key (blank to read from the environment): ********
✓ credentials look resolvable
Done. Saved to ~/.arbor/config.yaml
```

- **API type** is the provider — see [Providers](#providers) below.
- **Base URL** stays blank for the official Anthropic/OpenAI APIs; set it only for a local
  proxy or gateway.
- **API key** can be left blank to read from an environment variable (recommended) — e.g.
  `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

After this, just run `arbor`.

### 2. Inspect or edit the global config: `arbor config`

```bash
arbor config show           # print the effective config (secrets masked)
arbor config path           # where the file lives
arbor config init --provider openai --model gpt-5 --api-key dummy   # write it non-interactively
```

`arbor config init` is the scriptable sibling of the wizard — handy for setting up a local
gateway in one line:

```bash
arbor config init --provider litellm --model qwen-72b \
  --base-url http://localhost:4141 --api-key dummy
```

### Providers

Pick **one** API type. The value you give to `arbor setup` / `--provider` is one of exactly
three:

| `provider` | Use for | Notes |
| --- | --- | --- |
| `anthropic` | Claude models | Native Anthropic API. |
| `openai` | OpenAI models | Uses the Responses API for reasoning models. |
| `litellm` | DeepSeek, Gemini, Qwen, vLLM, Ollama, local gateways | Anything OpenAI-compatible. Set `base_url`. |

=== "Anthropic"

    ```yaml
    llm:
      provider: anthropic
      model: claude-sonnet-4-5
      api_key: ${ANTHROPIC_API_KEY}
    ```

=== "OpenAI"

    ```yaml
    llm:
      provider: openai
      model: gpt-5
      api_key: ${OPENAI_API_KEY}
      reasoning_effort: medium
    ```

=== "litellm (OpenAI-compatible / local)"

    ```yaml
    llm:
      provider: litellm
      model: deepseek-chat
      api_key: ${OPENAI_API_KEY}   # local gateways often accept any dummy value
      base_url: https://your-gateway/v1
    ```

!!! tip "Keep keys out of files"
    Prefer an environment variable (`${ANTHROPIC_API_KEY}`) over pasting a secret into a
    file. `arbor setup` stores your global key under `~/.arbor/` with the rest of the config.

!!! warning "Experimental: ChatGPT subscription login (`openai-oauth`)"
    ChatGPT Plus/Pro/Team subscribers can drive Arbor with their subscription instead of a
    pay-per-token key. Run `arbor login openai` to sign in through the browser; the token is
    stored in `~/.arbor/oauth/openai.json` and refreshed automatically. This writes:

    ```yaml
    llm:
      provider: openai-oauth
      model: gpt-5
    ```

    Manage the session with `arbor login status` / `arbor login logout`. Requests go to the
    ChatGPT backend (`chatgpt.com/backend-api/codex`), **not** `api.openai.com`.

    Using a subscription token with third-party tooling may violate OpenAI's terms and can
    get your account rate-limited or banned. This path is opt-in and unsupported — prefer a
    standard `OPENAI_API_KEY` for anything you care about.

!!! warning "Experimental: Claude subscription login (`anthropic-oauth`)"
    Claude Pro/Max subscribers can drive Arbor with their subscription instead of a
    pay-per-token key. Run `arbor login claude` to sign in through the browser, then paste
    the code the callback page shows; the token is stored in `~/.arbor/oauth/anthropic.json`
    and refreshed automatically. This writes:

    ```yaml
    llm:
      provider: anthropic-oauth
      model: claude-sonnet-4-5-20250929
    ```

    Manage the session with `arbor login status` / `arbor login logout`. Requests go to the
    Anthropic Messages API as a `Bearer` token (with the `anthropic-beta: oauth-2025-04-20`
    header), **not** with an `x-api-key`.

    Using a subscription token with third-party tooling may violate Anthropic's terms and can
    get your account rate-limited or banned. This path is opt-in and unsupported — prefer a
    standard `ANTHROPIC_API_KEY` for anything you care about.

### 3. Per-project: a config file

When a project needs its own durable settings, drop a YAML file in it. Arbor auto-detects
`research_config.yaml`, `arbor.yaml`, or `autoresearch.yaml` in the target directory (or
pass `--config PATH`). Settings here override your global setup but lose to CLI flags.

```yaml title="research_config.yaml"
# ── Model ──────────────────────────────────────────────
llm:
  provider: anthropic            # anthropic | openai | litellm
  model: claude-sonnet-4-5
  api_key: ${ANTHROPIC_API_KEY}  # env vars are expanded
  base_url: null                 # set for litellm / OpenAI-compatible gateways
  reasoning_effort: medium       # low | medium | high (where supported)
  meta_model: null               # optional cheaper model for meta/report steps

# ── Orchestration ─────────────────────────────────────
max_cycles: 12                   # experiments before Arbor finalizes and reports
executor_max_turns: 60           # hard cap on one experiment's reasoning turns

# ── Timeouts (seconds) ────────────────────────────────
timeout:
  executor: 172800               # 48 h per experiment
  run_training_max: 604800       # 7 d ceiling for one training command

# ── Human-in-the-loop & monitoring ────────────────────
ui:
  interaction_mode: auto         # auto | direction | review | collaborative
  webui_port: 8765               # read-only browser monitor

# ── Literature search / external knowledge ────────────
search:
  enabled: true
  backends: [alphaxiv, jina]     # ordered; fanned out & merged (see below)
  grounded_ideation: false       # let the coordinator search during ideation
  auto_search_on_add: false      # novelty-check every new idea before running it
```

#### External knowledge & search {#search}

!!! tip "Search backends and grounded ideation"
    Arbor can use literature and the open web in two independent, default-off
    lanes — **grounded ideation** (search *during* ideation → `node.grounding`)
    and the **novelty audit** (prior-art check *after* an experiment →
    `node.related_work`). `search.backends` is an ordered, merged list of
    sources; the keyless default is `[alphaxiv, jina]` (papers + general web,
    no setup), with `serper` / `exa` available behind API keys and `exa-mcp`
    keyless. Page reading is keyless too (`visit_backend: auto`, via the Jina
    reader, with PDF support). See the **[Search & External Knowledge](search.md)**
    guide for the full backend table, intents, keys, and examples.

    Key fields: `backends`, `grounded_ideation`, `auto_search_on_add`,
    `visit_backend`, `visit_max_content_tokens` / `research_visit_tokens`,
    `serper_api_key` / `exa_api_key` / `jina_api_key`, `exa_mcp_url`. Legacy
    `builtin_backend` / `web_search_endpoint` still work.



!!! note "Flat keys also work"
    The nested groups (`llm:`, `timeout:`, `ui:`) are the recommended style, but equivalent
    flat keys are accepted. See `examples/research_config.example.yaml` in the repository
    for an annotated reference.

### 4. One-off: CLI flags

Flags override everything else, for a single run only:

```bash
arbor run --max-cycles 20 --mode review --no-webui
```

Common ones: `--max-cycles N`, `--max-turns N`, `--mode MODE`, `--webui-port N`,
`--no-webui`. See the [CLI reference](cli.md#arbor-run) for the full list.

### 5. In the chat: pick a plugin or skill for this run

You don't have to edit files to change domain behavior. While the intake chat is open, type
`/`:

```text
/plugin load mle_kaggle mle_bench_lite   # use a domain plugin (+ profile) for this run
/plugin unload                           # ignore any configured plugin this run
/skill load idea_drafting                # load an extra reasoning playbook
/skill unload first_principles_probe     # drop a default skill this run
```

These choices apply to the single run you're about to launch and don't touch your config.
See [Plugins](plugins.md) and [Skills](skills.md).

## What each setting means

### Orchestration

| Key | Meaning |
| --- | --- |
| `max_cycles` | Maximum number of completed / skipped / failed idea experiments before Arbor finalizes and writes the report. Override per-run with `--max-cycles`. |
| `executor_max_turns` | Hard cap on a single experiment's reasoning turns — a runaway/cost safety valve. Override with `--max-turns`. |
| `reasoning_effort` | How hard the model thinks per step (`low`/`medium`/`high`, where the provider supports it). |
| `meta_model` | Optional cheaper/faster model for meta-level steps (distilling insight, drafting the report) while `model` drives the main loop. |
| `logprobs` | Opt-in sampled-token logprobs for token-faithful trajectory traces. Off by default — some OpenAI-compatible endpoints (Gemini, Ollama) reject the field with HTTP 400 instead of ignoring it. Enable only where supported (OpenAI, vLLM, DeepSeek). Override with `--logprobs`/`--no-logprobs`. |

### Budgets and timeouts

The `timeout:` group bounds how long individual operations may run (in seconds):

| Key | Default | Meaning |
| --- | --- | --- |
| `executor` | `172800` (48 h) | Wall-clock limit for one experiment. |
| `run_training_max` | `604800` (7 d) | Ceiling for one long-running training command. |

For benchmarks, the tidiest way to set a coherent budget is a **plugin profile**, which
bundles `max_cycles`, tree depth, executor timeout, and total time budget under one name
(e.g. `mle_bench_lite`). See [Plugins](plugins.md).

### Human-in-the-loop & monitoring

The `ui:` group controls oversight and the live monitor:

| Key | Meaning |
| --- | --- |
| `interaction_mode` | `auto`, `direction`, `review`, or `collaborative`. See [Interaction Modes](interaction-modes.md). Override with `--mode`. |
| `webui_port` | Port for the browser monitor (default `8765`). See [Web UI & Monitoring](web-ui.md). Override with `--webui-port`; disable with `--no-webui`. |

### Domain targeting

Two top-level keys retarget Arbor to a domain without touching code:

```yaml
plugin: mle_kaggle              # load a bundled domain plugin
plugin_profile: mle_bench_lite  # pick a named budget/behaviour profile within it
```

See [Plugins](plugins.md) for the full plugin format and the built-in `mle_kaggle` plugin.

## When settings disagree: precedence

Configuration comes from several places. When two set the same value, the higher one wins:

```text
built-in defaults  <  plugin overrides  <  plugin profile  <  global setup (~/.arbor)  <  project config  <  CLI flags
```

!!! info "Rule of thumb"
    A CLI flag beats everything. Your project config beats your global setup. Set durable
    choices in a file; use flags for one-off changes.

## Verify it works

```bash
arbor config show   # confirm provider/model/key are what you expect (secrets masked)
arbor doctor        # check PATH, Python, git, and that your API key resolves
```

`arbor doctor` is the fastest way to catch a missing key or unreachable gateway before a
run starts.
