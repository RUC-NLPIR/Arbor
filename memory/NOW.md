# Current State

## Mission

Implement and validate Agent-centric AROS while preserving the Design Book
invariants, then retire Arbor only after equivalent public capabilities are
commissioned.

## Current position

- Branch: `aros-wave2-child` in `/workspace/Arbor/.worktree/aros-wave2-child`.
- Code baseline immediately before this checkpoint: `5254cc159986d6c9eed79878356d9ab564979ab1`.
- Wave 2 Tasks 1–6 are implemented, independently reviewed, and commissioned.
- Registered evidence: `docs/analysis/aros-wave2-child-substrate-smoke.md`.
- Real commissioning demonstrated concurrent read/write children, launcher-exit
  survival, ordered messages, B-C-R returns, explicit rejection without
  assimilation, dirty preservation, clean prune, and retained task branches.
- The first two task launches remain preserved at `worktree_ready` because they
  exposed that the workspace FUSE mount cannot express mode `0600`. The Task
  runner now records this filesystem capability explicitly for trusted-local
  execution; those original tasks were not retried.
- Generic create-once JSON publication now writes complete temp content before
  atomic no-replace publication, recovers exact interrupted aliases, and fsyncs
  existing/relative directory chains.
- Worktree ownership is non-expiring and can be released only by explicit clean
  prune. The create-once execution claim is the local one-attempt execution
  lease; dead holders become `lost` and never transfer ownership or relaunch.
- Final Wave 2 verification reached `1133 passed, 6 skipped`; architecture,
  public-entry, registry, maintained Ruff, diff, and lock gates are green.

## Preserved operational evidence

Two independent isolated-linux RunService inspections attempted to observe the
original task worktrees and reached factual `failed_process` because process
launch raised `Exception occurred in preexec_fn`:

- `runs/RUN-20260803-075733-check-inspect-task-worktree-8849/`
- `runs/RUN-20260803-075735-check-write-task-worktree-b583/`

These failures are process evidence, not scientific negatives. Their manifests
and final receipts are retained for the later shared Run/Task process-core
consolidation.

## Next obligations

1. Complete the final whole-wave re-review and merge Wave 2 while retaining all
   negative and positive receipts.
2. Continue Eval, Operations/shared process core, semantic K/M/G/Skills,
   MCP/provider parity, and Arbor retirement waves.
