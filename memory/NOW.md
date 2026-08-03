# Current State

## Mission

Implement and validate Agent-centric AROS while preserving the Design Book
invariants, then retire Arbor only after equivalent public capabilities are
commissioned.

## Current position

- Branch: `aros-wave2-child` in `/workspace/Arbor/.worktree/aros-wave2-child`.
- Final post-review code baseline: `114da5959ae39be4b6977550ab291f9045d23679`.
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
- Interrupted prune recovery is intent-bound and targeted: at the removal
  midpoint it may complete only the exact prunable worktree registration bound
  by the persisted prune intent. It rejects unrelated prunable registrations
  and never invokes global `git worktree prune`.
- Task adapters are trusted-local and application-scoped, not a security
  sandbox. Network and shell capability flags are audit declarations and are
  not enforced. Secrets and untrusted adapters are unsupported. Daemonizing or
  new-session descendants that do not drain fail closed as `lost` with no
  terminal receipt.
- V1 terminal truth covers the exact PGID plus descendants reparented to the
  live subreaper. A new-session process that outlives runner death is not
  claimed contained and cannot justify a clean final receipt or prune.
  Delegated per-task cgroups belong to the shared Operations process core, not
  the Wave 2 security claim.
- The `254f754a122bcaea6852abc55480eff339cbe889` post-containment receipts
  remain valid for that code but are superseded for current verification.
- Fresh verification at the exact final post-review baseline reached
  `1177 passed, 6 skipped in 260.91s`; Tasks reached `258 passed in 122.38s`,
  task runner reached `75 passed in 111.16s`, architecture/public-entry/registry
  reached `227 passed in 9.61s`, and TaskTool/CLI/Principal reached
  `46 passed in 0.93s`. Maintained Ruff, diff, and
  working-tree/commissioning-baseline lock gates are green.

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

1. Complete the final whole-wave review and integration while retaining all
   negative and positive receipts.
2. Consolidate Run/Task into the shared Operations process core, including
   delegated per-task cgroups for runner-death containment, then continue Eval.
3. Complete the still-open Phase 3 provider profiles, Principal lease, and
   `child_done` event work, followed by semantic K/M/G/Skills, MCP/provider
   parity, and Arbor retirement waves.
