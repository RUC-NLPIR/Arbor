# Current State

## Mission

Implement and validate Agent-centric AROS while preserving the Design Book
invariants, then retire Arbor only after equivalent public capabilities are
commissioned.

## Current position

- Branch: `aros-wave2-child` in `/workspace/Arbor/.worktree/aros-wave2-child`.
- Code baseline immediately before this checkpoint: `c5f58d62dd34470948766a1ee19335bf7eba325d`.
- Wave 2 Tasks 1–5 are implemented and independently reviewed.
- Real commissioning created two initial task briefs/worktrees, but their first
  launch was preserved at `worktree_ready` because the workspace FUSE mount
  cannot express mode `0600`. The Task runner now records this filesystem
  capability explicitly for trusted-local execution; those original tasks are
  not retried.
- Generic create-once JSON publication now writes complete temp content before
  atomic no-replace publication and recovers exact interrupted aliases.

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

1. Resume Wave 2 commissioning with new task IDs and idempotency keys.
2. Demonstrate concurrent read/write children, B-C-R returns, explicit rejection,
   dirty preservation, clean prune, and public CLI receipts.
3. Publish exact smoke evidence, run the Wave 2 full gate, and merge only after
   final whole-wave review.
4. Continue Eval, Operations, semantic K/M/G/Skills, MCP/provider parity, and
   Arbor retirement waves.
