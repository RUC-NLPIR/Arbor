# AROS Wave 3 Visible Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Commission deterministic visible evaluation over exact candidate/apparatus commits while `RunService` remains the sole process authority and lost requests never retry.

**Architecture:** First serialize Run launch/reconcile and extract only pure receipt/Git-checkout mechanics. Then add a two-checkout read-only execution bundle to Run, strict evaluator records/parser, and a thin foreground Eval broker that prepares one Run, waits, parses verified stdout, and publishes one measurement receipt. Protected admission, activity gates, hidden FDs, and synchronous process primitives are a separate follow-on plan after visible commissioning.

**Tech Stack:** Python 3.10+, Git worktrees, tmux-backed `RunService`, Linux isolated profile, strict JSON, pytest, Typer.

**Authority:** `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`, `docs/superpowers/specs/2026-08-02-aros-v1-product-and-migration-design.md`, and `docs/superpowers/specs/2026-08-03-aros-wave3-eval-design.md`.

**Global constraint:** Never run `uv`; use `/workspace/Arbor/.venv/bin/python` and `/workspace/Arbor/.venv/bin/ruff`. Never cherry-pick the experimental M4 branch.

---

### Task 1: Serialize Run launch and reconciliation

**Files:**
- Modify: `src/aros/runs.py`
- Modify: `tests/test_aros_runs.py`

- [ ] **Step 1: Write the concurrent RED test**

Add one parameterized real-thread test. The fake tmux call pauses after
`_start_locked` has published `launched`; public `status()` and `reconcile()`
must block on the same run lock and must never publish `lost` during that pause.

```python
@pytest.mark.parametrize("operation", ("status", "reconcile"))
def test_reconcile_waits_for_inflight_launch_lock(tmp_path, monkeypatch, operation):
    service, manifest = _prepared_run(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def paused_tmux(command, **_kwargs):
        if "new-session" in command:
            entered.set()
            assert release.wait(5)
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "missing")

    monkeypatch.setattr(runs_module.subprocess, "run", paused_tmux)
    monkeypatch.setattr(runs_module, "_tmux_session_exists", lambda _name: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        launch = pool.submit(service.start, manifest["run_id"])
        assert entered.wait(5)
        observer = pool.submit(getattr(service, operation), manifest["run_id"])
        try:
            with pytest.raises(concurrent.futures.TimeoutError):
                observer.result(timeout=0.2)
        finally:
            release.set()
        launch.result(timeout=5)
        assert observer.result(timeout=5)["state"] != "lost"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_runs.py::test_reconcile_waits_for_inflight_launch_lock
```

Expected: FAIL because public reconcile is currently unlocked and can return
`lost` before launch completes.

- [ ] **Step 3: Implement one locked transition path**

Keep the current reconciliation body unchanged behind a locked helper:

```python
def reconcile(self, run_id: str) -> dict[str, object]:
    self._validate_run_id(run_id)
    with file_lock(self._run_lock_path(run_id)):
        return self._reconcile_locked(run_id)
```

Move the current body beginning with `manifest = self._load_manifest(run_id)`
and ending with `return lost` verbatim into
`_reconcile_locked(self, run_id)`. Do not change its branches, fields, events,
or errors in this task.

Inside `_start_locked`, use `self.status(run_id, reconcile=False)`. Any method already
holding the run lock calls `_reconcile_locked`, never recursively acquires a
second file descriptor. Runner bootstrap stays a strict read.

- [ ] **Step 4: Verify GREEN and regressions**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_runs.py
/workspace/Arbor/.venv/bin/ruff check src/aros/runs.py tests/test_aros_runs.py
git diff --check
git diff --exit-code -- uv.lock
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/runs.py tests/test_aros_runs.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "fix(aros): serialize run launch reconciliation"
```

### Task 2: Extract pure receipt helpers without changing schemas

**Files:**
- Create: `src/aros/receipts.py`
- Create: `tests/test_aros_receipts.py`
- Modify: `src/aros/runs.py`
- Modify: `src/aros/runner.py`
- Modify: `src/aros/tasks.py`
- Modify: `src/aros/task_runner.py`

- [ ] **Step 1: Characterize existing byte shapes**

Write tests that compare the new helper output with current Run/Task records:

```python
def test_record_sha256_excludes_only_named_hash_field():
    record = {"schema_version": 1, "value": "x", "record_sha256": "old"}
    assert record_sha256(record, "record_sha256") == json_sha256(
        {"schema_version": 1, "value": "x"}
    )

def test_content_receipt_preserves_existing_shape():
    assert content_receipt("stdout.log", 3, hashlib.sha256(b"abc").hexdigest()) == {
        "path": "stdout.log", "bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()
    }
```

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_receipts.py
```

Expected: import failure because `aros.receipts` does not exist.

- [ ] **Step 3: Add the three pure helpers**

```python
def record_sha256(record: Mapping[str, object], hash_field: str) -> str:
    payload = dict(record)
    payload.pop(hash_field, None)
    return json_sha256(payload)

def digest_chunks(chunks: Iterable[bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in chunks:
        byte_count += len(chunk)
        digest.update(chunk)
    return byte_count, digest.hexdigest()

def content_receipt(path: str, byte_count: int, sha256: str) -> dict[str, object]:
    return {"path": path, "bytes": byte_count, "sha256": sha256}
```

Existing private Run/Task wrappers delegate to these helpers but retain secure
open, inode/link/mode checks, existing exceptions, and existing JSON fields.

- [ ] **Step 4: Verify byte compatibility**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_receipts.py tests/test_aros_store.py \
  tests/test_aros_runs.py tests/test_aros_tasks.py tests/test_aros_task_runner.py
/workspace/Arbor/.venv/bin/ruff check src/aros tests/test_aros_*.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/receipts.py tests/test_aros_receipts.py \
  src/aros/runs.py src/aros/runner.py src/aros/tasks.py src/aros/task_runner.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "refactor(aros): share receipt hashing primitives"
```

### Task 3: Add shared exact Git worktree bindings

**Files:**
- Create: `src/aros/worktrees.py`
- Create: `tests/test_aros_worktrees.py`
- Modify: `src/aros/tasks.py`
- Modify: `tests/test_aros_tasks.py`

- [ ] **Step 1: Write RED tests for repository and detached checkouts**

Cover real Git behavior with six exact tests:

- `test_detached_checkout_is_exact_clean_and_hermetic`: assert detached HEAD,
  exact commit/tree, empty porcelain status, and disabled hooks/global config.
- `test_checkout_rejects_hooks_filters_and_ambient_git_config`: install a
  post-checkout hook and smudge filter; assert no hook marker and a filter error.
- `test_checkout_validation_rejects_head_index_or_registration_drift`:
  parameterize each drift and assert `WorktreeError` without cleanup.
- `test_remove_preserves_dirty_or_ambiguous_checkout`: create tracked and
  untracked dirt; assert `False` and byte preservation.
- `test_execution_bundle_binds_candidate_and_apparatus_trees`: use distinct
  commits and assert both tree hashes contribute to `bundle_sha256`.
- `test_remove_clean_execution_bundle_validates_both_before_any_removal`:
  dirty either checkout in turn; assert neither checkout is removed.

The bundle has this fixed shape:

```python
@dataclass(frozen=True)
class ExecutionBundle:
    root: Path
    candidate: CheckoutBinding
    apparatus: CheckoutBinding
    bundle_sha256: str
```

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_worktrees.py
```

Expected: import failure because `aros.worktrees` does not exist.

- [ ] **Step 3: Implement only pinned Git mechanics**

Public internal API consists exactly of `bind_repository(root)`,
`create_detached_checkout(repo, path, commit)`,
`validate_detached_checkout(repo, checkout)`,
`create_execution_bundle(repo, root, candidate, apparatus)`,
`validate_execution_bundle(repo, bundle)`, and
`remove_clean_checkout(repo, checkout)`, plus
`remove_clean_execution_bundle(repo, bundle)`. Creation returns the frozen dataclass;
validation returns `None`; removal returns `True` only after exact clean removal
and `False` for dirty material. Ambiguous authority raises `WorktreeError`.
Bundle removal validates both checkouts before removing either one.

All Git commands use the current Task allowlist: scrub loader variables,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, hooks disabled,
`--no-replace-objects`, filters rejected, exact common-dir registration, exact
HEAD/tree, empty porcelain status, and no global worktree prune.

Task retains ownership/prune semantics. Its private Git environment and
registration parser delegate to shared pure helpers, so attached Task tests
prove the module is shared rather than an Eval-only copy.

- [ ] **Step 4: Verify Task compatibility and dirty preservation**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_worktrees.py tests/test_aros_tasks.py tests/test_aros_task_runner.py
/workspace/Arbor/.venv/bin/ruff check src/aros/worktrees.py src/aros/tasks.py \
  tests/test_aros_worktrees.py tests/test_aros_tasks.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/worktrees.py src/aros/tasks.py \
  tests/test_aros_worktrees.py tests/test_aros_tasks.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "refactor(aros): share exact worktree bindings"
```

### Task 4: Let Run execute a verified two-checkout bundle

**Files:**
- Modify: `src/aros/runs.py`
- Modify: `src/aros/runner.py`
- Modify: `src/aros/isolation.py`
- Modify: `src/aros/store.py`
- Modify: `tests/test_aros_runs.py`
- Modify: `tests/test_aros_isolation.py`
- Modify: `tests/test_aros_architecture_boundary.py`

- [ ] **Step 1: Write RED execution-bundle tests**

Add five exact tests:

- `test_prepare_bundle_keeps_control_state_in_primary_workspace`;
- `test_bundle_run_reads_candidate_and_apparatus_but_cannot_write_them`;
- `test_runner_revalidates_both_trees_immediately_before_spawn`;
- `test_bundle_run_rejects_path_symlink_head_tree_or_filter_drift`;
- `test_existing_run_manifest_and_final_schema_remain_readable`.

Each test asserts exact manifest/final hashes and that failure leaves both
checkout directories and registrations available for inspection.

Add an architecture assertion that future `src/aros/eval.py` may call
`RunService`/`worktrees`, but may not call `Popen`, tmux, `killpg`, or write a
process final/status record.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_runs.py -k bundle \
  tests/test_aros_architecture_boundary.py -k eval_process
```

- [ ] **Step 3: Add the internal Run seam**

Add `RunService.prepare_bundle(bundle, argv, *, cwd, timeout_seconds,
idempotency_key, actor, label=None) -> dict[str, object]`. It validates all
arguments before publication and delegates common Run manifest publication to
the same private helper as `prepare`.

The manifest records bundle-relative paths, both commits/trees, and
`bundle_sha256`; it never records a caller-supplied unvalidated absolute root.
Runner loads and revalidates the binding, uses bundle root for isolation, uses
candidate-relative cwd, grants read-only candidate/apparatus roots, and grants
only `.worktree/eval/<eval-id>/tmp` as an ephemeral writable root. Source
checkouts cannot contain that temp directory. Existing schema-v1 Run records
remain valid; bundle fields are optional only for legacy/non-bundle runs and
are included in final identity when present.

- [ ] **Step 4: Verify all Run/isolation behavior**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_runs.py tests/test_aros_run_tool.py tests/test_aros_run_cli.py \
  tests/test_aros_isolation.py tests/test_aros_architecture_boundary.py
/workspace/Arbor/.venv/bin/ruff check src/aros tests/test_aros_*.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/runs.py src/aros/runner.py src/aros/isolation.py src/aros/store.py \
  tests/test_aros_runs.py tests/test_aros_isolation.py \
  tests/test_aros_architecture_boundary.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): run verified evaluation bundles"
```

### Task 5: Add strict visible evaluator records and scalar parser

**Files:**
- Create: `src/aros/eval_records.py`
- Create: `tests/test_aros_eval_records.py`

- [ ] **Step 1: Write table-driven RED tests**

The exact visible manifest fields come from the Wave 3 design. Tests reject
unknown/missing fields, non-commit IDs, unsafe paths/argv, booleans, NaN,
infinity, duplicate keys, multiple JSON documents, huge integers, output over
65,536 bytes, out-of-range metric, and invalid sample counts.

Use `test_scalar_metric_parser_rejects_non_contract_output` parameterized by
`INVALID_METRIC_DOCUMENTS`, plus
`test_scalar_metric_parser_returns_valid_or_underpowered`,
`test_visible_manifest_is_strict_and_self_hashed`, and
`test_process_and_measurement_states_have_only_declared_pairings`. Each valid
case asserts the entire returned dictionary, not only the numeric metric.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_eval_records.py
```

- [ ] **Step 3: Implement plain strict validators**

Use `json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
parse_constant=reject_constant)` to reject duplicate keys and non-finite
constants. Do not add parser plugins,
thresholds, direction, or verdict fields.

Implement exactly four public-internal functions:
`parse_visible_manifest(value)`, `parse_scalar_metric(raw, contract)`,
`build_measurement_receipt(request, run_final, measurement_state,
measurement)`, and `validate_measurement_receipt(value)`. All return new plain
dictionaries; none mutates input or imports Agent/provider code.

- [ ] **Step 4: Verify GREEN**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_eval_records.py
/workspace/Arbor/.venv/bin/ruff check src/aros/eval_records.py tests/test_aros_eval_records.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/eval_records.py tests/test_aros_eval_records.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): add strict visible evaluator records"
```

### Task 6: Register exact visible apparatus and create one-attempt requests

**Files:**
- Create: `src/aros/eval.py`
- Create: `tests/test_aros_eval.py`

- [ ] **Step 1: Write RED registration/request tests**

Add exact tests named
`test_register_freezes_manifest_blob_and_exact_apparatus_files`,
`test_register_rejects_dirty_untracked_filter_hook_or_blob_drift`,
`test_eval_id_is_full_idempotency_digest_and_request_is_create_once`,
`test_same_key_different_request_rejects_without_materialization`, and
`test_execution_claim_is_local_one_attempt_and_never_transfers`.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_eval.py -k 'register or request or execution_claim'
```

- [ ] **Step 3: Implement registration and request publication only**

Add `EvalService.register(manifest_ref, *, actor)` and
`EvalService.prepare_visible(evaluator_id, version, candidate_commit, *, actor,
idempotency_key)`. Both return validated copies of the exact persisted record;
neither starts a process.

`register` reads exact Git blobs and publishes one descriptor below
`.aros/evaluators/`. Evaluation IDs have the exact form
`EVAL-<64-lowercase-hex>`. `prepare_visible` publishes
`.aros/evaluations/<eval-id>/request.json`, creates the execution bundle, calls
`RunService.prepare_bundle` without starting, then publishes create-once
`.aros/evaluations/<eval-id>/run.json`. Its execution claim and lock also live
below that runtime directory. The eventual measurement receipt lives at
`eval/evaluations/<eval-id>/receipt.json`; publishing it happens only after Run
completion. Crash injection tests cover every boundary; recovery never
prepares a second Run.

- [ ] **Step 4: Verify GREEN**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_eval.py tests/test_aros_eval_records.py tests/test_aros_worktrees.py
/workspace/Arbor/.venv/bin/ruff check src/aros/eval.py tests/test_aros_eval.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/eval.py tests/test_aros_eval.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): freeze visible evaluation requests"
```

### Task 7: Execute visible Eval through Run and publish measurement receipts

**Files:**
- Modify: `src/aros/eval.py`
- Modify: `src/aros/runs.py`
- Modify: `tests/test_aros_eval.py`
- Modify: `tests/test_aros_runs.py`

- [ ] **Step 1: Write RED end-state tests**

Add exact tests named
`test_visible_eval_uses_one_run_and_parses_verified_stdout`,
`test_visible_eval_separates_failed_invalid_underpowered_and_valid_negative`,
`test_run_lost_makes_eval_lost_without_receipt_or_retry`,
`test_broker_loss_after_run_final_never_reconstructs_measurement`, and
`test_same_lost_key_never_prepares_starts_attaches_or_finalizes`, plus
`test_visible_eval_removes_exact_clean_bundle_and_preserves_dirty_bundle`.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_eval.py -k 'visible_eval or lost'
```

- [ ] **Step 3: Add the foreground composition**

`EvalService.run` calls `prepare_visible`, acquires the request's execution
lock, loads the exact `run.json`, calls `RunService.start` once, and polls only
`RunService.status` while the referenced state is `launched` or `running`.
Each active poll waits 20 milliseconds; observer callers use the same factual
status API rather than a second watcher loop.
After a terminal Run state it calls `_publish_visible_receipt` once. No branch
calls `prepare_visible` or `start` after a released prior execution claim.

Add one read-only Run helper that opens stdout only after verifying the final
receipt hash/size. Eval never reads an unverified path and never writes process
state/final fields. Known process terminal states publish `not_available` when
appropriate. Run `lost`, released execution lock with no receipt, or
unconfirmed material means Eval `lost` with no measurement receipt.

Before publishing a measurement receipt, Eval revalidates both source trees.
An exact clean bundle is removed through `remove_clean_execution_bundle`. If
either checkout is dirty or authority is ambiguous, both are preserved and the
factual receipt uses `measurement_state=invalid_eval` with
`bundle_cleanup_state=preserved`; no metric from that run is accepted. A crash
after clean removal but before receipt publication is `lost` and never causes
re-parsing during recovery.

- [ ] **Step 4: Verify GREEN and Run parity**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_eval.py tests/test_aros_eval_records.py \
  tests/test_aros_runs.py tests/test_aros_isolation.py
/workspace/Arbor/.venv/bin/ruff check src/aros tests/test_aros_*.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/eval.py src/aros/runs.py tests/test_aros_eval.py tests/test_aros_runs.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): measure visible evaluation through runs"
```

### Task 8: Add visible status, observe, and audit

**Files:**
- Modify: `src/aros/eval.py`
- Modify: `tests/test_aros_eval.py`

- [ ] **Step 1: Write RED observation/audit tests**

Add exact tests named
`test_status_keeps_eval_and_referenced_run_states_separate`,
`test_released_lock_without_receipt_is_immediately_lost`,
`test_observe_returns_only_requested_bounded_visible_stream`,
`test_audit_detects_request_run_bundle_log_or_receipt_tampering`, and
`test_status_and_audit_never_parse_or_repair_missing_measurement`.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_eval.py -k 'status or observe or audit'
```

- [ ] **Step 3: Implement read-only projections**

Add `status(eval_id)`, `observe(eval_id, *, stream, max_bytes=65536)`, and
`audit(eval_id)` methods with the return contracts described below.

`status` reports `evaluation_state`, `referenced_process_state`, and
`measurement_state` independently. `observe` delegates to the linked Run and
accepts only stdout/stderr and a positive bound. `audit` validates only; it
does not repair, parse, retry, clean, or interpret.

- [ ] **Step 4: Verify GREEN**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_eval.py
/workspace/Arbor/.venv/bin/ruff check src/aros/eval.py tests/test_aros_eval.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/eval.py tests/test_aros_eval.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): observe and audit visible evaluations"
```

### Task 9: Expose visible Eval to Principal and CLI

**Files:**
- Create: `src/aros/eval_tool.py`
- Create: `tests/test_aros_eval_tool.py`
- Create: `tests/test_aros_eval_cli.py`
- Modify: `src/aros/principal.py`
- Modify: `src/cli/commands/aros_cmd.py`
- Modify: `tests/test_aros_principal.py`
- Modify: `docs/aros/README.md`
- Modify: `tests/test_document_registry.py`

- [ ] **Step 1: Write RED schema/forwarding tests**

Visible actions are exactly:

```text
register | run | status | observe | audit
```

`admit` is absent until protected commissioning. Tool/CLI tests assert exact
request forwarding and that descriptions say apparatus produces measurement,
Principal interprets it, and lost never retries.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_eval_tool.py tests/test_aros_eval_cli.py tests/test_aros_principal.py
```

- [ ] **Step 3: Implement thin adapters**

`EvalTool` and the Typer `eval` group instantiate the same `EvalService`; they
contain no parser, Git, process, or receipt implementation. Add `EvalTool` to
the Principal default tools. Update the public guide to list visible Eval and
keep protected admission under “not yet implemented.”

- [ ] **Step 4: Verify GREEN and architecture boundary**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_eval_tool.py tests/test_aros_eval_cli.py \
  tests/test_aros_principal.py tests/test_document_registry.py \
  tests/test_aros_architecture_boundary.py tests/test_aros_public_entry.py
/workspace/Arbor/.venv/bin/ruff check src tests scripts
git diff --check
git diff --exit-code -- uv.lock
```

- [ ] **Step 5: Commit**

```bash
git add src/aros/eval_tool.py src/aros/principal.py src/cli/commands/aros_cmd.py \
  tests/test_aros_eval_tool.py tests/test_aros_eval_cli.py \
  tests/test_aros_principal.py docs/aros/README.md tests/test_document_registry.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): expose visible evaluation system calls"
```

### Task 10: Commission visible evaluation and close Gate B

**Files:**
- Create: `docs/analysis/aros-wave3-visible-eval-smoke.md`
- Modify: `docs/document_registry.json`
- Modify: `tests/test_document_registry.py`
- Modify: `memory/NOW.md`

- [ ] **Step 1: Run a real exact-commit visible evaluation**

Create distinct committed candidate and apparatus revisions in a disposable
workspace below `.worktree/commissioning/`. Through the installed direct
`aros` entry:

```bash
aros eval register --manifest eval/suites/quality/1/manifest.json --cwd "$PROJECT"
aros eval run quality 1 "$CANDIDATE" \
  --idempotency-key visible-commission-1 --cwd "$PROJECT"
aros eval status "$EVAL_ID" --cwd "$PROJECT"
aros eval audit "$EVAL_ID" --cwd "$PROJECT"
```

Capture candidate/apparatus commits, bundle hash, Run ID/manifest/final hashes,
raw stdout/stderr hashes, parser version, measurement receipt hash, and cleanup
result. Prove candidate and apparatus checkouts remain exact and clean.

- [ ] **Step 2: Commission lost/no-retry**

Pause the foreground Eval broker after Run linkage, kill it, and show:

```text
evaluation_state = lost
referenced_process_state remains independently observable
same key creates no second Run
new key creates a new Eval/Run only after Principal action
no missing measurement is reconstructed
```

- [ ] **Step 3: Run module and full gates**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -o addopts= -q \
  tests/test_aros_eval_records.py tests/test_aros_eval.py \
  tests/test_aros_eval_tool.py tests/test_aros_eval_cli.py \
  tests/test_aros_worktrees.py tests/test_aros_runs.py
/workspace/Arbor/.venv/bin/python -m pytest -o addopts= -q
/workspace/Arbor/.venv/bin/ruff check src tests scripts
git diff --check
git diff --exit-code -- uv.lock
```

- [ ] **Step 4: Obtain two-stage and whole-gate review**

Dispatch a fresh Design Book/spec reviewer. Only after approval, dispatch a
fresh code-quality/security/simplicity reviewer. Resolve all Critical/Important
findings and rerun affected/full gates. Explicitly verify no Eval-owned process
stack, no retry/attempt history, no semantic verdict, and no M4 whole-port.

- [ ] **Step 5: Record exact evidence and commit**

Register the smoke as current/informative/on-demand, update NOW without claiming
protected admission, and commit:

```bash
git add docs/analysis/aros-wave3-visible-eval-smoke.md \
  docs/document_registry.json tests/test_document_registry.py memory/NOW.md
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "docs(aros): commission visible evaluation"
```

## Visible Eval exit gate

Gate B is complete only when a real direct-CLI evaluation at distinct exact
candidate/apparatus commits produces one Run-backed measurement receipt,
invalid/underpowered/process-failed/lost remain distinct, observer/audit are
bounded and factual, same-key lost never launches again, and full spec/quality
reviews approve the absence of a duplicate Eval process/worktree/recovery
stack. Protected `admit` remains unavailable until the follow-on Gate C-D plan
is separately implemented and commissioned.
