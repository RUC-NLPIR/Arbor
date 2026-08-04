# Current State

## Mission

Implement and validate Agent-centric AROS while preserving the Design Book
invariants, then retire Arbor only after equivalent public capabilities are
commissioned.

## Current position

- Branch: `aros-run-terminal-projection` in
  `/workspace/Arbor/.worktree/aros-run-terminal-projection`.
- Current implementation baselines are deliberately separated: immutable Run
  terminal projection is `01a72f07ed6bc88fa6d7bd5fbe1527d985ffe558`;
  read-only Eval audit compatibility is
  `c6b80ee2a5b8df183d1c92b51d525b43d246a47c`; the final create-once
  carrier-race evidence is `a9ee500379107d9f777f5b57b11917b5a472429b`.
- The guarded Task carrier work is merged on `main` at
  `904f7ae2786ce7f57c9d987c7d6c4608f7d37dc2`; this branch descends from that
  baseline.
- Visible Eval remains commissioned at code baseline
  `d5cbc7ec4ddd4b677e55df341912394d32e55846`; its post-hardening evidence
  checkpoint is `25db6fe7f6e24a703e2319783d2466fe59a98f4e`.
- Wave 3 visible evaluation is implemented and commissioned through the direct
  `aros eval register|run|status|observe|audit` surface. Registered evidence:
  `docs/analysis/aros-wave3-visible-eval-smoke.md`.
- Eval composes the durable Run service and separate exact candidate/apparatus
  checkouts. It owns no second process, tmux, final-process, or recovery stack.
- Real commissioning produced a `valid` scalar measurement from distinct exact
  candidate/apparatus commits, proved clean removal, and proved direct factual
  status, bounded observe, and lineage audit.
- Post-hardening recommissioning proved that every stage-0 candidate/apparatus
  index blob OID matched an OID recomputed from raw checkout bytes before clean
  removal. The current exact Run again produced one valid measurement and a
  valid audit; the original dirty/lost receipts remain pre-hardening behavior
  evidence rather than proof of this raw-byte verifier.
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
- Run terminal reads now use manifest + create-once prelaunch + immutable final
  as authority. Mutable terminal status is a deterministic 16-field
  projection repaired by `status`, `start`, or `reconcile`; immutable
  final/output readers do not require or recreate it.
- Eval audit observes Run status with reconciliation disabled. Missing mutable
  status is an independent issue and does not prevent immutable final/log
  validation; audit does not reconstruct status, final, measurement or retry.
- Fresh current-branch gates reached `92 passed` for Run, `383 passed in
  64.98s` for the Run/Eval/Eval-records combination, `231 passed (exit 0)` for
  architecture/public-entry/registry, and `1641 passed, 6 skipped in
  350.36s (0:05:50)` for the final exact unqualified full pytest command. Full
  Ruff reported `All checks passed!`; diff-check and the `uv.lock` comparison
  both exited 0. No `uv` command ran and no commissioning receipt changed.
- Wave 2 child-task evidence remains current. Exact carrier probing and the
  per-task OFD guardian close the reviewed Task startup-loss intervals without
  retry; Task adapters remain trusted-local, application-scoped, and not a
  security sandbox. Their containment and filesystem-permission limits are
  unchanged.
- Protected registration/admission, disclosure budgets, migration adapters,
  MCP parity, and Arbor retirement remain unavailable.
- This repair does not commission all Phase 3 exits, Phase 4, or the complete
  AROS target.
- The Phase 3 sequence deviation is already recorded in the current
  implementation baseline; it is not a pending registration action.

## Preserved operational evidence

- The ignored commissioning repository retains exact v1 failure, v2 success,
  dirty, and lost Run records. All commissioning execution bundles were removed
  only through clean validation; no unexpected dirty material was discarded.
- The two original Wave 2 task-worktree inspection failures and their receipts
  remain historical process evidence for shared Run/Task process-core work, not
  scientific negative results.

## Next obligations

1. Integrate the immutable Run projection and Eval audit repair.
2. Implement and commission protected registration/admission as a separate
   Gate C-D change; do not infer it from visible evidence.
3. Consolidate Run/Task into the shared Operations process core, including
   delegated per-task cgroups for runner-death containment.
4. Complete provider profiles, Principal lease, `child_done`, semantic
   K/M/G/Skills, MCP/provider parity, migration, and Arbor retirement waves.
