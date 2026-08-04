# Current State

## Mission

Implement and validate Agent-centric AROS while preserving the Design Book
invariants, then retire Arbor only after equivalent public capabilities are
commissioned.

## Current position

- Branch: `aros-wave3-eval` in
  `/workspace/Arbor/.worktree/aros-wave3-eval`.
- Commissioned visible-Eval code baseline:
  `265aee93808d533d51005790e242c0222612ed10`.
- Wave 3 visible evaluation is implemented and commissioned through the direct
  `aros eval register|run|status|observe|audit` surface. Registered evidence:
  `docs/analysis/aros-wave3-visible-eval-smoke.md`.
- Eval composes the durable Run service and separate exact candidate/apparatus
  checkouts. It owns no second process, tmux, final-process, or recovery stack.
- Real commissioning produced a `valid` scalar measurement from distinct exact
  candidate/apparatus commits, proved clean removal, and proved direct factual
  status, bounded observe, and lineage audit.
- External candidate dirt changed a completed process result to
  `measurement_state=invalid_eval`; both checkouts were preserved until the
  injected bytes were recorded, restored, and removed by the clean-only helper.
- Killing only the foreground Eval broker made the request permanently `lost`
  while the linked Run remained independently observable. Same-key replay
  created no Run; a new key created exactly one new Eval/Run. Explicitly stopped
  Runs became `cancelled`, and no measurement receipt was reconstructed.
- A schema-valid absolute virtual-environment launcher failed under
  `isolated-linux` because its outside-bundle `pyvenv.cfg` was unreadable. That
  attempt remains factual `failed_process/not_available` evidence and was not
  retried. The commissioned descriptor uses fixed `python3`; no isolation
  policy was weakened.
- Fresh verification reached `422 passed` in the exact visible-Eval module
  gate and `1594 passed, 6 skipped` in the whole suite. Ruff, registry, diff,
  and `uv.lock` gates are clean. Independent spec and quality/security reviews
  approved with no remaining Critical or Important findings.
- Wave 2 child-task evidence remains current. Task adapters are trusted-local,
  application-scoped, and not a security sandbox; their exact containment and
  filesystem-permission limits are unchanged.
- Protected registration/admission, disclosure budgets, migration adapters,
  MCP parity, and Arbor retirement remain unavailable.

## Preserved operational evidence

- The ignored commissioning repository retains exact v1 failure, v2 success,
  dirty, and lost Run records. All commissioning execution bundles were removed
  only through clean validation; no unexpected dirty material was discarded.
- The two original Wave 2 task-worktree inspection failures and their receipts
  remain historical process evidence for shared Run/Task process-core work, not
  scientific negative results.

## Next obligations

1. Implement and commission protected registration/admission as a separate
   Gate C-D change; do not infer it from visible evidence.
2. Consolidate Run/Task into the shared Operations process core, including
   delegated per-task cgroups for runner-death containment.
3. Complete provider profiles, Principal lease, `child_done`, semantic
   K/M/G/Skills, MCP/provider parity, migration, and Arbor retirement waves.
