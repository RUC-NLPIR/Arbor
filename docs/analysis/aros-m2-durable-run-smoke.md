# AROS M2 Durable Run Smoke Evidence

This document records the real acceptance evidence for the M2 durable-run
kernel. It does not claim evaluator integrity, child isolation or scientific
interpretation; those remain later modules in the Design Book sequence.

## Implemented contract

- `RunService` freezes a versioned `runs/<run-id>/manifest.json` before launch.
- Per-key and per-run `flock` guards prevent duplicate manifests and duplicate
  tmux process launch.
- tmux carries an independent `python -m arbor.aros.runner`; neither the CLI nor
  Principal coroutine owns the experiment lifetime.
- `.aros/runs/<id>/status.json` contains bounded live process observations;
  stdout/stderr and attributed stop requests remain runtime data.
- `runs/<id>/final.json` is the immutable terminal authority and binds the
  manifest hash, prelaunch receipt hash, exact argv/cwd/base commit, process
  outcome, output hashes, duration and stop lineage.
- Reconciliation prefers the final receipt, verifies PID start tokens, reports
  absent process plus missing final as `lost`, restores a missing completion
  event, and fails closed on contradictory/corrupt evidence.
- Native Principal exposes one action-based `Run` system call. CLI exposes
  `arbor aros run start|status|list|tail|stop` over the same service.
- `boot` and `status` derive a bounded inventory from manifests/runtime/finals;
  `runs/ACTIVE.md` is not a source of process truth.

M2 deliberately uses `security_profile=trusted-local`. It fingerprints only an
explicit safe environment allowlist; it does not claim sandbox isolation or
secret protection for the launched command.

## Automated verification

```text
M0-M2 targeted tests: 65 passed
Full suite: 548 passed, 6 skipped
Ruff: passed
runner module launch check: passed
git diff --check: passed
```

The suite includes real tmux processes and covers concurrent prepare/start,
idempotency conflict, manifest tamper, clean-Git/cwd containment, client-state
loss, success, nonzero exit, timeout, attributed stop, ignored SIGTERM followed
by SIGKILL, PID reuse defense, lost reconciliation, missing-event recovery,
contradictory final/process evidence, log hashes and safe environment
fingerprinting.

A read-only Claude review against the Design Book found two pre-commit issues:
a crash after persisting a stop request could make retry conflict on its new
timestamp, and cross-process hash/PID helpers were duplicated between service
and runner. The stop request now compares stable semantic identity and resumes
the same request; shared manifest, environment and process-identity contracts
now live in `store.py`. Both paths have regression tests.

## Real Principal and cross-client smoke

The retained local workspace is `.worktree/aros-m1-smoke`; its versioned M2 run
records are committed at `395e877`.

1. A real `gpt-5.4-mini` native Principal invoked the `Run` tool and started
   `RUN-20260801-083207-principal-survival-3af7`; it returned the stable run ID
   without polling. The independent runner completed and sealed stdout containing
   `M2_BEGIN` and `M2_END`.
2. A separate CLI process started
   `RUN-20260801-083237-cli-survival-e0bd`. Reusing the exact idempotency key and
   manifest later returned the same completed run; run-directory count remained
   unchanged at three.
3. A third CLI process started
   `RUN-20260801-083254-stop-survival-bb60`. After the launcher exited, a fresh
   process observed `state=running` and tailed `STOP_READY`.
4. A human-attributed stop request with reason `M2 attributed stop smoke`
   delivered SIGTERM. The runner sealed `state=cancelled`, exit code `-15`, the
   PID start token, stop actor/reason/signal sequence, launch lineage and output
   hashes.
5. A fresh `arbor aros boot` discovered two completed and one cancelled run from
   operational truth, without transcript or in-memory service state.

## Evidence hashes

```text
principal manifest  2d4cf1161db681355fe20a78b05e5118c543b0baec0f5e6e08499a4c9a855e35
principal final     cc367194a8541221114c7b7ad5ea055a4ea19fad262b75e809d7aabc410f4690
CLI manifest        6b889951cf776d2bf0dcfefcf105f1e289eaf10fc0944e28b272642543a2cadc
CLI final           4b1b414d9472ad13dbe26e069c1a880b6eaf0e34764d408ddc6c06296d854998
stopped manifest    8142afb173deaab5a93c5b61447dd1f6ec53e4b029a327ee3a7da1e4a1444e65
stopped final       db5646420321f95fc060fe94ba36e8b23a71a3120b153ffb62d8f21a4e372daa
```

## Immutable terminal projection repair

The 2026-08-04 repair found that terminal validation had treated mutable
`.aros/runs/<id>/status.json` as a co-authority: otherwise valid immutable
final and output evidence could be invalidated by a missing, stale or forged
status, and Eval audit needed a special validation bypass. Terminal authority
is now the Run manifest, create-once prelaunch receipt and immutable final.
Mutable status is only a reconstructible operational projection.

For a terminal Run with valid immutable lineage, `status`, `start` and
`reconcile` deterministically repair all tested mutable projections: missing,
stale `launched`, stale `lost`, and forged terminal status. The exact
16-field projection is derived from manifest + prelaunch + final:

```text
schema_version, run_id, state, manifest_sha256,
actor, carrier, tmux_session, host, launch_receipt_sha256, launched_at,
started_at, exit_code, finished_at, heartbeat_at, final_ref, updated_at
```

The launch-failure path also derives its terminal final and status from the
manifest and prelaunch receipt. Forged mutable actor, host and launch-hash
fields cannot enter that projection. Conversely, invalid immutable manifest,
prelaunch or final evidence fails closed before any status rewrite.

`read_validated_final`, `read_verified_output` and `verify_output` no longer
depend on mutable status and do not recreate it. Eval audit keeps its separate
status observation at `reconcile=False`: a missing status is reported against
its own reference, while immutable final, stdout and stderr remain in
`checked_refs` and validate independently. The regression snapshots every
remaining file's relative path, inode and bytes, proves the status remains
absent, and therefore proves audit does not rebuild status, final, measurement
receipt or an Eval retry.

The change sequence is recorded by these commits:

| Commit | Evidence/change |
| --- | --- |
| `a614ec3` | scope and ordering checkpoint for the terminal projection repair |
| `2a87d0d` | tests exposing mutable terminal-status co-authority |
| `30d5872` | tests for direct repair of missing/stale/forged projections |
| `57be2c7` | tests separating launch actor authority from mutable status |
| `01a72f0` | Run terminal projection derived from immutable receipts |
| `c6b80ee` | read-only Eval audit kept independent of missing Run status |
| `05aa41d` | carrier failure preserves an already sealed Runner final |
| `a9ee500` | loser-path event writer projects the create-once final winner |

Before the final create-once loser-race regression, implementation gates
reached `92 passed` for `tests/test_aros_runs.py` and `383 passed in 64.98s`
for `tests/test_aros_runs.py`,
`tests/test_aros_eval.py` and `tests/test_aros_eval_records.py`. The focused
missing-status Eval audit gate reached `2 passed`; Ruff and both Git diff gates
were clean. No `uv` command was invoked and no commissioning receipt was
rewritten.

Documentation commissioning on the `c6b80ee` implementation baseline then
reached `231 passed (exit 0)` for its architecture/public-entry/registry gate.
After the final carrier-race evidence, the current tree reached `93 passed in
21.17s` for Run, `384 passed in 63.98s` for the Run/Eval/Eval-records gate, and
`244 passed (exit 0)` for the final documentation/architecture gate.
The final exact unqualified `/workspace/Arbor/.venv/bin/python -m pytest`
command reached `1641 passed, 6 skipped in 350.36s (0:05:50)`. Full
`ruff check src tests scripts` reported `All checks passed!`; both
`git diff --check` and `git diff --exit-code -- uv.lock` exited 0. The full gate
ran on the clean `a9ee500` code/test/evidence tree; subsequent evidence-only
documentation commits did not rewrite an operational or measurement receipt.

## Exit result

M2 proves that a real AROS Principal or CLI can launch a process that survives
the launching client, can be recovered and controlled by a fresh process, and
terminates with immutable, attributable operational evidence. No field in the
run final is interpreted as a scientific result.
