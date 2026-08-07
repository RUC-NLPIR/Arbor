# AROS Phase 0A Task-on-Run Carrier Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Task's duplicate process carrier and project Task execution through the existing durable Run service while preserving brief, worktree, return, collection, and failure lineage.

**Architecture:** Task remains the owner of immutable briefs, isolated worktrees, mailbox records, reviewed returns, collections, preservation, and pruning. A small `task_run` bridge creates one idempotent trusted-local Run whose command is a thin `exec` adapter; Task status and stop become validated projections of that Run. This plan is Phase 0A only and must reduce `src/aros` to at most 16,000 physical lines; Phase 0B performs the remaining consolidation needed for the program-wide 12,000-line gate.

**Tech Stack:** Python 3.10+, Git worktrees, existing `RunService`, `GitCheckpoint`, durable JSON primitives, Typer, pytest.

---

## Scope and file map

Create:

- `src/aros/task_run.py` — immutable Task-to-Run binding, Run preparation, status projection, stop delegation, and final lookup.
- `src/aros/task_adapter.py` — thin validated environment setup followed by `os.execvpe`; no process supervision.
- `tests/test_aros_task_on_run.py` — focused Task/Run bridge, crash seam, status, stop, collection, and observation tests.

Modify:

- `src/aros/tasks.py` — keep Task records/worktrees/returns; replace launch and reconciliation with `task_run` calls.
- `src/aros/task_tool.py` — supply the operational commit callback and record both Task collection and owned Run final observations.
- `src/cli/commands/aros_cmd.py` — supply `GitCheckpoint.commit_paths` to human-direct Task start.
- `src/aros/observations.py` — bind a Task return to its owned Run manifest/final.
- `tests/test_aros_tasks.py` — retain brief/worktree/return/prune tests; replace carrier expectations with Run projections.
- `tests/test_aros_task_tool.py` and `tests/test_aros_task_cli.py` — assert commit-before-launch and public response behavior.
- `tests/test_aros_receipts.py` — remove Task-specific process-receipt assertions now owned by Run.
- `tests/test_aros_architecture_boundary.py` — enforce removal of the duplicate carrier and the Phase 0A source budget.
- `scripts/commission_aros_simple_loop.py` and `scripts/verify_aros_simple_loop.py` — retain and verify exact Task-owned Run lineage.
- `docs/architecture/aros-implementation-baseline.md`, `docs/aros/README.md`, and `docs/analysis/aros-simple-loop-smoke.md` — record the commissioned behavior and remaining Phase 0B limit.

Delete:

- `src/aros/task_runner.py` — duplicate process carrier, stop delivery, logs, process claims, and terminal receipt.
- `tests/test_aros_task_runner.py` — tests for the deleted duplicate carrier; equivalent process truth remains covered by Run tests and the new Task-on-Run integration suite.

Public Task actions remain `create|start|status|list|message|stop|collect|preserve|prune`. Task adapters remain trusted-local and application-scoped. No compatibility reader for `.aros/tasks/<id>/{launch,execution,adapter,final}.json` is added; those files belong only to pre-commissioning ignored runtimes.

## Task 1: Freeze the Task-on-Run contract

**Files:**

- Create: `tests/test_aros_task_on_run.py`
- Modify: `tests/test_aros_architecture_boundary.py`

- [ ] **Step 1: Add a shared test workspace and fake commit callback**

```python
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from arbor.aros.tasks import TaskError, TaskService
from arbor.aros.workspace import init_workspace


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Task on Run Test")
    _git(root, "config", "user.email", "task-run@example.invalid")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "base")
    init_workspace(root, "Test Task on Run.")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initialize AROS")


def _brief(service: TaskService) -> dict[str, object]:
    return service.create(
        "Produce one reviewed return.",
        actor="principal",
        mode="write",
        adapter_argv=[sys.executable, "worker.py"],
        capabilities={"network": False, "shell": True},
        deliverables=["tasks/<task-id>/return.json"],
        acceptance=["return commit is valid"],
        timeout_seconds=60,
        idempotency_key="task-on-run-contract",
    )


def _commit(root: Path):
    from arbor.aros.checkpoint import GitCheckpoint

    return GitCheckpoint(root).commit_paths
```

- [ ] **Step 2: Add failing bridge-contract tests**

```python
def test_start_commits_one_run_manifest_before_launch(tmp_path: Path) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")

    status = service.start(
        str(brief["task_id"]),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    binding = json.loads(
        (tmp_path / ".aros/tasks" / str(brief["task_id"]) / "run.json").read_text()
    )
    assert binding["task_id"] == brief["task_id"]
    assert status["run_id"] == binding["run_id"]
    assert _git(tmp_path, "show", f"HEAD:runs/{binding['run_id']}/manifest.json")
    assert not (tmp_path / ".aros/tasks" / str(brief["task_id"]) / "launch.json").exists()


def test_status_projects_run_identity_without_task_process_claims(tmp_path: Path) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")

    status = service.start(
        str(brief["task_id"]),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    assert status["state"] in {"launched", "running", "completed", "failed_process"}
    assert "run_id" in status
    assert "adapter_pid" not in status
    assert "runner_pid" not in status


def test_task_runtime_contains_no_duplicate_process_authority(tmp_path: Path) -> None:
    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service.start(
        str(brief["task_id"]),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    entries = {path.name for path in (tmp_path / ".aros/tasks" / str(brief["task_id"])).iterdir()}
    assert entries.isdisjoint({"launch.json", "execution.json", "adapter.json", "final.json"})
```

- [ ] **Step 3: Add the architectural deletion and interim LOC tests**

```python
def test_phase0a_removes_duplicate_task_carrier() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "src/aros/task_runner.py").exists()
    assert (root / "src/aros/task_adapter.py").is_file()
    source = (root / "src/aros/tasks.py").read_text(encoding="utf-8")
    assert "task_runner" not in source
    assert "_run_carrier_guardian" not in source


def test_phase0a_aros_source_budget() -> None:
    root = Path(__file__).resolve().parents[1] / "src/aros"
    lines = sum(
        len(path.read_bytes().splitlines())
        for path in root.glob("*.py")
    )
    assert lines <= 16_000
```

- [ ] **Step 4: Run the tests and confirm the intended failures**

Run:

```bash
pytest -q tests/test_aros_task_on_run.py tests/test_aros_architecture_boundary.py -x
```

Expected: FAIL because `TaskService.start` has no `commit_paths`, `task_adapter.py` is absent, and `task_runner.py` still exists.

- [ ] **Step 5: Commit the contract tests**

```bash
git add tests/test_aros_task_on_run.py tests/test_aros_architecture_boundary.py
git commit -m "test(aros): freeze task-on-run replacement contract"
```

## Task 2: Add the thin exec-only Task adapter

**Files:**

- Create: `src/aros/task_adapter.py`
- Test: `tests/test_aros_task_on_run.py`

- [ ] **Step 1: Add failing adapter environment and exec tests**

```python
def test_task_adapter_execs_frozen_argv_in_owned_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_adapter as adapter

    worktree = tmp_path / "child"
    worktree.mkdir()
    context = {
        "argv": ["worker", "--exact"],
        "worktree": str(worktree),
        "environment": {
            "PATH": "/controlled/bin",
            "AROS_TASK_ID": "TASK-20260807-test",
        },
    }
    monkeypatch.setattr(adapter, "load_adapter_context", lambda *_args: context)
    changed: list[Path] = []
    executed: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(adapter.os, "chdir", lambda path: changed.append(Path(path)))
    monkeypatch.setattr(
        adapter.os,
        "execvpe",
        lambda executable, argv, env: executed.append((executable, argv, env)),
    )

    assert adapter.main(["--workspace", str(tmp_path), "--task-id", "TASK-20260807-test"]) == 0
    assert changed == [worktree]
    assert executed == [("worker", ["worker", "--exact"], context["environment"])]


def test_task_adapter_context_has_no_ambient_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbor.aros.task_adapter import build_adapter_environment

    monkeypatch.setenv("SECRET_TOKEN", "must-not-pass")
    environment = build_adapter_environment(
        tmp_path / "runtime",
        task_id="TASK-20260807-test",
        brief_path=tmp_path / "tasks/TASK-20260807-test/brief.json",
        worktree=tmp_path / ".worktree/tasks/TASK-20260807-test",
        base_commit="a" * 40,
        brief_sha256="b" * 64,
    )

    assert "SECRET_TOKEN" not in environment
    assert environment["AROS_TASK_ID"] == "TASK-20260807-test"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest -q tests/test_aros_task_on_run.py -k task_adapter -x`

Expected: FAIL because `arbor.aros.task_adapter` does not exist.

- [ ] **Step 3: Implement the complete exec-only adapter**

```python
"""Thin Task command adapter executed by the shared durable Run carrier."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from pathlib import Path


_ENVIRONMENT_KEYS = (
    "BLIS_NUM_THREADS",
    "CUDA_VISIBLE_DEVICES",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "PATH",
    "TZ",
    "VECLIB_MAXIMUM_THREADS",
)


def build_adapter_environment(
    runtime: Path,
    *,
    task_id: str,
    brief_path: Path,
    worktree: Path,
    base_commit: str,
    brief_sha256: str,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    ambient = os.environ if source is None else source
    environment = {key: ambient[key] for key in _ENVIRONMENT_KEYS if key in ambient}
    environment.update(
        {
            "HOME": str(runtime / "home"),
            "TMPDIR": str(runtime / "tmp"),
            "AROS_TASK_ID": task_id,
            "AROS_TASK_BRIEF": str(brief_path),
            "AROS_TASK_WORKTREE": str(worktree),
            "AROS_TASK_BASE_COMMIT": base_commit,
            "AROS_TASK_BRIEF_SHA256": brief_sha256,
        }
    )
    return environment


def load_adapter_context(workspace: Path, task_id: str) -> dict[str, object]:
    from .tasks import TaskService

    return TaskService(workspace).adapter_context(task_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)
    context = load_adapter_context(args.workspace, args.task_id)
    command = context["argv"]
    environment = context["environment"]
    if not isinstance(command, list) or not command or not isinstance(environment, dict):
        raise ValueError("Task adapter context is invalid")
    os.chdir(str(context["worktree"]))
    os.execvpe(str(command[0]), [str(item) for item in command], environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `pytest -q tests/test_aros_task_on_run.py -k task_adapter`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aros/task_adapter.py tests/test_aros_task_on_run.py
git commit -m "feat(aros): add exec-only task adapter"
```

## Task 3: Create the immutable Task-to-Run binding

**Files:**

- Create: `src/aros/task_run.py`
- Modify: `src/aros/tasks.py`
- Test: `tests/test_aros_task_on_run.py`

- [ ] **Step 1: Add failing binding idempotency and tamper tests**

```python
def test_task_run_binding_is_create_once_and_hash_bound(tmp_path: Path) -> None:
    from arbor.aros.task_run import ensure_task_run, load_task_run

    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    ownership = service._ensure_worktree(str(brief["task_id"]), actor="principal")

    first = ensure_task_run(
        tmp_path,
        brief,
        service._load_ownership(brief),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )
    second = ensure_task_run(
        tmp_path,
        brief,
        service._load_ownership(brief),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    assert first == second == load_task_run(tmp_path, brief, service._load_ownership(brief))
    assert first["run_manifest_ref"] == f"runs/{first['run_id']}/manifest.json"


def test_task_run_binding_rejects_tampered_run_identity(tmp_path: Path) -> None:
    from arbor.aros.task_run import TaskRunError, ensure_task_run

    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(str(brief["task_id"]), actor="principal")
    ownership = service._load_ownership(brief)
    binding = ensure_task_run(
        tmp_path, brief, ownership, actor="principal", commit_paths=_commit(tmp_path)
    )
    path = tmp_path / ".aros/tasks" / str(brief["task_id"]) / "run.json"
    value = json.loads(path.read_text())
    value["run_id"] = "RUN-tampered"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(TaskRunError, match="binding"):
        ensure_task_run(
            tmp_path, brief, ownership, actor="principal", commit_paths=_commit(tmp_path)
        )
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_aros_task_on_run.py -k binding -x`

Expected: FAIL because `task_run.py` does not exist.

- [ ] **Step 3: Implement the binding record and exact Run request**

Use this public shape in `src/aros/task_run.py`:

```python
"""Project one Task execution onto the shared durable Run service."""

from __future__ import annotations

import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from .runs import RunService, read_validated_run_manifest
from .store import create_json, json_sha256, read_json_strict_no_repair, utc_now


_RUN_ID = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$")
_COMMIT_PATHS = Callable[[tuple[str, ...], str], dict[str, object]]
_BINDING_FIELDS = {
    "schema_version",
    "task_id",
    "brief_sha256",
    "ownership_sha256",
    "run_id",
    "run_manifest_ref",
    "run_manifest_sha256",
    "created_at",
    "binding_sha256",
}


class TaskRunError(ValueError):
    pass


def task_run_argv(root: Path, task_id: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "arbor.aros.task_adapter",
        "--workspace",
        str(root),
        "--task-id",
        task_id,
    ]


def _binding_path(root: Path, task_id: str) -> Path:
    return root / ".aros" / "tasks" / task_id / "run.json"


def _binding_sha256(value: dict[str, object]) -> str:
    return json_sha256({key: item for key, item in value.items() if key != "binding_sha256"})


def _ensure_adapter_runtime(root: Path, task_id: str) -> None:
    runtime = root / ".aros" / "tasks" / task_id
    for path in (runtime, runtime / "home", runtime / "tmp"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise TaskRunError(f"Task adapter runtime is unsafe: {path}")
        path.chmod(0o700)


def load_task_run(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
) -> dict[str, object]:
    value = read_json_strict_no_repair(_binding_path(root, str(brief["task_id"])))
    if not isinstance(value, dict) or set(value) != _BINDING_FIELDS:
        raise TaskRunError("invalid Task Run binding schema")
    if (
        value["schema_version"] != 1
        or value["task_id"] != brief["task_id"]
        or value["brief_sha256"] != brief["brief_sha256"]
        or value["ownership_sha256"] != ownership["ownership_sha256"]
        or not isinstance(value["run_id"], str)
        or _RUN_ID.fullmatch(str(value["run_id"])) is None
        or value["run_manifest_ref"] != f"runs/{value['run_id']}/manifest.json"
        or value["binding_sha256"] != _binding_sha256(value)
    ):
        raise TaskRunError("Task Run binding lineage mismatch")
    manifest = read_validated_run_manifest(root, str(value["run_id"]))
    if (
        manifest["manifest_sha256"] != value["run_manifest_sha256"]
        or manifest["argv"] != task_run_argv(root, str(brief["task_id"]))
        or manifest["idempotency_key"]
        != f"task-run-v1:{brief['brief_sha256']}"
        or manifest["actor"] != ownership["actor"]
        or manifest["cwd"] != "."
        or float(manifest["timeout_seconds"]) != float(brief["timeout_seconds"])
        or manifest["security_profile"] != "trusted-local"
    ):
        raise TaskRunError("Task Run manifest binding mismatch")
    return value


def ensure_task_run(
    root: Path,
    brief: dict[str, object],
    ownership: dict[str, object],
    *,
    actor: str,
    commit_paths: _COMMIT_PATHS,
) -> dict[str, object]:
    path = _binding_path(root, str(brief["task_id"]))
    _ensure_adapter_runtime(root, str(brief["task_id"]))
    if path.exists():
        return load_task_run(root, brief, ownership)
    runs = RunService(root)
    manifest = runs.prepare(
        task_run_argv(root, str(brief["task_id"])),
        cwd=".",
        timeout_seconds=float(brief["timeout_seconds"]),
        idempotency_key=f"task-run-v1:{brief['brief_sha256']}",
        actor=actor,
        label=f"task-{str(brief['task_id']).lower()}",
        security_profile="trusted-local",
    )
    run_id = str(manifest["run_id"])
    commit_paths(
        (f"runs/{run_id}/manifest.json",),
        f"Record Task {brief['task_id']} Run {run_id}",
    )
    binding: dict[str, object] = {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "run_id": run_id,
        "run_manifest_ref": f"runs/{run_id}/manifest.json",
        "run_manifest_sha256": manifest["manifest_sha256"],
        "created_at": utc_now(),
    }
    binding["binding_sha256"] = _binding_sha256(binding)
    if not create_json(path, binding):
        return load_task_run(root, brief, ownership)
    return load_task_run(root, brief, ownership)


def commit_terminal_run_if_present(
    root: Path,
    binding: dict[str, object],
    status: dict[str, object],
    *,
    commit_paths: _COMMIT_PATHS,
) -> dict[str, object]:
    terminal = RunService(root).terminal_with_commit(str(binding["run_id"]))
    if terminal is None:
        return status
    _final, paths, message = terminal
    commit_paths(paths, message)
    return RunService(root).status(str(binding["run_id"]))
```

Catch `RunError`, `OSError`, and `TaskRunError` at the `TaskService` boundary and re-raise one `TaskError` with the task id. Do not duplicate Run validation inside `tasks.py`.

- [ ] **Step 4: Add `TaskService.adapter_context`**

```python
def adapter_context(self, task_id: str) -> dict[str, object]:
    from .task_adapter import build_adapter_environment
    from .task_run import load_task_run

    self._validate_task_id(task_id)
    brief = self._load_brief(task_id)
    ownership = self._load_ownership(brief)
    load_task_run(self.root, brief, ownership)
    runtime = self._runtime_path(task_id)
    worktree = Path(str(ownership["worktree_path"]))
    return {
        "argv": list(brief["adapter_argv"]),
        "worktree": str(worktree),
        "environment": build_adapter_environment(
            runtime,
            task_id=task_id,
            brief_path=self.root / "tasks" / task_id / "brief.json",
            worktree=worktree,
            base_commit=str(brief["base_commit"]),
            brief_sha256=str(brief["brief_sha256"]),
        ),
    }
```

- [ ] **Step 5: Run the binding and adapter tests**

Run: `pytest -q tests/test_aros_task_on_run.py -k 'binding or adapter'`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/aros/task_run.py src/aros/tasks.py tests/test_aros_task_on_run.py
git commit -m "feat(aros): bind child tasks to durable runs"
```

## Task 4: Replace Task launch with commit-before-Run-start

**Files:**

- Modify: `src/aros/tasks.py:500-708`
- Modify: `src/aros/task_tool.py:84-188`
- Modify: `src/cli/commands/aros_cmd.py:491-502`
- Test: `tests/test_aros_task_on_run.py`
- Test: `tests/test_aros_task_tool.py`
- Test: `tests/test_aros_task_cli.py`

- [ ] **Step 1: Add a failing crash-seam test**

```python
def test_retry_after_manifest_commit_before_run_start_reuses_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_run as task_run

    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    _git(tmp_path, "add", f"tasks/{brief['task_id']}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    calls = 0

    def crash_once(self, run_id: str, *, actor: str | None = None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected crash after manifest commit")
        return {
            "schema_version": 1,
            "run_id": run_id,
            "state": "launched",
            "updated_at": "2026-08-07T00:00:00.000Z",
        }

    monkeypatch.setattr(task_run.RunService, "start", crash_once)
    with pytest.raises(TaskError, match="injected crash"):
        service.start(
            str(brief["task_id"]),
            actor="principal",
            commit_paths=_commit(tmp_path),
        )
    status = service.start(
        str(brief["task_id"]),
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    assert status["run_id"]
    assert len(list((tmp_path / "runs").glob("RUN-*/manifest.json"))) == 1
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_aros_task_on_run.py -k crash -x`

Expected: FAIL while `TaskService.start` still launches its own carrier.

- [ ] **Step 3: Replace `TaskService.start` with the exact bridge sequence**

```python
def start(
    self,
    task_id: str,
    *,
    actor: str | None = None,
    commit_paths: Callable[[tuple[str, ...], str], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Create one Task-owned Run, commit its manifest, and launch it once."""
    from .task_run import (
        TaskRunError,
        commit_terminal_run_if_present,
        ensure_task_run,
        project_task_status,
    )
    from .runs import RunError, RunService

    self._validate_task_id(task_id)
    if commit_paths is None:
        raise TaskError("task start requires an operational commit callback")
    self._ensure_worktree(task_id, actor=actor)
    publication_lock = self._publication_lock_path()
    _ensure_durable_lock_file(publication_lock, "task record publication lock")
    with file_lock(publication_lock):
        lifecycle_lock = self._lifecycle_lock_path(task_id)
        _ensure_durable_lock_file(lifecycle_lock, "task lifecycle lock")
        with file_lock(lifecycle_lock):
            brief = self._load_brief(task_id)
            ownership = self._load_ownership(brief)
            launch_actor = _validate_text(
                actor if actor is not None else ownership["actor"],
                "actor",
            )
            if launch_actor != ownership["actor"]:
                raise TaskError(f"task ownership actor conflict: {task_id}")
            try:
                binding = ensure_task_run(
                    self.root,
                    brief,
                    ownership,
                    actor=launch_actor,
                    commit_paths=commit_paths,
                )
            except (OSError, RunError, TaskRunError, RuntimeError, ValueError) as error:
                raise TaskError(f"task Run preparation failed for {task_id}: {error}") from error
    try:
        run_status = RunService(self.root).start(
            str(binding["run_id"]),
            actor=launch_actor,
        )
        run_status = commit_terminal_run_if_present(
            self.root,
            binding,
            run_status,
            commit_paths=commit_paths,
        )
        return project_task_status(brief, ownership, binding, run_status)
    except (OSError, RunError, TaskRunError, RuntimeError, ValueError) as error:
        raise TaskError(f"task Run start failed for {task_id}: {error}") from error
```

`project_task_status` must return only:

```python
{
    "schema_version": 1,
    "task_id": brief["task_id"],
    "state": run_status["state"],
    "brief_sha256": brief["brief_sha256"],
    "ownership_sha256": ownership["ownership_sha256"],
    "run_id": binding["run_id"],
    "run_manifest_sha256": binding["run_manifest_sha256"],
    "updated_at": run_status["updated_at"],
    "final_ref": run_status.get("final_ref"),
    "reason": run_status.get("reason"),
}
```

Prepared and worktree-ready statuses keep their existing shapes before a Run binding exists.

- [ ] **Step 4: Wire the Principal Task tool**

Change the start branch in `TaskTool.execute` to:

```python
elif action == "start":
    result = service.start(
        kwargs["task_id"],
        actor="principal",
        commit_paths=self.commit_paths,
    )
    final_ref = result.get("final_ref")
    if isinstance(final_ref, str) and self.record_observation is not None:
        self.record_observation(final_ref)
```

If `self.commit_paths` is absent, `Task.start` must fail before Run preparation. Do not silently launch an uncommitted manifest.

- [ ] **Step 5: Wire the human CLI**

```python
root = _root(cwd)
status = TaskService(root).start(
    task_id,
    actor=actor,
    commit_paths=GitCheckpoint(root).commit_paths,
)
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest -q \
  tests/test_aros_task_on_run.py \
  tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py -x
```

Expected: PASS for commit-before-launch, retry, Principal actor, and human actor paths.

- [ ] **Step 7: Commit**

```bash
git add src/aros/tasks.py src/aros/task_tool.py src/cli/commands/aros_cmd.py \
  tests/test_aros_task_on_run.py tests/test_aros_task_tool.py tests/test_aros_task_cli.py
git commit -m "refactor(aros): launch tasks through durable runs"
```

## Task 5: Project status, list, and stop from Run truth

**Files:**

- Modify: `src/aros/task_run.py`
- Modify: `src/aros/tasks.py:819-866,1584-2169`
- Modify: `src/aros/task_tool.py`
- Modify: `src/cli/commands/aros_cmd.py`
- Test: `tests/test_aros_task_on_run.py`
- Test: `tests/test_aros_tasks.py`

- [ ] **Step 1: Add failing projection tests for every Run state**

```python
@pytest.mark.parametrize(
    ("run_state", "task_state"),
    [
        ("prepared", "launched"),
        ("launched", "launched"),
        ("running", "running"),
        ("completed", "completed"),
        ("failed_process", "failed_process"),
        ("timed_out", "timed_out"),
        ("cancelled", "cancelled"),
        ("lost", "lost"),
    ],
)
def test_task_status_is_a_pure_run_projection(run_state: str, task_state: str) -> None:
    from arbor.aros.task_run import project_task_status

    projected = project_task_status(
        {"task_id": "TASK-20260807-test", "brief_sha256": "a" * 64},
        {"ownership_sha256": "b" * 64},
        {"run_id": "RUN-test", "run_manifest_sha256": "c" * 64},
        {"state": run_state, "updated_at": "2026-08-07T00:00:00.000Z"},
    )

    assert projected["state"] == task_state
    assert projected["run_id"] == "RUN-test"
```

```python
def test_task_stop_delegates_exact_actor_reason_and_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arbor.aros.task_run as task_run
    from arbor.aros.task_run import ensure_task_run

    _workspace(tmp_path)
    service = TaskService(tmp_path)
    brief = _brief(service)
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service._ensure_worktree(task_id, actor="principal")
    ownership = service._load_ownership(brief)
    binding = ensure_task_run(
        tmp_path,
        brief,
        ownership,
        actor="principal",
        commit_paths=_commit(tmp_path),
    )

    calls: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        task_run.RunService,
        "stop",
        lambda self, run_id, *, actor, reason, signal_name: calls.append(
            (run_id, actor, reason, signal_name)
        ) or {"run_id": run_id, "kind": "run_stop"},
    )
    result = service.stop(
        task_id,
        actor="principal",
        reason="evidence is sufficient",
        signal_name="INT",
    )
    assert calls == [
        (str(binding["run_id"]), "principal", "evidence is sufficient", "INT")
    ]
    assert result["run_id"] == binding["run_id"]
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_aros_task_on_run.py -k 'projection or stop' -x`

Expected: FAIL until status and stop delegate to Run.

- [ ] **Step 3: Implement state projection and status lookup**

Add this complete projection to `task_run.py`:

```python
_RUN_STATES = {
    "prepared",
    "launched",
    "running",
    "completed",
    "failed_process",
    "timed_out",
    "cancelled",
    "lost",
}


def project_task_status(
    brief: dict[str, object],
    ownership: dict[str, object],
    binding: dict[str, object],
    run_status: dict[str, object],
) -> dict[str, object]:
    run_state = run_status.get("state")
    if run_state not in _RUN_STATES:
        raise TaskRunError(f"unknown owned Run state: {run_state!r}")
    state = "launched" if run_state == "prepared" else run_state
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": state,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "run_id": binding["run_id"],
        "run_manifest_sha256": binding["run_manifest_sha256"],
        "updated_at": run_status["updated_at"],
        "final_ref": run_status.get("final_ref"),
        "reason": run_status.get("reason"),
    }
```

In `TaskService._status_unlocked`, use the existing prepared/worktree-ready path until `run.json` exists, then:

```python
binding = load_task_run(self.root, brief, ownership)
run_status = RunService(self.root).status(str(binding["run_id"]))
if commit_paths is not None:
    run_status = commit_terminal_run_if_present(
        self.root,
        binding,
        run_status,
        commit_paths=commit_paths,
    )
return project_task_status(brief, ownership, binding, run_status)
```

Add `commit_paths` as an optional keyword-only argument to `TaskService.status`
and `_status_unlocked`, and pass it through unchanged under the existing
publication/lifecycle locks.
`TaskTool.status` passes `self.commit_paths`; the CLI passes
`GitCheckpoint(root).commit_paths`. A read-only service caller may omit it, but
then it cannot claim the terminal Run final has been committed. Do not write a
second execution status snapshot under `.aros/tasks`.

Use this Task tool branch so a terminal status also enters the session's
observed refs:

```python
elif action == "status":
    result = service.status(
        kwargs["task_id"],
        commit_paths=self.commit_paths,
    )
    final_ref = result.get("final_ref")
    if isinstance(final_ref, str) and self.record_observation is not None:
        self.record_observation(final_ref)
```

- [ ] **Step 4: Replace Task stop**

```python
def stop(
    self,
    task_id: str,
    *,
    actor: str,
    reason: str,
    signal_name: str = "TERM",
) -> dict[str, object]:
    from .task_run import load_task_run
    from .runs import RunService

    brief = self._load_brief(task_id)
    ownership = self._load_ownership(brief)
    binding = load_task_run(self.root, brief, ownership)
    receipt = RunService(self.root).stop(
        str(binding["run_id"]),
        actor=actor,
        reason=reason,
        signal_name=signal_name,
    )
    return {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": binding["run_id"],
        "run_stop": receipt,
    }
```

- [ ] **Step 5: Delete task execution reconciliation paths from `tasks.py`**

Delete these exact responsibilities and their private helpers:

- task launch/carrier records and carrier-launch locks;
- runner and adapter process claims;
- Task-owned stop delivery/result records;
- Task-owned stdout/stderr/final receipts;
- task process liveness and grace-period reconciliation;
- task runtime HOME/TMP/log preparation that the thin adapter or Run now owns.

Retain brief publication, worktree ownership, mailbox, return validation, collection, preservation, pruning, Git safety helpers, and Task idempotency.

- [ ] **Step 6: Run focused status and stop suites**

Run:

```bash
pytest -q tests/test_aros_task_on_run.py tests/test_aros_tasks.py \
  -k 'status or list or stop or start' -x
```

Expected: PASS with no Task process claims or carrier calls.

- [ ] **Step 7: Commit**

```bash
git add src/aros/task_run.py src/aros/tasks.py src/aros/task_tool.py \
  src/cli/commands/aros_cmd.py tests/test_aros_task_on_run.py \
  tests/test_aros_tasks.py tests/test_aros_task_tool.py tests/test_aros_task_cli.py
git commit -m "refactor(aros): project task lifecycle from run truth"
```

## Task 6: Bind collection and observations to the owned Run final

**Files:**

- Modify: `src/aros/tasks.py:90-119,979-1058,1163-1497,3541-3648`
- Modify: `src/aros/task_tool.py:100-183`
- Modify: `src/aros/observations.py:206-230`
- Test: `tests/test_aros_task_on_run.py`
- Test: `tests/test_aros_tasks.py`
- Test: `tests/test_aros_observations.py`

- [ ] **Step 1: Add failing collection-lineage test**

```python
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_collection_binds_owned_run_final_and_child_return(tmp_path: Path) -> None:
    _workspace(tmp_path)
    worker = tmp_path / "worker.py"
    worker.write_bytes(
        (
            Path(__file__).resolve().parents[1]
            / "commissioning/simple_loop/task_adapter.py"
        ).read_bytes()
    )
    _git(tmp_path, "add", "worker.py")
    _git(tmp_path, "commit", "-qm", "add task worker")
    service = TaskService(tmp_path)
    brief = service.create(
        "Produce one reviewed return.",
        actor="principal",
        mode="write",
        adapter_argv=[sys.executable, "worker.py"],
        capabilities={"network": False, "shell": True},
        deliverables=["candidate-mode.txt"],
        acceptance=["return commit is valid"],
        timeout_seconds=60,
        idempotency_key="task-on-run-collection",
    )
    task_id = str(brief["task_id"])
    _git(tmp_path, "add", f"tasks/{task_id}/brief.json")
    _git(tmp_path, "commit", "-qm", "record task brief")
    service.start(task_id, actor="principal", commit_paths=_commit(tmp_path))
    deadline = time.monotonic() + 20
    while True:
        status = service.status(task_id, commit_paths=_commit(tmp_path))
        if status["state"] in {"completed", "failed_process", "timed_out", "cancelled", "lost"}:
            break
        if time.monotonic() >= deadline:
            pytest.fail(f"Task did not become terminal: {status}")
        time.sleep(0.05)
    assert status["state"] == "completed"
    collected, paths, message = service.collect_with_commit(task_id)
    assert paths is not None and message is not None
    _commit(tmp_path)(paths, message)

    assert collected["run_id"].startswith("RUN-")
    assert collected["run_manifest_ref"] == f"runs/{collected['run_id']}/manifest.json"
    assert collected["run_final_ref"] == f"runs/{collected['run_id']}/final.json"
    assert len(collected["run_final_sha256"]) == 64
    assert collected["child_commit"]
    assert collected["return_commit"]
```

```python
def test_task_observation_requires_owned_run_records(tmp_path: Path) -> None:
    from arbor.aros.observations import ObservationCatalog

    record = ObservationCatalog(tmp_path).resolve(
        "tasks/TASK-20260807-test/collected.json"
    )
    assert record.versioned_paths == (
        "tasks/TASK-20260807-test/brief.json",
        f"runs/{record.payload['run_id']}/manifest.json",
        f"runs/{record.payload['run_id']}/final.json",
        "tasks/TASK-20260807-test/collected.json",
    )
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
pytest -q tests/test_aros_task_on_run.py tests/test_aros_observations.py \
  -k 'collection or task_observation' -x
```

Expected: FAIL because current collections bind Task-owned final receipts.

- [ ] **Step 3: Change the collection schema**

Replace `final_sha256` as the execution authority with these exact fields while retaining `final_state`:

```python
"run_id",
"run_manifest_ref",
"run_manifest_sha256",
"run_final_ref",
"run_final_sha256",
```

Build the snapshot from:

```python
binding = load_task_run(self.root, brief, ownership)
runs = RunService(self.root)
final = runs.read_validated_final(str(binding["run_id"]))
snapshot.update(
    {
        "run_id": binding["run_id"],
        "run_manifest_ref": binding["run_manifest_ref"],
        "run_manifest_sha256": binding["run_manifest_sha256"],
        "run_final_ref": f"runs/{binding['run_id']}/final.json",
        "run_final_sha256": json_sha256(final),
        "final_state": final["state"],
    }
)
```

Require a valid child return for non-completed Run states exactly as before. Preserve deterministic `completed_no_return` for a successful adapter that intentionally returns no Task report.

Change `collect_with_commit` to return both exact versioned records in one
selected-path checkpoint:

```python
return (
    record,
    (str(record["run_final_ref"]), f"tasks/{task_id}/collected.json"),
    f"Record task {task_id} Run final and collection",
)
```

The method must reject a missing or nonterminal Run final before creating the
collection. Repeated collection reuses the same two committed records.

- [ ] **Step 4: Update Task observation resolution**

```python
run_id = str(collected["run_id"])
manifest_ref = str(collected["run_manifest_ref"])
final_ref = str(collected["run_final_ref"])
reader.require_file(manifest_ref)
reader.require_file(final_ref)
read_validated_run_manifest(self.root, run_id, reader=reader)
read_validated_run_final(self.root, run_id, reader=reader)
```

Return `versioned_paths=(brief_ref, manifest_ref, final_ref, ref)`.

- [ ] **Step 5: Record both observations after Task collection**

Change `_committed_result` to accept `observations: tuple[str, ...] = ()` and call `record_observation` for each. The collect branch passes:

```python
observations=(
    f"tasks/{task_id}/collected.json",
    str(record["run_final_ref"]),
)
```

The underlying Run final remains independently auditable and is marked observed in the same Principal session; the Task collection remains the scientific child-return observation.

Update the human `task collect` command to call `collect_with_commit`, commit the
returned paths with `GitCheckpoint(root).commit_paths`, and then print the
collection. It must not leave a terminal Run final or collection untracked.

- [ ] **Step 6: Run focused collection, observation, and Attention tests**

Run:

```bash
pytest -q \
  tests/test_aros_task_on_run.py \
  tests/test_aros_tasks.py \
  tests/test_aros_observations.py \
  tests/test_aros_attention.py \
  tests/test_aros_task_tool.py -x
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/aros/tasks.py src/aros/task_tool.py src/aros/observations.py \
  tests/test_aros_task_on_run.py tests/test_aros_tasks.py \
  tests/test_aros_observations.py tests/test_aros_attention.py \
  tests/test_aros_task_tool.py
git commit -m "refactor(aros): bind task returns to run receipts"
```

## Task 7: Remove the duplicate Task carrier and its tests

**Files:**

- Delete: `src/aros/task_runner.py`
- Delete: `tests/test_aros_task_runner.py`
- Modify: `src/aros/tasks.py`
- Modify: `tests/test_aros_receipts.py`
- Modify: `tests/test_aros_tasks.py`
- Test: `tests/test_aros_architecture_boundary.py`

- [ ] **Step 1: Delete the carrier implementation and carrier-only test suite**

```bash
git rm src/aros/task_runner.py tests/test_aros_task_runner.py
```

- [ ] **Step 2: Remove the exact obsolete Task carrier symbols**

Run:

```bash
rg -n 'task_runner|_TASK_RUNNER_BOOTSTRAP|_CARRIER_LAUNCH_GUARDIAN|_run_carrier_guardian|_carrier_launch_guard|_carrier_launch_is_active|_carrier_is_live|_record_carrier_failure|_prepare_execution_paths|_create_preparation|_load_preparation|_load_launch|_load_final|_reconcile_execution|_execution_path|_adapter_path|_stop_result_path|_stop_delivery_lock_path' src/aros/tasks.py tests/test_aros_tasks.py tests/test_aros_receipts.py
```

Expected: no matches after deleting the corresponding constants, methods, path
helpers, imports, and Task-only tests. Generic Run process identity remains in
`runs.py`, `runner.py`, `processes.py`, and their Run tests.

- [ ] **Step 3: Remove Task-specific process receipt tests**

Delete the `arbor.aros.task_runner` import and the assertions for `.aros/tasks/<id>/{stdout,stderr,final}.json` from `tests/test_aros_receipts.py`. Add this replacement assertion to the real Task-on-Run test:

```python
binding = json.loads((root / ".aros/tasks" / task_id / "run.json").read_text())
run_id = str(binding["run_id"])
RunService(root).read_validated_final(run_id)
RunService(root).verify_output(run_id, "stdout")
RunService(root).verify_output(run_id, "stderr")
```

- [ ] **Step 4: Replace carrier-specific Task tests with Run-owned equivalents**

Remove tests whose only subject is Task's tmux guardian, launch lock, runner claim, adapter claim, Task log file, Task stop delivery, or Task final receipt. Retain and update tests for:

- exact brief creation and idempotency;
- clean committed parent requirement;
- worktree allocation and Git hook/filter hardening;
- concurrent start returning one Task-to-Run binding;
- status projection and lost truth from Run;
- mailbox hash chain;
- B-C-R return validation and dirty-work preservation;
- collect, preserve, and prune recovery.

The new `tests/test_aros_task_on_run.py` must cover completed, failed, timed-out, cancelled, lost, concurrent-start, crash-before-start, and stop behavior using Run fixtures.

- [ ] **Step 5: Run the deletion gate**

Run:

```bash
pytest -q \
  tests/test_aros_task_on_run.py \
  tests/test_aros_tasks.py \
  tests/test_aros_receipts.py \
  tests/test_aros_architecture_boundary.py -x
```

Expected: PASS and no import of `arbor.aros.task_runner`.

- [ ] **Step 6: Check the Phase 0A line budget**

Run:

```bash
wc -l src/aros/*.py | tail -n 1
```

Expected: total is at most `16000`.

- [ ] **Step 7: Commit**

```bash
git add -u src/aros tests
git add src/aros/task_adapter.py src/aros/task_run.py tests/test_aros_task_on_run.py
git commit -m "refactor(aros): delete duplicate task process carrier"
```

## Task 8: Recommission the public Task tool and CLI

**Files:**

- Modify: `tests/test_aros_task_tool.py`
- Modify: `tests/test_aros_task_cli.py`
- Modify: `tests/test_aros_public_entry.py`
- Modify: `scripts/commission_aros_simple_loop.py`
- Modify: `scripts/verify_aros_simple_loop.py`
- Modify: `tests/test_aros_simple_loop_commissioning.py`

- [ ] **Step 1: Update unit fixtures to expose Task-owned Run identity**

All fake `TaskService.start` implementations accept:

```python
def start(
    self,
    task_id: str,
    *,
    actor: str | None = None,
    commit_paths=None,
) -> dict[str, object]:
    assert callable(commit_paths)
    return {"task_id": task_id, "run_id": "RUN-task", "state": "running"}
```

Assert the CLI and tool response includes `run_id`, while action names and required inputs remain unchanged.

- [ ] **Step 2: Update deterministic provider expectations**

The existing normalized tool sequence remains:

```text
Attention, Write, Write, Checkpoint,
Task.create, Task.start, Task.status..., Task.collect,
Eval.run, Attention, Write..., Checkpoint
```

Do not expose Run tool calls to the model for Task execution; Task owns the Run internally.

- [ ] **Step 3: Extend commissioning evidence**

In `scripts/commission_aros_simple_loop.py`, add to the Task section:

```python
"run_id": collected["run_id"],
"run_manifest_ref": collected["run_manifest_ref"],
"run_manifest_sha256": collected["run_manifest_sha256"],
"run_final_ref": collected["run_final_ref"],
"run_final_sha256": collected["run_final_sha256"],
```

In `scripts/verify_aros_simple_loop.py`, require those exact Git objects, recompute both Run hashes through canonical JSON, and require the final checkpoint trailers to be:

```python
expected_refs = sorted([collected_ref, task["run_final_ref"], receipt_ref])
```

Also require:

```python
if (package / "task_runner.py").exists():
    raise VerificationError("deleted Task carrier remains in wheel")
if not (package / "task_adapter.py").is_file():
    raise VerificationError("Task exec adapter is missing from wheel")
```

- [ ] **Step 4: Run the public and commissioning unit suites**

Run:

```bash
pytest -q \
  tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py \
  tests/test_aros_public_entry.py \
  tests/test_aros_simple_loop_commissioning.py -x
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_aros_task_tool.py tests/test_aros_task_cli.py \
  tests/test_aros_public_entry.py tests/test_aros_simple_loop_commissioning.py \
  scripts/commission_aros_simple_loop.py scripts/verify_aros_simple_loop.py
git commit -m "test(aros): recommission simple loop on shared Run"
```

## Task 9: Update current product truth and run all gates

**Files:**

- Modify: `docs/architecture/aros-implementation-baseline.md`
- Modify: `docs/aros/README.md`
- Modify: `docs/analysis/aros-simple-loop-smoke.md`

- [ ] **Step 1: Build and normally install a clean wheel**

Use an ignored commissioning directory and a fresh virtual environment. Run:

```bash
uv build --wheel --out-dir /tmp/aros-task-on-run-dist
python -m venv /tmp/aros-task-on-run-venv
/tmp/aros-task-on-run-venv/bin/pip install /tmp/aros-task-on-run-dist/*.whl
/tmp/aros-task-on-run-venv/bin/pip check
```

Expected: wheel build succeeds and `pip check` reports no broken requirements.

- [ ] **Step 2: Run the clean-wheel simple-loop commissioning**

Run with a new absent runtime root:

```bash
/tmp/aros-task-on-run-venv/bin/python scripts/commission_aros_simple_loop.py \
  --aros /tmp/aros-task-on-run-venv/bin/aros \
  --runtime /workspace/Arbor/.worktree/commissioning/aros-task-on-run-run-1
```

Expected: the driver and standalone verifier return `state=verified`; evidence binds one Task, its owned Run, one Eval, the final checkpoint, and zero-message restart.

- [ ] **Step 3: Update registered current documentation**

Record:

- exact source commit, wheel name/hash, and evidence hash;
- Task id, owned Run id, candidate C, return R, collection hash, and Run final hash;
- exact three observed refs in the final checkpoint;
- absence of `task_runner.py` and presence of `task_adapter.py` in the wheel;
- Phase 0A `src/aros` physical line count and the remaining Phase 0B 12,000-line obligation;
- explicit non-claims: no real Researcher, Mission Supervisor, budgets, Source, Reviewer, or Arbor retirement.

- [ ] **Step 4: Run focused AROS gates**

Run:

```bash
pytest -q \
  tests/test_aros_task_on_run.py \
  tests/test_aros_tasks.py \
  tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py \
  tests/test_aros_runs.py \
  tests/test_aros_run_tool.py \
  tests/test_aros_run_cli.py \
  tests/test_aros_eval.py \
  tests/test_aros_eval_tool.py \
  tests/test_aros_observations.py \
  tests/test_aros_attention.py \
  tests/test_aros_simple_loop_commissioning.py \
  tests/test_aros_architecture_boundary.py \
  tests/test_document_registry.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Run full verification**

Run:

```bash
python -m pytest -q
ruff check src/aros src/cli/aros_app.py src/cli/aros_start.py \
  src/cli/commands/aros_cmd.py tests/test_aros_*.py \
  scripts/commission_aros_*.py scripts/verify_aros_*.py
git diff --check
git status --short
wc -l src/aros/*.py | tail -n 1
```

Expected:

- full pytest exits 0;
- focused Ruff reports `All checks passed!`;
- `git diff --check` exits 0;
- worktree status contains only the intended documentation changes before the final commit;
- `src/aros` is at most 16,000 physical lines.

- [ ] **Step 6: Commit current truth and evidence**

```bash
git add docs/architecture/aros-implementation-baseline.md docs/aros/README.md \
  docs/analysis/aros-simple-loop-smoke.md
git commit -m "docs(aros): record task-on-run commissioning"
```

## Final acceptance checklist

- Task public actions are unchanged.
- Task create/brief/worktree/mailbox/return/collect/preserve/prune semantics remain.
- Task launch creates and commits exactly one idempotent Run manifest before execution.
- Task status and stop contain no second process authority.
- Task collection binds exact Run manifest/final and B-C-R child lineage.
- Task-owned Run final is observed with the Task collection, so restart has no duplicate unread return.
- `src/aros/task_runner.py` and all carrier-only imports/tests are absent.
- `src/aros/task_adapter.py` performs validation/environment setup and `exec`, but no process supervision.
- Existing Run stop/timeout/lost/descendant truth is the only execution truth.
- Clean-wheel deterministic simple-loop commissioning passes.
- `src/aros <= 16,000 LOC`; Phase 0B remains explicitly required for `<=12,000 LOC`.
- No Mission Supervisor, budget scheduler, real Researcher, Source, Reviewer, or semantic workflow is added by this plan.
