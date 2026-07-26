# Testing the Docker image

`docker/test_mcp.py` is a self-contained integration test that verifies the
containerized arbor MCP server end-to-end. It spawns the container as a
subprocess, speaks MCP over stdio, and drives the full research-loop primitive
set (tree, eval, worktree, merge, prune, report, dashboard) with assertions.

## Prerequisites

```bash
docker compose build          # from the repo root
```

The test uses a **dummy API key** — none of the MCP tools in the loop call the
LLM (`eval_run` runs your `eval.py`; `generate_report` renders from durable
artifacts). So no provider credentials are needed.

## Run

```bash
python docker/test_mcp.py
```

Expected: `14/14 checks passed`.

## What it verifies

The test builds a throwaway toy benchmark under `/tmp/arbor-mcp-test-ws/`,
mounts it into the container as `/workspace`, and runs three use cases:

### UC1 — baseline eval
Sets session metadata (`eval_cmd`, `metric_direction`, `trunk_branch`), runs the
baseline eval on dev and test splits, and confirms the tree records
`baseline=19`. Tools: `tree_set_meta`, `eval_run`, `tree_view`.

### UC2 — hypothesis → experiment → merge
Adds a hypothesis node, creates an isolated git worktree, edits `LEARNING_RATE`
to `0.1` (the peak), evals the experiment (score 100), then merges the
experiment branch into `trunk` — after a dry-run guard check. Tools:
`tree_add_node`, `worktree_create`, `eval_run`, `git_merge_branch`,
`tree_update_node`.

### UC3 — bad hypothesis → prune → report → dashboard
Adds an overshoot hypothesis (`LR=1.0`, score −8000), evals it, prunes it,
generates `REPORT.md`, and opens the read-only dashboard. Tools: `tree_prune`,
`generate_report`, `open_dashboard`.

## The fixture

`docker/test-bench/` holds the toy benchmark the test copies into the workspace:

- `solution.py` — the edit surface. `solve()` returns a deterministic score that
  peaks at `LEARNING_RATE=0.1` (baseline `0.01` → 19, peak `0.1` → 100,
  overshoot `1.0` → −8000).
- `eval.py` — protected harness printing `score: <float>` (the contract
  `eval_run` parses).

## Merge isolation (safety)

**No real repository is touched.** All git operations — worktrees, commits,
merges — happen inside the throwaway toy repo at `/tmp/arbor-mcp-test-ws/test-bench`.
Merges target only its non-protected `trunk` branch (arbor refuses to merge into
the protected `main`). The workspace and the container are removed in a
`finally` block when the test exits.

## CI

The test is plain Python (no pytest dependency) and exits non-zero on any
failed assertion, so it can run as a single step:

```yaml
- run: docker compose build
- run: python docker/test_mcp.py
```
