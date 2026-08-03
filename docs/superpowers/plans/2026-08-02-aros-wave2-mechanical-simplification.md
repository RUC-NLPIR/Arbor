# AROS Wave 2 Mechanical Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove worktree hardening that only defends against an arbitrary hostile same-UID process while preserving all V1 trusted-local crash, attribution, Git, and dirty-work invariants.

**Architecture:** Keep Task authority, safe Git configuration, worktree ownership, and the reviewed durable runner unchanged. Simplify only three worktree-validation mechanisms in `src/aros/tasks.py`: per-blob checkout rereads, directory-fd launch pinning, and inode-only Git-directory pinning. A later Operations-wave plan will consolidate shared Run/Task process mechanics without creating duplicate Run records.

**Tech Stack:** Python 3.10+, Git worktrees, pytest, Ruff.

---

### Task 1: Trust Git-native checkout semantics

**Files:**
- Modify: `src/aros/tasks.py`
- Modify: `tests/test_aros_tasks.py`

- [ ] **Step 1: Write the failing Git-native EOL acceptance test**

Add a real repository test that commits an LF blob under `.gitattributes` with `text eol=crlf`, creates and commits a task brief, calls `_ensure_worktree`, and asserts:

```python
assert status["state"] == "worktree_ready"
assert checked_out.read_bytes() == b"line-one\r\nline-two\r\n"
assert _git(child, "status", "--porcelain=v1", "--untracked-files=all") == ""
```

The current `_verify_checkout_bytes()` must reject this valid Git checkout, proving RED for the behavior being simplified.

- [ ] **Step 2: Run the single test and observe RED**

Run:

```bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_tasks.py::test_start_accepts_clean_git_native_eol_checkout
```

Expected: FAIL because checked-out CRLF bytes differ from the committed LF blob.

- [ ] **Step 3: Remove the O(repository-size) byte verifier**

Delete `_verify_checkout_bytes()` and its call from `_validate_new_checkout()`. Keep both strict metadata/cleanliness passes around ownership publication:

```python
tip = self._validate_worktree(target, branch, base_commit)
if tip != base_commit:
    raise TaskError("new task worktree does not start at its brief base commit")
self._require_pristine_new_checkout(target, base_commit)
```

Remove tests whose only purpose was byte rereading:

- checkout-bytes mismatch rejection;
- local gitlink blob reread;
- the tracked-hardlink case from the racy-new-checkout parameterization.

Keep base-tip, staged, unstaged, untracked, ignored, executable-mode, hook/filter marker, and replacement-object tests.

- [ ] **Step 4: Verify Task 1 GREEN**

Run:

```bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_aros_tasks.py
/workspace/Arbor/.venv/bin/ruff check src/aros/tasks.py tests/test_aros_tasks.py
git diff --check
```

Expected: all pass; no `uv.lock` change.

- [ ] **Step 5: Commit**

```bash
git add src/aros/tasks.py tests/test_aros_tasks.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "refactor(aros): trust clean Git checkout semantics"
```

### Task 2: Remove hostile same-UID directory-fd pinning

**Files:**
- Modify: `src/aros/tasks.py`
- Modify: `tests/test_aros_tasks.py`

- [ ] **Step 1: Change the Git-command contract test to require an absolute target**

Update the existing scrubbed/pinned/non-destructive Git command test so its recorded `git worktree add` argv must contain:

```python
expected_target = str(root / ".worktree" / "tasks" / task_id)
assert expected_target in worktree_add_argv
assert "--force" not in worktree_add_argv
assert "prune" not in worktree_add_argv
```

Remove expectations for `sys.executable -c`, `pass_fds`, and a relative task-ID target. Against the current fd trampoline this must fail.

- [ ] **Step 2: Run the contract test and observe RED**

Run the exact updated test with `/workspace/Arbor/.venv/bin/pytest -q`. Expected: FAIL because current Git receives only the relative task ID through the fd trampoline.

- [ ] **Step 3: Invoke safe Git directly with the absolute target**

Delete `_DIRECTORY_FD_EXEC`, `_require_directory_fd_identity()`, and the `os.open`/`fstat`/`pass_fds` Python launcher in `_add_task_worktree()`. Use the existing scrubbed pinned Git path:

```python
result = self._safe_git_result(
    "worktree", "add", "-b", branch, str(target), base_commit
)
if result.returncode != 0:
    raise TaskError(f"unable to create task worktree: {_git_error(result)}")
```

Retain static plain-directory/symlink checks, absolute containment, ref/registration conflict checks, post-add validation, and preservation of every partial/dirty worktree.

Remove only the fd-specific tests for root-swap pinning, fd/identity errors, exact fd launcher argv, and launcher `sitecustomize` isolation.

- [ ] **Step 4: Verify Task 2 GREEN and commit**

Run the focused Task suite, Ruff, and `git diff --check`; then commit only the two files:

```bash
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -am "refactor(aros): simplify task worktree creation"
```

### Task 3: Keep Git path binding, drop inode-adversary checks

**Files:**
- Modify: `src/aros/tasks.py`
- Modify: `tests/test_aros_tasks.py`

- [ ] **Step 1: Record the pre-refactor focused baseline**

Run:

```bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_tasks.py tests/test_aros_task_runner.py
```

Expected: PASS. Save the count in the task report.

- [ ] **Step 2: Simplify repository identity to resolved paths**

Keep:

- exact workspace top-level path;
- `.git` marker to resolved Git-directory association;
- resolved common Git-directory path;
- explicit `--git-dir`, `--work-tree`, and `--no-replace-objects`;
- child worktree common-dir containment and branch registration.

Remove:

- cached `(st_dev, st_ino)` fields from `TaskService.__init__`;
- `_require_pinned_git_identity()` and its calls;
- inode comparisons in `_require_git_root()`;
- the child Git-directory pre/post inode comparison in `_validate_worktree()`.

Continue rejecting a changed resolved Git/common-directory path. Remove only tests for replacing a Git directory at the same pathname and swapping it during HEAD capture; retain ambient `GIT_*`, changed association, linked common-dir redirection, replace-object, and static symlink tests.

- [ ] **Step 3: Run the complete simplification gate**

Run:

```bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_tasks.py tests/test_aros_task_runner.py \
  tests/test_aros_runs.py tests/test_aros_run_tool.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/tasks.py src/aros/task_runner.py \
  tests/test_aros_tasks.py tests/test_aros_task_runner.py
git diff --check
git status --short
```

Expected: all pass; clean status after commit; `uv.lock` byte-identical.

- [ ] **Step 4: Confirm the intended reduction and preserved boundaries**

Record before/after line counts and confirm by source/test search that all remain:

```text
hard-link brief publication
hook/filter/environment controls
dirty/ambiguous preservation
gated adapter launch
zombie-aware process identity
lost without retry
TERM-to-KILL attributed stop
```

No Task code may import Coordinator, IdeaTree, campaign, or semantic update modules.

- [ ] **Step 5: Commit and request whole-change review**

```bash
git add src/aros/tasks.py tests/test_aros_tasks.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "refactor(aros): align task safety with V1 threat model"
```

Dispatch independent spec and quality reviews across all simplification commits before beginning Wave 2 Task 4.

## Deferred Operations-wave consolidation

Create a separate reviewed plan after Wave 2 commissioning to extract one Run/Task process substrate for gated spawn, process identity, liveness, stop, logs, final receipts, and recovery. This simplification must not make TaskService create duplicate RunService manifests or state, and must port the stronger Task invariants before deleting either implementation.
