# Arbor Codex Overlay

This file is the repo-local Codex overlay for Arbor.

Global Codex safety rules live in `~/.codex/AGENTS.md`. Use
[CLAUDE.md](CLAUDE.md) as the primary repo-level guide. This file only adds the
Codex-specific deltas that matter in this repo.

## Workspace Focus

- Inspect `src/cli/`, `src/coordinator/`, `src/executor/`, `src/core/`,
  `src/webui/`, `skills/`, and `docs/` before making Arbor behavior or interface
  changes.
- Treat this checkout as the WSL-local Arbor clone at
  `/home/user01/agent-workspace/Arbor`.
- Do not run model-provider calls, paid APIs, long experiments, or final test
  evaluation unless the user explicitly authorizes that work in the current
  turn.

## Shared Repo Guidance

- Follow [CLAUDE.md](CLAUDE.md) for commands, verification, and Arbor-specific
  guardrails.
- Keep Arbor edits inside this nested git repo. The parent
  `/home/user01/agent-workspace` repo should track only control-plane files, not
  the contents of this checkout.

## Verification

- Prefer `uv run pytest`, `uv run ruff check .`, and `uv run arbor doctor` for
  focused Codex verification.
- Use `mkdocs build` when documentation navigation or rendered docs change.
