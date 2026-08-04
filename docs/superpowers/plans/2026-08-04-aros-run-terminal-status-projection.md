# AROS Run Terminal Status Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a valid immutable Run final authoritative when mutable `status.json` is missing or stale, while keeping audit read-only and fail-closed on invalid immutable lineage.

**Architecture:** `manifest.json`, the create-once prelaunch receipt, and `final.json` are the only inputs to one deterministic terminal-status projection. Normal `status()`/`reconcile()` may atomically repair the mutable projection; `reconcile=False`, final validation, output verification, and Eval audit never repair it. No new lifecycle service, receipt schema, retry path, or process authority is introduced.

**Tech Stack:** Python, pytest, atomic JSON store, existing RunService/EvalService receipts.

---

### Task 1: Specify immutable terminal authority with failing Run tests

**Files:**
- Modify: `tests/test_aros_runs.py`

- [ ] **Step 1: Add a missing/stale terminal-status repair test**

Create one real completed Run with `_mark_runner_launched(...)` and `runner_module.run(...)`. Parameterize `status.json` as `missing`, `launched`, `lost`, and a forged terminal state. Assert that `RunService.status(run_id)` returns the same deterministic terminal projection in every case, rewrites `status.json`, and that `RunService.start(run_id)` reattaches without launch.

The expected projection must contain only values reconstructible from immutable records:

```python
{
    "schema_version": 1,
    "run_id": run_id,
    "state": final["state"],
    "manifest_sha256": manifest["manifest_sha256"],
    "actor": prelaunch["actor"],
    "carrier": "tmux",
    "tmux_session": prelaunch["tmux_session"],
    "host": prelaunch["host"],
    "launch_receipt_sha256": prelaunch["receipt_sha256"],
    "launched_at": prelaunch["created_at"],
    "started_at": final["started_at"],
    "exit_code": final["exit_code"],
    "finished_at": final["finished_at"],
    "heartbeat_at": final["finished_at"],
    "final_ref": f"runs/{run_id}/final.json",
    "updated_at": final["finished_at"],
}
```

- [ ] **Step 2: Add independent final validation and fail-closed tests**

Delete `status.json` after a real completed Run and assert `read_validated_final()` and `read_verified_output()` still succeed without recreating status. Then corrupt the immutable final, call normal status, and assert `RunError` while status remains absent.

- [ ] **Step 3: Correct the old mutable-status authority expectation**

Remove `status-mismatch` from `test_validated_final_rejects_forged_prelaunch_provenance`. Add a separate test showing that a forged mutable actor does not invalidate the immutable final and is replaced by the prelaunch actor during normal reconciliation.

- [ ] **Step 4: Run the new tests and verify RED**

Run:

```bash
/workspace/Arbor/.venv/bin/python -m pytest \
  tests/test_aros_runs.py -k 'terminal_status or immutable_final or mutable_status'
```

Expected: failures showing missing status is required by `status()`, `_reconcile_locked()`, and `read_validated_final()`.

### Task 2: Make Run terminal status a deterministic projection

**Files:**
- Modify: `src/aros/runs.py`
- Test: `tests/test_aros_runs.py`

- [ ] **Step 1: Make prelaunch validation independent of status**

Change `_validate_prelaunch_receipt(...)` so `expected_actor` and `expected_host` are optional creation-time checks. Always validate that the receipt itself contains non-empty actor/host, exact manifest/session/invocation lineage, and its self-hash. Remove its `status` argument and all mutable-status comparisons.

- [ ] **Step 2: Make final validation immutable-only**

Remove the `status.json` read and the `for_audit` branch from `read_validated_final()`. Validate:

```text
manifest hash
-> prelaunch schema/hash/manifest/runner lineage
-> final schema/manifest/prelaunch hash/host/output receipts
```

The method must not read or write mutable status.

- [ ] **Step 3: Add one private terminal projection helper**

Add `_terminal_status(manifest, prelaunch, final)` returning exactly the reconstructible fields specified in Task 1. It owns no I/O and no process logic.

- [ ] **Step 4: Reconcile immutable final before mutable status**

In `_reconcile_locked()`, check for `final.json` before requiring `status.json`. Load and validate the immutable records, build the terminal projection, atomically replace a missing/stale status, emit the existing idempotent completion event, and return the projection. When no final exists, preserve the current prepared/active/lost process reconciliation unchanged.

In `status()`, when `reconcile=True` and `final.json` exists, call `reconcile()` before trying to read status. With `reconcile=False`, continue to report a missing/invalid status instead of repairing it.

- [ ] **Step 5: Run Run tests and verify GREEN**

Run:

```bash
/workspace/Arbor/.venv/bin/python -m pytest tests/test_aros_runs.py
```

Expected: all Run tests pass.

### Task 3: Preserve read-only Eval audit semantics

**Files:**
- Modify: `src/aros/eval.py`
- Modify: `tests/test_aros_eval.py`
- Modify: `src/aros/runs.py`

- [ ] **Step 1: Add a missing-status Eval audit test**

After a commissioned visible measurement, delete the linked Run `status.json`. Snapshot all remaining files, call `EvalService.audit(eval_id)`, and assert:

```text
audit is invalid because status is missing
final/stdout/stderr refs were still checked independently
no status was recreated
no file bytes or inodes changed
```

- [ ] **Step 2: Run the audit test and verify RED**

Run:

```bash
/workspace/Arbor/.venv/bin/python -m pytest \
  tests/test_aros_eval.py -k 'audit and missing and status'
```

Expected: final/log validation currently fails through the missing status dependency.

- [ ] **Step 3: Remove obsolete audit-mode plumbing**

Remove `for_audit` from `RunService.read_validated_final()`, `verify_output()`, `_verify_output()`, and their Eval callers. Eval audit already reads raw status with `reconcile=False` and reports status/final mismatches separately; immutable final validation no longer needs a weaker audit mode.

- [ ] **Step 4: Run Eval and Run gates**

Run:

```bash
/workspace/Arbor/.venv/bin/python -m pytest \
  tests/test_aros_runs.py tests/test_aros_eval.py tests/test_aros_eval_records.py
```

Expected: all selected tests pass.

### Task 4: Record, review, and commission the correction

**Files:**
- Modify: `docs/analysis/aros-m2-durable-run-smoke.md`
- Modify: `memory/NOW.md`

- [ ] **Step 1: Record exact behavior and limits**

Document the missing/stale-status reproduction, the immutable inputs used for repair, the read-only audit result, focused/full test receipts, and the fact that no final, measurement, or retry is reconstructed.

- [ ] **Step 2: Run maintained gates**

Run:

```bash
/workspace/Arbor/.venv/bin/python -m pytest
/workspace/Arbor/.venv/bin/ruff check src tests scripts
git diff --check
git diff --exit-code main..HEAD -- uv.lock
```

- [ ] **Step 3: Obtain two-stage review**

First request Design Book/spec review for process truth and read-only audit. After it approves, request quality/security/simplicity review. Resolve every Critical/Important finding and rerun affected gates.

- [ ] **Step 4: Commit the commissioned change**

```bash
git add src/aros/runs.py src/aros/eval.py \
  tests/test_aros_runs.py tests/test_aros_eval.py \
  docs/analysis/aros-m2-durable-run-smoke.md memory/NOW.md
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "fix(aros): derive terminal runs from immutable receipts"
```

## Exit gate

A valid immutable Run final can be validated and projected when mutable status is absent or stale; normal reconciliation repairs only that projection; read-only audit reports status loss while still checking final/log integrity; invalid immutable lineage never causes repair; no retry, measurement, process final, or scientific meaning is invented.
