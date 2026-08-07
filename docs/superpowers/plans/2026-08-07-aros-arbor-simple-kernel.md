# AROS Arbor-Simple Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the transition/assimilation/audit/index ontology and replace it with a small Git-native Checkpoint while preserving native intake, Task/Run/Eval receipts, host-observed refs, and restart Attention.

**Architecture:** Principal and Researcher own research loops; AROS owns only workspace, selected-path Git commits, worktree/process, independent Eval receipts, and Attention. The cutover has no compatibility parser, migration, dual write, old schema reader, or fallback.

**Tech Stack:** Python standard library, Git CLI, existing Arbor Agent/Task/Run/Eval services, pytest. No new dependency or config layer.

---

## File map and deletion map

Create:

- `src/aros/observed.py`: one in-memory set of validated Task/Run/Eval refs.
- `src/aros/attention_tool.py`: bounded read-only Attention tool.
- `src/aros/checkpoint_tool.py`: `message + paths` Checkpoint tool.
- `tests/test_aros_simple_checkpoint.py`: Git commit/trailer/tool behavior.
- `scripts/commission_aros_simple_loop.py` and verifier: replacement E2E.

Replace in place:

- `src/aros/checkpoint.py`: <=350-line ordinary Git implementation.
- `src/aros/attention.py`: Git-trailer observed/unread derivation, no index.
- `src/aros/research_files.py`: readable frontmatter/sections only.
- Task/Run/Eval service/tool operational commit callback signatures.
- Principal and direct CLI wiring.

Delete:

- `src/aros/transitions.py`
- `src/aros/transition_index.py`
- `src/aros/checkpoint_bridge.py`
- `src/aros/operational.py`
- `src/aros/research_tool.py`
- strict checkpoint/transition/index/assimilation/EvidenceLink test suites
- old transition/audit CLI commands
- old principal-loop and real-principal drivers/verifiers that require schemas
- obsolete schema specs/plans and current-schema smoke evidence

### Task 1: Host-tracked observed refs

**Files:**
- Create: `src/aros/observed.py`
- Create: `tests/test_aros_observed.py`

- [ ] **Step 1: Write RED behavior tests**

Desired API:

```python
observed = ObservedRefs()
observed.record("tasks/TASK-x/collected.json")
observed.record("eval/evaluations/EVAL-x/receipt.json")
assert observed.snapshot() == (
    "eval/evaluations/EVAL-x/receipt.json",
    "tasks/TASK-x/collected.json",
)
observed.clear(observed.snapshot())
assert observed.snapshot() == ()
```

Reject absolute paths, traversal, backslash/NUL, runtime paths, arbitrary
semantic paths, and nonterminal Task/Run/Eval refs. Duplicate record is
idempotent. Snapshot is immutable and sorted.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_observed.py
```

Expected: missing module failure.

- [ ] **Step 3: Implement <=80 lines**

Use `PurePosixPath`, one compiled pattern per accepted owning service, and a
plain `set[str]`. No JSON encoder, dataclass, persistence, schema version, or
cache.

- [ ] **Step 4: Verify and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_observed.py
/workspace/Arbor/.venv/bin/ruff check src/aros/observed.py tests/test_aros_observed.py
test "$(wc -l < src/aros/observed.py)" -le 80
git diff --check
git add src/aros/observed.py tests/test_aros_observed.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'feat(aros): track observed returns in session'
```

### Task 2: Atomic cutover to ordinary Git Checkpoint

**Files:**
- Replace: `src/aros/checkpoint.py`
- Create: `src/aros/attention_tool.py`
- Create: `src/aros/checkpoint_tool.py`
- Modify: `src/aros/principal.py`
- Modify: `src/aros/task_tool.py`, `run_tool.py`, `eval_tool.py`
- Modify: `src/aros/tasks.py`, `runs.py`, `eval.py`
- Modify: `src/cli/commands/aros_cmd.py`
- Delete: `src/aros/operational.py`, `checkpoint_bridge.py`, `research_tool.py`
- Delete/replace related tests with `tests/test_aros_simple_checkpoint.py`

- [ ] **Step 1: Write RED Git checkpoint tests**

Cover:

- attached clean-index repo with selected tracked/new/deleted paths;
- exact selected blobs and one new commit;
- unselected unstaged file preserved;
- pre-existing staged file blocks without mutation;
- absolute/traversal/runtime/symlink path rejected;
- duplicate/empty path or empty message rejected;
- automatic sorted `AROS-Observed:` trailers;
- commit failure remains visibly staged and does not reset;
- successful commit clears only committed ObservedRefs;
- no proposal/audit/admission/transition files exist.

Desired API:

```python
service = GitCheckpoint(root)
result = service.commit(
    paths=["model/CURRENT.md", "memory/NOW.md"],
    message="Revise model after measurement",
    observed_refs=observed.snapshot(),
)
```

- [ ] **Step 2: Verify RED against old checkpoint**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_simple_checkpoint.py
```

- [ ] **Step 3: Replace checkpoint.py completely**

Implement only `CheckpointError`, `GitCheckpoint.commit`, safe path
normalization, Git subprocess helper, trailer rendering, and commit result.
Require clean cached diff, use `git add -A -- exact paths`, ordinary
`git commit --only --no-verify -- exact paths`, then verify parent/path/blob results. No gateway,
receipt, audit, temp index, commit-tree, CAS, fence, projection, recovery state,
or bridge.

- [ ] **Step 4: Add Attention and Checkpoint tools**

`AttentionTool` has no action field and only optional `max_chars`.
`CheckpointTool` schema is exactly:

```json
{
  "message": "non-empty string",
  "paths": ["workspace-relative path"]
}
```

It snapshots ObservedRefs, calls GitCheckpoint, and clears them only after
success. No model-supplied observation list.

- [ ] **Step 5: Replace Principal wiring**

Build one `ObservedRefs`, one `GitCheckpoint`, and tools:

```text
Read, Grep, Glob, Edit, Write,
Attention, Task, Run, Eval,
Checkpoint only when host grants cooperative checkpoint,
optional Bash
```

Task/Run/Eval receive `record_observation=observed.record` and
`commit_paths=checkpoint.commit_operational`. Remove AdmissionGateway and
Attention transition context plumbing that only served the old receipt.

- [ ] **Step 6: Delete OperationalIntent atomically**

Rename service methods to return `(record, paths, message)` tuples:

```text
Task create/collect
Run prepare/terminal
Eval run terminal closure
```

Tools call the shared commit callback directly and return only factual service
record plus optional checkpoint result. Delete `admission_required`,
`operational_intent`, and the operational module.

- [ ] **Step 7: Replace CLI surface**

Delete transition subapp, transition audit, audit rebuild-index, proposal-based
checkpoint, HumanDirectGateway, admission receipt decoding, and associated
imports. Direct CLI checkpoint becomes:

```text
aros checkpoint --message TEXT --path PATH [--path PATH]
```

It is explicitly cooperative in help/output and uses GitCheckpoint with no
observed refs because it is a standalone human invocation.

- [ ] **Step 8: Delete old tests and run focused cutover tests**

Delete checkpoint/bridge/transition/index/research-tool tests that test removed
behavior. Rewrite Principal/Task/Run/Eval/CLI tests for the new callbacks and
tool names. Run:

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_observed.py tests/test_aros_simple_checkpoint.py \
  tests/test_aros_principal.py tests/test_aros_task_tool.py \
  tests/test_aros_run_tool.py tests/test_aros_eval_tool.py \
  tests/test_aros_cli.py tests/test_aros_public_entry.py
```

- [ ] **Step 9: Enforce LOC and commit atomic cutover**

```bash
test "$(wc -l < src/aros/checkpoint.py)" -le 350
test "$(wc -l < src/aros/checkpoint_tool.py)" -le 100
/workspace/Arbor/.venv/bin/ruff check src/aros src/cli/commands/aros_cmd.py \
  tests/test_aros_observed.py tests/test_aros_simple_checkpoint.py
git diff --check
git add -A
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'refactor(aros): replace transition ceremony with git checkpoint'
```

### Task 3: Attention from Git trailers, no semantic index

**Files:**
- Replace/simplify: `src/aros/attention.py`, `attention_fit.py`, `research_files.py`
- Delete: `src/aros/transition_index.py`, `src/aros/transitions.py`
- Modify: Attention/workspace tests
- Delete: transition/index/research-file schema tests

- [ ] **Step 1: Write RED trailer-derived Attention tests**

Create real Git history with terminal Task/Eval records. Assert:

- no trailer => both appear in `unread_returns`;
- checkpoint with automatic trailers => neither appears unread;
- session crash before checkpoint => refs remain unread;
- latest checkpoint exposes `recent_evidence_delta` as commit, observed refs,
  and changed paths;
- malformed/unowned trailers are ignored with warning;
- no `.aros/indexes` path or rebuild is needed;
- active Question/Model/uncertainty/dirty/worktree/runtime facts remain bounded.

- [ ] **Step 2: Implement Git trailer scan directly**

Use `git log --format=%H%x00%B%x00` and `git diff-tree --name-only` through the
existing Git subprocess pattern. Parse exact `AROS-Observed: ` lines and
validate refs with `ObservedRefs` validation. No cache/index class.

Rename packet field `unassimilated_returns` to `unread_returns`; keep
`recent_evidence_delta` as one compact factual commit view.

- [ ] **Step 3: Remove EvidenceLink parsing**

Reduce `research_files.py` to UTF-8/frontmatter/heading navigation required by
Attention. Delete relation enums, JSON-line parsing, occurrence/link IDs,
confidence/scope validation, and all admission uses.

- [ ] **Step 4: Delete transition/index code and tests**

Delete production modules and every import/test/doc assertion requiring them.
No historical parser or migration fixture remains.

- [ ] **Step 5: Verify LOC, focused Attention, and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_attention.py tests/test_aros_workspace.py \
  tests/test_aros_observed.py tests/test_aros_simple_checkpoint.py
test "$(wc -l < src/aros/attention.py)" -le 700
/workspace/Arbor/.venv/bin/ruff check src/aros/attention.py \
  src/aros/attention_fit.py src/aros/research_files.py tests/test_aros_attention.py
git diff --check
git add -A
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'refactor(aros): derive unread returns from git trailers'
```

### Task 4: Replacement E2E and obsolete artifact deletion

**Files:**
- Create: `scripts/commission_aros_simple_loop.py`
- Create: `scripts/verify_aros_simple_loop.py`
- Create: `tests/test_aros_simple_loop_commissioning.py`
- Delete: old principal-loop/real-principal schema drivers/verifiers and
  schema-bound commissioning provider
- Delete/replace: obsolete schema specs/plans/docs and registry entries
- Modify: native-start driver only where tool names changed

- [ ] **Step 1: Write RED replacement verifier tests**

The deterministic fixture must prove:

```text
Attention
-> preregistration prose files
-> Checkpoint(message, paths)
-> one Task + Eval
-> final prose files with ordinary Markdown refs
-> Checkpoint(message, paths)
-> automatic trailers
-> primary destruction
-> fresh Attention with unread_returns=[]
```

Reject proposal/audit/admission/transition files, model-supplied observed refs,
strict EvidenceLink JSON, missing trailers, extra Task/Eval, semantic driver
writes, and old command/module resurrection.

- [ ] **Step 2: Implement one replacement driver/verifier**

Reuse current Task adapter/scorer and deterministic provider style. The model
fixture emits new Attention/Checkpoint tool calls and ordinary Markdown. Driver
creates no scientific bytes after Agent start. Verifier reads exact Git
commits/trailers, Task/Eval receipts, and restart packet.

- [ ] **Step 3: Delete superseded code/docs directly**

Remove old drivers/verifiers, transition-schema specs/plans, schema smoke docs,
and registry entries. Keep native-start evidence and the real Attempt 1 analysis
only as evidence explaining the deletion decision; mark neither as current
product behavior.

- [ ] **Step 4: Clean-wheel run and independent verification**

Build from clean source in a normal venv, run replacement E2E once, then invoke
the verifier separately. Preserve any failure root; do not run old E2E in
parallel.

- [ ] **Step 5: Commit replacement evidence**

Document exact source/wheel hashes, Git commits/trailers, Task C/R, Eval receipt,
restart, deleted surfaces, production LOC delta, and cooperative boundary.

### Task 5: Full gates and complexity audit

**Files:**
- Update current public guide, implementation baseline, document registry, and NOW.

- [ ] **Step 1: Run architecture/deletion scans**

Require no import/path/help/doc occurrence of removed modules, commands,
proposal/audit/admission/assimilation/EvidenceLink schema, or OperationalIntent.

- [ ] **Step 2: Run full repository tests and Ruff**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q
/workspace/Arbor/.venv/bin/ruff check src/aros src/cli/aros_app.py \
  src/cli/aros_start.py src/cli/commands/aros_cmd.py \
  scripts/commission_aros_simple_loop.py scripts/verify_aros_simple_loop.py
git diff --check
git status --short --branch
```

- [ ] **Step 3: Enforce source budgets**

```bash
test "$(find src/aros -name '*.py' -print0 | xargs -0 cat | wc -l)" -le 19000
test "$(wc -l < src/aros/checkpoint.py)" -le 350
test "$(wc -l < src/aros/checkpoint_tool.py)" -le 100
```

Record before/after production LOC and deleted test/docs counts.

- [ ] **Step 4: Audit Design Section 17**

Verify every item from current code, wheel, Git objects, receipts, and help
output. Keep overall AROS goal active; next slice is Task-on-Run plus one real
Researcher inner loop.
