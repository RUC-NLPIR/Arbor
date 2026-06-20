# Arbor Interface Map

This page maps the operational interfaces exposed by the Arbor checkout at
`/home/user01/agent-workspace/Arbor`. It is written for local onboarding and
verification, not as a product overview.

## What The Interface Is

Arbor has two main ways to drive work:

| Interface         | Entry point           | Purpose                                                                                                                                 | Provider boundary                                                                                       |
| ----------------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Native runtime    | `arbor` / `arbor run` | Runs Arbor's intake agent, coordinator, executor worktrees, terminal dashboard, Web UI, checkpoints, reports, plugins, and resume flow. | Uses Arbor's configured LLM provider. Do not run real sessions unless provider/API use is authorized.   |
| WSL local adapter | `arbor local`         | Runs the public Arbor skill suite through the installed WSL `claude` or `codex` CLI.                                                    | Does not use Arbor provider setup or `~/.arbor/config.yaml`; delegates to the selected local agent CLI. |

The native runtime is the full Arbor system. The local adapter is a WSL-only
bridge for using the `skills/arbor-*` suite from this checkout without
configuring Arbor's native provider clients.

## Run Locally From This Clone

From the Arbor checkout:

```bash
cd /home/user01/agent-workspace/Arbor
uv sync --group dev
uv run arbor --help
```

For local onboarding without provider calls, prefer the no-model-call surfaces:

```bash
uv run arbor --help
uv run arbor run --help
uv run arbor local --help
uv run arbor local doctor
uv run arbor local run --agent codex --cwd /home/user01/agent-workspace/Arbor --dry-run \
  "summarize the Arbor interface without changing files"
```

`arbor local` requires WSL and WSL-native paths. The Arbor checkout, target
repository, skill source, and installed skill destination must stay under Linux
paths such as `/home/user01/...`; paths under `/mnt/c` are rejected.

For a native provider-backed research run, configure credentials first:

```bash
uv run arbor setup
uv run arbor "maximize dev score without changing eval or data"
```

Native `arbor run` constructs provider clients during intake and coordinator
startup, so it can contact Anthropic, OpenAI, or an OpenAI-compatible gateway.

## CLI Surfaces

The top-level console script is configured in `pyproject.toml`:

| Script            | Python entry point           | Role                               |
| ----------------- | ---------------------------- | ---------------------------------- |
| `arbor`           | `arbor.cli.app:main`         | Primary Typer CLI.                 |
| `executor`        | `arbor.executor.main:cli`    | Low-level executor entry point.    |
| `coordinator`     | `arbor.coordinator.main:cli` | Low-level coordinator entry point. |
| `run-research`    | `arbor.run:cli`              | Legacy/advanced run entry point.   |
| `review-research` | `arbor.review:cli`           | Review helper entry point.         |

The primary `arbor` commands are:

| Command         | Purpose                                                                                                                                                         |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `arbor`         | With no command, dispatches to `arbor run`.                                                                                                                     |
| `arbor run`     | Starts intake, confirms the Research Contract, runs preflight, launches the coordinator, writes session artifacts, and optionally opens a follow-up Q&A prompt. |
| `arbor setup`   | Writes user LLM defaults to `~/.arbor/config.yaml`.                                                                                                             |
| `arbor config`  | Shows, initializes, or prints the path for user config.                                                                                                         |
| `arbor doctor`  | Checks install path, Python, importability, git, and provider config/API key presence.                                                                          |
| `arbor report`  | Re-renders a finished run report.                                                                                                                               |
| `arbor export`  | Exports a finished session to HTML or JSONL.                                                                                                                    |
| `arbor local`   | WSL-native adapter for the Arbor skill suite.                                                                                                                   |
| `arbor version` | Prints the installed package version.                                                                                                                           |

`arbor local` contains:

| Subcommand                         | Purpose                                                                                                                                 |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `arbor local doctor`               | Checks the WSL-native Arbor checkout, local `skills/` tree, and `claude`/`codex` CLI availability.                                      |
| `arbor local install --agent both` | Copies `skills/arbor-*` into user skill directories such as `~/.claude/skills` and `~/.codex/skills` (or `CODEX_HOME/skills`).          |
| `arbor local run`                  | Builds a prompt that points the selected local CLI at `skills/arbor-research-agent/SKILL.md` and runs `claude --print` or `codex exec`. |

Use `--dry-run` with `arbor local run` when you only need to inspect the command
that would be launched.

## Web UI And Runtime Views

Interactive native runs expose two live views over the same run state:

| Surface            | Implementation                                                         | Notes                                                                                              |
| ------------------ | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Terminal dashboard | `src/cli/run_dashboard.py`                                             | Shows cycle state, Idea Tree, scores, token usage, companion turns, and slash-command interaction. |
| Browser monitor    | `src/webui/server.py`, `src/webui/launcher.py`, `src/webui/index.html` | Starts on `127.0.0.1`, defaulting near port `8765` for interactive TTY runs.                       |

The browser monitor is a small local HTTP server with these routes:

| Route      | Method | Purpose                                                                                    |
| ---------- | ------ | ------------------------------------------------------------------------------------------ |
| `/`        | GET    | Serves the packaged Web UI HTML.                                                           |
| `/events`  | GET    | Server-Sent Events stream carrying snapshots and event-bus frames.                         |
| `/healthz` | GET    | Health check returning `ok`.                                                               |
| `/input`   | POST   | Optional token-gated browser input for ask, steer, and gate actions when input is enabled. |

The Web UI binds to `127.0.0.1` by default. When browser input is enabled, Arbor
adds a per-run token to the printed URL and requires that token for `/input`.

Control flags:

```bash
uv run arbor --webui-port 9000
uv run arbor --no-webui
uv run arbor --no-dashboard-input
```

`ARBOR_DASHBOARD_INPUT_MODE=raw|line` selects legacy terminal input modes, and
`ARBOR_FORCE_DASHBOARD_INPUT=1` forces raw input back on. These are escape
hatches for terminal behavior, not normal setup knobs.

## State And Data Flow

Native runs write durable artifacts under the target project, not under the
Arbor source checkout by default:

```text
<target-project>/.arbor/sessions/<run_name>/
```

Important artifacts include:

| Artifact                            | Purpose                                          |
| ----------------------------------- | ------------------------------------------------ |
| `.coordinator/config_snapshot.yaml` | Fully resolved run config with secrets redacted. |
| `events.jsonl`                      | Event stream used by reports and live views.     |
| `REPORT.md`                         | Final human-readable report.                     |
| `COORDINATOR_FINAL_REPORT.txt`      | Raw coordinator final report when available.     |
| Checkpoint/message/tree files       | Resume state for interrupted or continued runs.  |

The coordinator emits events through `arbor.events.EventBus`. The terminal
dashboard and Web UI consume that same bus, while `RunState` holds the live
snapshot flattened by `src/webui/snapshot.py`.

Experiment implementation work happens in isolated git worktrees and branches
managed by coordinator/executor tools. The target benchmark repo should be a
normal git checkout on the WSL filesystem.

## Configuration And Environment

Configuration layers are resolved in `src/core/config_resolve.py` and validated
by the Pydantic models in `src/core/config_schema.py`.

Precedence is:

```text
pydantic defaults
< plugin config overrides
< active plugin profile
< user/project YAML
< CLI overrides
```

Common files and environment variables:

| Surface                                                   | Purpose                                                                        |
| --------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `~/.arbor/config.yaml`                                    | User-level LLM/default config written by `arbor setup` or `arbor config init`. |
| `~/.autoresearch/config.yaml`                             | Legacy fallback config path.                                                   |
| `research_config.yaml`, `arbor.yaml`, `autoresearch.yaml` | Project config names auto-detected in the target repo.                         |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`               | Anthropic credentials used by native provider-backed runs.                     |
| `OPENAI_API_KEY`                                          | OpenAI/OpenAI-compatible credential used by native provider-backed runs.       |
| `CODEX_HOME`                                              | Overrides the Codex user directory used by `arbor local install`.              |
| `WEB_SEARCH_ENDPOINT`, `WEB_BROWSE_ENDPOINT`              | Optional web-search/browse endpoints for search-agent tooling.                 |
| `ARBOR_CRASH_LOG`                                         | Optional path for terminal dashboard crash logs.                               |

Secrets are redacted from config snapshots and WebUI-visible config dumps by the
schema helpers in `src/core/config_schema.py`.

## API Boundaries

Arbor has no always-on backend service. The only HTTP surface in normal local
use is the per-run Web UI server started by a native run.

Provider/API boundaries are:

| Boundary                       | Native runtime                                                | WSL local adapter                               |
| ------------------------------ | ------------------------------------------------------------- | ----------------------------------------------- |
| Anthropic/OpenAI/LiteLLM calls | Yes, through `create_provider()` and the configured provider. | No direct Arbor provider calls.                 |
| Local agent CLI process        | No, unless a tool/run explicitly launches one.                | Yes, launches `claude --print` or `codex exec`. |
| Web search/browse endpoints    | Optional tool endpoints when configured.                      | Not used by the adapter itself.                 |
| Web UI HTTP server             | Per native run, bound locally.                                | Not started.                                    |

For no-paid-API onboarding, use `--help`, `doctor`, docs builds, unit tests, and
`arbor local ... --dry-run`; do not run provider-backed native sessions.

## Build, Test, And Verification

Recommended checks from this checkout:

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv run arbor local doctor
uv run arbor doctor
mkdocs build
```

Notes:

- `uv run arbor local doctor` should be the first WSL-adapter smoke test.
- `uv run arbor doctor` may warn or exit non-zero for a project-local `.venv`
  install; that is useful install guidance, not a provider call.
- `mkdocs build` verifies the documentation navigation and rendered Markdown.
- Native `arbor run` is intentionally not part of no-paid-API verification.

## Known Gaps And Risks

- `arbor local` is WSL-only and rejects mounted Windows paths. This is
  intentional to keep Arbor, target repos, generated `.arbor/` artifacts, and
  agent CLIs on the Linux filesystem.
- `arbor local install` copies skill directories into user/project skill roots;
  restart Claude Code or Codex before relying on installed skills.
- `arbor doctor` diagnoses global install readiness. In this local checkout it
  can flag the `.venv` install even when `uv run arbor ...` works.
- The Web UI is per-run local state, not a durable API. Durable evidence lives
  in `.arbor/sessions/<run_name>/`.
- Full native-run validation requires configured provider credentials and can
  consume paid API credits, so it is outside no-permission onboarding.
