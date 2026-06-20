# CLAUDE.md

Primary repo-level operational guidance for coding agents working in Arbor.

Arbor is an autonomous research-agent runtime and skill suite. It turns a
research or benchmark objective into hypothesis-tree exploration, executor
worktrees, experiment evidence, and merge decisions.

## Repo Focus

- `src/`: Python package imported as `arbor`; CLI, coordinator, executor, event
  bus, plugins, reports, search agent, and WebUI code live here.
- `skills/`: Codex and Claude Code skill suite for Arbor-style workflows.
- `docs/` and `mkdocs.yml`: user-facing documentation site.
- `project_page/`: separate project-page frontend.
- `examples/`: runnable benchmark examples.
- Generated or local runtime outputs such as `.venv/`, `site/`,
  `.pytest_cache/`, `.ruff_cache/`, and `arbor_agent.egg-info/` are not primary
  edit targets.

## Key Commands

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
uv run mypy src
uv run arbor doctor
```

For source installs without uv, use a virtual environment and `pip install -e .`.
For docs changes, install docs dependencies and run `mkdocs build`.

## High-Value Guardrails

- Keep Arbor and target benchmark repos on the WSL/Linux filesystem. The
  `arbor local` adapter refuses `/mnt/c` and other mounted Windows paths.
- Do not call model-provider APIs, install paid services, run long experiments,
  or touch final test splits unless the current task explicitly authorizes it.
- Prefer the native Arbor CLI for real research runs; use `skills/` as the
  Codex/Claude integration layer.
- When adding a new Python subpackage under `src/`, update the explicit
  `packages` list in `pyproject.toml`.
- Preserve isolated experiment discipline: Arbor executor work should happen in
  dedicated git worktrees and branches, not directly on `main`.

## Verification

- Python changes: `uv run pytest` and `uv run ruff check .`.
- Typing-sensitive changes: `uv run mypy src`.
- CLI/runtime changes: `uv run arbor doctor`.
- Docs changes: `mkdocs build`.

## Hotspots

- `src/cli/`: Typer command surface, intake flow, dashboard/local adapter.
- `src/coordinator/`: hypothesis tree and orchestration.
- `src/executor/`: experiment implementation and evaluation worker.
- `src/core/`: provider config, shared runtime tools, LLM boundaries.
- `src/webui/`: read-only browser monitor.
- `skills/README.md`: skill-suite entrypoint and install expectations.
