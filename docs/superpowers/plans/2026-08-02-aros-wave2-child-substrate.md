# AROS Wave 2 Child Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (- [ ]) syntax.

**Goal:** Let the Principal create, launch, monitor, message, stop, and collect isolated child tasks without automatic semantic assimilation or destructive cleanup.

**Architecture:** TaskService owns strict task briefs, one dedicated Git worktree per child, a tmux-carried exact adapter command, runtime status, attributed stop, immutable return receipts, and clean-only pruning. The adapter is provider-neutral in Wave 2; Wave 6 supplies Codex/Claude/OpenCode adapters.

**Tech Stack:** Python, Git worktrees, tmux, Typer, existing AROS store/process helpers, pytest.

---

### Task 1: Task records and creation

**Files:**
- Create: src/aros/tasks.py
- Create: tests/test_aros_tasks.py

- [ ] Write RED tests for TaskService.create/list/status:
  - Git-root required;
  - strict TASK ID;
  - brief contains objective, exact base commit, actor, mode, capabilities, deliverables, acceptance, timeout, idempotency key;
  - create writes tasks/TASK-ID/brief.json and .aros/tasks/TASK-ID/status.json;
  - duplicate idempotency returns same task; differing request rejects;
  - no child process/worktree is created.

- [ ] Run:

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_aros_tasks.py
~~~

- [ ] Implement the minimum TaskService and strict helpers. Public brief schema:

~~~json
{
  "schema_version": 1,
  "task_id": "TASK-20260802-example",
  "objective": "bounded child objective",
  "mode": "read_only",
  "base_commit": "40-hex",
  "actor": "principal",
  "adapter_argv": ["exact", "argv"],
  "capabilities": {"network": false, "shell": false},
  "deliverables": ["path"],
  "acceptance": ["command"],
  "timeout_seconds": 3600,
  "idempotency_key": "stable-key",
  "created_at": "UTC"
}
~~~

- [ ] Verify focused tests/Ruff/diff/uv and commit:

~~~bash
git add src/aros/tasks.py tests/test_aros_tasks.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): add child task records"
~~~

### Task 2: Dedicated worktree and ownership

**Files:**
- Modify: src/aros/tasks.py
- Modify: tests/test_aros_tasks.py

- [ ] RED tests:
  - start requires committed brief at current clean HEAD;
  - every task, including read-only, receives .worktree/tasks/TASK-ID;
  - detached task worktree starts at brief base commit on branch aros/task/TASK-ID;
  - symlink/pre-existing path/registered branch conflicts fail closed;
  - owner/lease is recorded before adapter launch;
  - cleanup never removes dirty worktree;
  - repeated start reattaches or rejects, never creates a second worktree.

- [ ] Implement Git commands with hooks/filters disabled and exact HEAD verification. Worktree path must be absolute, contained below workspace .worktree/tasks, and non-symlinked.

- [ ] Verify and commit:

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_aros_tasks.py
git add src/aros/tasks.py tests/test_aros_tasks.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): isolate child task worktrees"
~~~

### Task 3: Durable task runner

**Files:**
- Create: src/aros/task_runner.py
- Modify: src/aros/tasks.py
- Modify: tests/test_aros_tasks.py
- Create: tests/test_aros_task_runner.py

- [ ] RED tests:
  - tmux is carrier only;
  - runner executes exact adapter argv in task worktree with scrubbed environment;
  - runtime records PID, PGID, start token, host, started_at, heartbeat;
  - timeout and attributed stop terminate/reap process group;
  - Principal/CLI exit does not terminate child;
  - missing process + final receipt reconciles terminal;
  - missing process + no receipt becomes lost;
  - no automatic retry;
  - adapter stdout/stderr are stored below .aros/tasks/TASK-ID.

- [ ] Implement task_runner with existing AROS store/process identity helpers. Wave 2 explicitly labels adapter execution trusted-local/application-scoped; the worktree is the attribution boundary, not a hostile-host sandbox.

- [ ] Verify and commit:

~~~bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_tasks.py tests/test_aros_task_runner.py
git add src/aros/tasks.py src/aros/task_runner.py \
  tests/test_aros_tasks.py tests/test_aros_task_runner.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): add durable child task runner"
~~~

### Task 4: Messages, returns, collection, and preservation

**Files:**
- Modify: src/aros/tasks.py
- Modify: tests/test_aros_tasks.py

- [ ] RED tests:
  - message appends immutable ordered records under .aros/tasks/TASK-ID/messages;
  - child return lives at tasks/TASK-ID/return.json in child worktree;
  - return binds task ID, brief hash, base commit, child commit, changed files, evidence, deviations, uncertainty, follow-up;
  - collect requires terminal process, clean committed child HEAD, strict return, and matching lineage;
  - collect writes parent tasks/TASK-ID/collected.json with return and diff/commit pointers;
  - collect never merges/cherry-picks or edits model/questions/ideas;
  - missing return is completed_no_return, not done;
  - preserve leaves worktree;
  - prune removes only clean, collected worktree after explicit request.

- [ ] Implement strict return hash and create-once collected record.

- [ ] Verify and commit:

~~~bash
/workspace/Arbor/.venv/bin/pytest -q tests/test_aros_tasks.py
git add src/aros/tasks.py tests/test_aros_tasks.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): collect child returns without assimilation"
~~~

### Task 5: CLI and Principal Task tool

**Files:**
- Create: src/aros/task_tool.py
- Modify: src/aros/principal.py
- Modify: src/cli/commands/aros_cmd.py
- Create: tests/test_aros_task_tool.py
- Create: tests/test_aros_task_cli.py
- Modify: tests/test_aros_principal.py

- [ ] RED tests for:

~~~text
aros task create|start|status|list|message|stop|collect|preserve|prune
~~~

TaskTool schema exposes objective/mode/adapter argv/capabilities/deliverables/acceptance/timeout/idempotency and task operations. It exposes no merge or semantic-update action.

- [ ] Implement one TaskTool over TaskService and one Typer task group over the same service.

- [ ] Verify exact tool schema, CLI forwarding, no legacy imports, and commit:

~~~bash
/workspace/Arbor/.venv/bin/pytest -q \
  tests/test_aros_task_tool.py tests/test_aros_task_cli.py \
  tests/test_aros_principal.py
git add src/aros/task_tool.py src/aros/principal.py \
  src/cli/commands/aros_cmd.py tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py tests/test_aros_principal.py
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "feat(aros): expose child task system calls"
~~~

### Task 6: Real parallel commissioning

**Files:**
- Create: docs/analysis/aros-wave2-child-substrate-smoke.md
- Modify: docs/document_registry.json
- Modify: docs/aros/README.md

- [ ] Create two deterministic real adapters:
  - read-only child inspects committed state and returns evidence;
  - write-heavy child edits a file, commits it, and writes strict return.

- [ ] Launch both tasks concurrently through installed aros. Verify:
  - separate task IDs/worktrees/processes;
  - no parent checkout write;
  - child survives Principal/launcher exit;
  - messages and status work;
  - collect returns exact commit/diff/evidence;
  - Principal performs an explicit selective apply or rejection;
  - no automatic semantic assimilation;
  - dirty worktree preservation and clean-only prune.

- [ ] Run focused/full tests, Ruff, checker, diff, uv.

- [ ] Record exact commits, commands, statuses, receipts, return hashes, worktree paths, assimilation decision, and cleanup result. Register current informative on_demand evidence.

- [ ] Commit:

~~~bash
git add docs/analysis/aros-wave2-child-substrate-smoke.md \
  docs/document_registry.json docs/aros/README.md
git -c user.name="AROS Agent" -c user.email="aros@local.invalid" \
  commit -m "docs(aros): commission child task substrate"
~~~

## Wave 2 exit gate

Wave 2 is complete only when a Principal can launch a read-only and a write-heavy child concurrently, both return attributed committed artifacts without shared-checkout corruption, and the Principal explicitly assimilates or rejects them. No provider session deletion or cleanup may delete committed/dirty work.
