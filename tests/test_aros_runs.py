"""Durable AROS run lifecycle tests.

These tests exercise the filesystem/process contract directly.  tmux tests are
real integration tests when tmux is available; deterministic state-machine
tests do not require it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import arbor.aros.processes as processes_module
import arbor.aros.runner as runner_module
import arbor.aros.runs as runs_module
import arbor.aros.store as store_module
from arbor.aros.isolation import (
    ENVIRONMENT_POLICY,
    NETWORK_POLICY,
    IsolationError,
    probe_isolated_linux,
)
from arbor.aros.receipts import record_sha256
from arbor.aros.runs import RunError, RunService
from arbor.aros.store import atomic_write_json, manifest_sha256
from arbor.aros.worktrees import (
    ExecutionBundle,
    RepositoryBinding,
    bind_repository,
    create_execution_bundle,
    validate_execution_bundle,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_clean_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / "README.md").write_text("# test workspace\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial state")
    return _git(root, "rev-parse", "HEAD")


def _create_test_execution_bundle(
    root: Path,
    eval_id: str,
    *,
    candidate_files: dict[str, str] | None = None,
    apparatus_files: dict[str, str] | None = None,
) -> tuple[RepositoryBinding, ExecutionBundle]:
    _init_clean_repo(root)
    files = {".gitignore": "/.worktree/\n", **(candidate_files or {})}
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add candidate")
    candidate_commit = _git(root, "rev-parse", "HEAD")
    for relative, content in (
        apparatus_files or {"apparatus.txt": "apparatus\n"}
    ).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add apparatus")
    apparatus_commit = _git(root, "rev-parse", "HEAD")
    repository = bind_repository(root)
    bundle = create_execution_bundle(
        repository,
        root / ".worktree" / "eval" / eval_id,
        candidate_commit,
        apparatus_commit,
    )
    return repository, bundle


def _mark_runner_launched(
    root: Path,
    service: RunService,
    manifest: dict[str, object],
) -> str:
    run_id = str(manifest["run_id"])
    prelaunch = _test_prelaunch_receipt(root, manifest)
    launched_at = str(prelaunch["created_at"])
    session_name = str(prelaunch["tmux_session"])
    atomic_write_json(
        root / ".aros" / "receipts" / f"{run_id}-prelaunch.json",
        prelaunch,
    )
    launched = service.status(run_id, reconcile=False)
    launched.update(
        {
            "state": "launched",
            "actor": prelaunch["actor"],
            "carrier": "tmux",
            "tmux_session": session_name,
            "host": prelaunch["host"],
            "launch_receipt_sha256": prelaunch["receipt_sha256"],
            "launched_at": launched_at,
            "updated_at": launched_at,
        }
    )
    atomic_write_json(
        root / ".aros" / "runs" / run_id / "status.json",
        launched,
    )
    return run_id


def _test_prelaunch_receipt(
    root: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    run_id = str(manifest["run_id"])
    launched_at = str(manifest["created_at"])
    prelaunch: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": f"{run_id}-prelaunch",
        "kind": "run_prelaunch",
        "run_id": run_id,
        "actor": manifest["actor"],
        "created_at": launched_at,
        "base_commit": manifest["base_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "carrier": "tmux",
        "tmux_session": f"aros-{run_id.lower()}",
        "host": runs_module.socket.gethostname(),
        "runner_version": 1,
        "runner_invocation": [
            sys.executable,
            "-B",
            "-m",
            "arbor.aros.runner",
            "--workspace",
            str(root),
            "--run-id",
            run_id,
        ],
    }
    prelaunch["receipt_sha256"] = record_sha256(prelaunch, "receipt_sha256")
    return prelaunch


def _prepare(
    root: Path,
    *,
    argv: list[str] | None = None,
    key: str = "test-run-key",
    timeout: float = 10,
) -> tuple[RunService, dict[str, object]]:
    service = RunService(root)
    manifest = service.prepare(
        argv or [sys.executable, "-c", "print('measured output', flush=True)"],
        cwd=".",
        timeout_seconds=timeout,
        idempotency_key=key,
        actor="test-principal",
        label="durable-test",
        security_profile="trusted-local",
    )
    return service, manifest


def test_run_prepare_returns_manifest_with_direct_commit_request(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)

    manifest, paths, message = service.prepare_with_commit(
        [sys.executable, "-c", "print('run')"],
        cwd=".",
        timeout_seconds=10,
        idempotency_key="run-direct-commit",
        actor="principal",
        label="intent",
        security_profile="trusted-local",
        writable_paths=[],
    )

    run_id = str(manifest["run_id"])
    assert paths == (f"runs/{run_id}/manifest.json",)
    assert message == f"Record run {run_id} manifest"
    assert json.loads((tmp_path / "runs" / run_id / "manifest.json").read_bytes()) == manifest
    assert not (tmp_path / "transitions").exists()


def test_manifest_for_idempotency_key_normalizes_and_strictly_loads(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)
    manifest = service.prepare(
        [sys.executable, "-c", "print('run')"],
        idempotency_key="run-manifest-lookup",
        actor="principal",
        security_profile="trusted-local",
    )

    assert service.manifest_for_idempotency_key("  run-manifest-lookup  ") == manifest
    assert service.manifest_for_idempotency_key("absent-run-key") is None
    with pytest.raises(RunError, match="idempotency_key"):
        service.manifest_for_idempotency_key("   ")


def test_manifest_for_idempotency_key_rejects_invalid_index_linkage(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)
    service.prepare(
        [sys.executable, "-c", "print('run')"],
        idempotency_key="run-manifest-invalid-index",
        actor="principal",
        security_profile="trusted-local",
    )
    index_path = service._idempotency_index_path("run-manifest-invalid-index")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["request_sha256"] = "0" * 64
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(RunError, match="idempotency index"):
        service.manifest_for_idempotency_key("run-manifest-invalid-index")


def test_manifest_for_idempotency_key_does_not_scan_or_repair_without_index(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)
    manifest = service.prepare(
        [sys.executable, "-c", "print('run')"],
        idempotency_key="run-manifest-no-index",
        actor="principal",
        security_profile="trusted-local",
    )
    service._idempotency_index_path("run-manifest-no-index").unlink()
    manifest_path = tmp_path / "runs" / str(manifest["run_id"]) / "manifest.json"
    crash_alias = _install_json_crash_alias(manifest_path)

    assert service.manifest_for_idempotency_key("run-manifest-no-index") is None
    assert crash_alias.exists()
    assert manifest_path.stat().st_nlink == 2


def test_manifest_for_idempotency_key_rejects_hardlinked_index(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)
    service.prepare(
        [sys.executable, "-c", "print('run')"],
        idempotency_key="run-manifest-hardlinked-index",
        actor="principal",
        security_profile="trusted-local",
    )
    index_path = service._idempotency_index_path("run-manifest-hardlinked-index")
    os.link(index_path, tmp_path / "index-alias.json", follow_symlinks=False)

    with pytest.raises(RunError, match="idempotency index"):
        service.manifest_for_idempotency_key("run-manifest-hardlinked-index")


def test_manifest_for_idempotency_key_rejects_rehashed_manifest(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)
    manifest = service.prepare(
        [sys.executable, "-c", "print('run')"],
        idempotency_key="run-manifest-rehashed",
        actor="principal",
        security_profile="trusted-local",
    )
    manifest_path = tmp_path / "runs" / str(manifest["run_id"]) / "manifest.json"
    forged = dict(manifest)
    forged["actor"] = "forged-actor"
    forged["manifest_sha256"] = manifest_sha256(forged)
    manifest_path.write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(RunError, match="idempotency index"):
        service.manifest_for_idempotency_key("run-manifest-rehashed")


@pytest.mark.parametrize("symlink_parent", ["index", "manifest"])
def test_manifest_for_idempotency_key_rejects_symlinked_parent_directory(
    tmp_path: Path,
    symlink_parent: str,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)
    manifest = service.prepare(
        [sys.executable, "-c", "print('run')"],
        idempotency_key="run-manifest-symlinked-parent",
        actor="principal",
        security_profile="trusted-local",
    )
    if symlink_parent == "index":
        parent = tmp_path / ".aros" / "runs" / "idempotency"
        moved = parent.with_name("idempotency-real")
    else:
        parent = tmp_path / "runs" / str(manifest["run_id"])
        moved = parent.with_name(f"{parent.name}-real")
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)

    with pytest.raises(RunError):
        service.manifest_for_idempotency_key("run-manifest-symlinked-parent")


def _install_json_crash_alias(path: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(path.name)).hexdigest()
    alias = path.parent / f".aros-json-{digest}.inspection-crash.tmp"
    os.link(path, alias, follow_symlinks=False)
    return alias


def _wait_for_state(
    service: RunService,
    run_id: str,
    terminal: set[str] | None = None,
    *,
    timeout: float = 10,
) -> dict[str, object]:
    wanted = terminal or {
        "completed",
        "failed_process",
        "timed_out",
        "cancelled",
        "lost",
    }
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = service.status(run_id)
        if latest["state"] in wanted:
            return latest
        time.sleep(0.05)
    pytest.fail(f"run {run_id} did not reach {wanted}; latest status: {latest}")


def _require_tmux() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux is unavailable")


def _linux_process_state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def _linux_process_is_live(pid: int) -> bool:
    state = _linux_process_state(pid)
    return state is not None and state not in {"Z", "X", "x"}


def _wait_for_running_status(
    service: RunService,
    run_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    status: dict[str, object] = {}
    while time.monotonic() < deadline:
        status = service.status(run_id, reconcile=False)
        if status["state"] == "running":
            return status
        time.sleep(0.02)
    pytest.fail(f"run {run_id} did not start; latest status: {status}")


def _wait_for_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 5
    while _linux_process_is_live(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _linux_process_is_live(pid)


def _leader_with_descendant(descendant: str, *, escaped: bool = False) -> str:
    return (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}],"
        f"start_new_session={escaped!r})\n"
        "Path('descendant.pid').write_text(str(child.pid),encoding='utf-8')\n"
        "deadline=time.monotonic()+5\n"
        "while not Path('descendant.ready').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "if not Path('descendant.ready').exists():\n"
        "    raise SystemExit(2)\n"
        "Path('leader.ready').touch()\n"
        "while not Path('leader.release').exists():\n"
        "    time.sleep(0.01)\n"
        "print('leader stdout',flush=True)\n"
    )


def _nested_adopted_descendant() -> str:
    grandchild = (
        "import signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path('grandchild.ready').touch()\n"
        "time.sleep(30)\n"
    )
    return (
        "import signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"child=subprocess.Popen([sys.executable,'-c',{grandchild!r}],"
        "start_new_session=True)\n"
        "Path('grandchild.pid').write_text(str(child.pid),encoding='utf-8')\n"
        "deadline=time.monotonic()+5\n"
        "while not Path('grandchild.ready').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "if not Path('grandchild.ready').exists(): raise SystemExit(2)\n"
        "Path('descendant.ready').touch()\n"
        "time.sleep(30)\n"
    )


def test_prepare_freezes_a_versioned_manifest_and_runtime_status(tmp_path: Path) -> None:
    head = _init_clean_repo(tmp_path)

    service, manifest = _prepare(tmp_path)

    run_id = str(manifest["run_id"])
    manifest_path = tmp_path / "runs" / run_id / "manifest.json"
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    assert manifest_path.is_file()
    assert status_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert manifest["schema_version"] == 1
    assert run_id.startswith("RUN-")
    assert manifest["base_commit"] == head
    assert manifest["repository_ref"] == "."
    assert manifest["argv"][0] == sys.executable
    assert manifest["cwd"] == "."
    assert manifest["timeout_seconds"] == 10
    assert manifest["idempotency_key"] == "test-run-key"
    assert manifest["security_profile"] == "trusted-local"
    assert manifest["writable_paths"] == []
    assert manifest["network_policy"] == "host"
    assert manifest["environment_policy"] == {"kind": "inherit"}
    assert manifest["environment_ref"]["kind"] == "allowlist-fingerprint-v1"
    assert "environment" not in manifest
    assert manifest["actor"] == "test-principal"
    assert manifest["created_at"].endswith("Z")
    assert len(str(manifest["environment_sha256"])) == 64
    assert len(str(manifest["manifest_sha256"])) == 64
    assert manifest["question_refs"] == []
    assert manifest["experiment_ref"] is None
    assert manifest["prediction_ref"] is None
    assert manifest["evaluator_ref"] is None
    assert manifest["evaluator_version"] is None
    assert manifest["seed"] is None
    assert manifest["dataset_ref"] is None
    assert manifest["resource_request"] == {}
    assert manifest["budget"] == {}
    assert manifest["output_paths"] == [
        f".aros/runs/{run_id}/stdout.log",
        f".aros/runs/{run_id}/stderr.log",
        f"runs/{run_id}/final.json",
    ]
    assert manifest["success_exit_codes"] == [0]
    assert manifest["candidate_commit"] is None
    assert service.status(run_id, reconcile=False) == {
        "schema_version": 1,
        "run_id": run_id,
        "state": "prepared",
        "manifest_sha256": manifest["manifest_sha256"],
        "updated_at": manifest["created_at"],
    }
    assert not (tmp_path / "runs" / run_id / "status.json").exists()
    assert not (tmp_path / "runs" / run_id / "stdout.log").exists()


def test_prepare_bundle_keeps_control_state_in_primary_workspace(
    tmp_path: Path,
) -> None:
    _repository, bundle = _create_test_execution_bundle(
        tmp_path,
        "EVAL-control-root",
        candidate_files={"scorer/.keep": ""},
    )

    manifest = RunService(tmp_path).prepare_bundle(
        bundle,
        ["/usr/bin/python3", "../apparatus/score.py"],
        cwd="scorer",
        timeout_seconds=30,
        success_exit_codes=[0],
        idempotency_key="bundle-control-root",
        actor="test-principal",
        label="bundle-control",
    )

    run_id = str(manifest["run_id"])
    payload = {
        "candidate": {
            "path": "candidate",
            "commit": bundle.candidate.commit,
            "tree": bundle.candidate.tree,
        },
        "apparatus": {
            "path": "apparatus",
            "commit": bundle.apparatus.commit,
            "tree": bundle.apparatus.tree,
        },
        "temp": "tmp",
        "bundle_sha256": bundle.bundle_sha256,
    }
    assert manifest["repository_ref"] == ".worktree/eval/EVAL-control-root"
    assert manifest["cwd"] == "candidate/scorer"
    assert manifest["candidate_commit"] == bundle.candidate.commit
    assert manifest["execution_bundle"] == payload
    assert manifest["success_exit_codes"] == [0]
    assert manifest["manifest_sha256"] == manifest_sha256(manifest)
    assert manifest["output_paths"] == [
        f".aros/runs/{run_id}/stdout.log",
        f".aros/runs/{run_id}/stderr.log",
        f"runs/{run_id}/final.json",
    ]
    assert (tmp_path / "runs" / run_id / "manifest.json").is_file()
    assert (tmp_path / ".aros" / "runs" / run_id / "status.json").is_file()
    assert not (bundle.root / "runs").exists()
    assert not (bundle.root / ".aros").exists()


def test_prepare_bundle_requires_strict_success_exit_codes(tmp_path: Path) -> None:
    _repository, bundle = _create_test_execution_bundle(
        tmp_path,
        "EVAL-exit-code-validation",
    )
    service = RunService(tmp_path)
    invalid_values: tuple[object, ...] = (
        [],
        [0, 0],
        [True],
        [1.0],
        (0,),
        "0",
    )

    for index, invalid in enumerate(invalid_values):
        with pytest.raises(RunError, match="success_exit_codes"):
            service.prepare_bundle(
                bundle,
                ["/usr/bin/python3", "-c", "pass"],
                cwd=".",
                timeout_seconds=10,
                success_exit_codes=invalid,  # type: ignore[arg-type]
                idempotency_key=f"invalid-exit-codes-{index}",
                actor="test-principal",
            )

    assert not (tmp_path / "runs").exists()


def test_bundle_run_honors_declared_nonzero_success_exit_codes(
    tmp_path: Path,
) -> None:
    _repository, bundle = _create_test_execution_bundle(
        tmp_path,
        "EVAL-nonzero-exit",
    )
    service = RunService(tmp_path)
    manifest = service.prepare_bundle(
        bundle,
        ["/usr/bin/python3", "-c", "raise SystemExit(7)"],
        cwd=".",
        timeout_seconds=10,
        success_exit_codes=[7],
        idempotency_key="bundle-nonzero-exit",
        actor="test-principal",
    )
    assert manifest["success_exit_codes"] == [7]
    run_id = _mark_runner_launched(tmp_path, service, manifest)

    assert runner_module.run(str(tmp_path), run_id) == 0

    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )
    assert final["state"] == "completed"
    assert final["exit_code"] == 7
    assert final["success_exit_codes"] == [7]
    assert final["manifest_sha256"] == manifest["manifest_sha256"]


def test_existing_run_manifest_and_final_schema_remain_readable(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=["/usr/bin/python3", "-c", "pass"],
        key="existing-schema-v1",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    manifest_path = tmp_path / "runs" / run_id / "manifest.json"
    final_path = tmp_path / "runs" / run_id / "final.json"
    manifest_bytes = manifest_path.read_bytes()
    final_bytes = final_path.read_bytes()
    final = json.loads(final_bytes)

    recovered = RunService(tmp_path)

    assert "execution_bundle" not in manifest
    assert "execution_bundle" not in final
    assert manifest["manifest_sha256"] == manifest_sha256(manifest)
    assert final["manifest_sha256"] == manifest["manifest_sha256"]
    assert final["stdout"] == {
        "path": f".aros/runs/{run_id}/stdout.log",
        "bytes": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert final["stderr"] == {
        "path": f".aros/runs/{run_id}/stderr.log",
        "bytes": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert recovered.status(run_id)["state"] == "completed"
    assert recovered.list() == [recovered.status(run_id)]
    assert recovered.stop(run_id, actor="reader", reason="already final") == final
    assert runner_module.run(str(tmp_path), run_id) == 0
    assert manifest_path.read_bytes() == manifest_bytes
    assert final_path.read_bytes() == final_bytes


def test_trusted_run_spawn_omits_parent_death_and_uses_no_preexec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "pass"],
        key="trusted-spawn-contract",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    real_spawn_process = processes_module.spawn_process
    observed: list[dict[str, object]] = []

    def observe_spawn(*args: object, **kwargs: object):
        observed.append(kwargs)
        return real_spawn_process(*args, **kwargs)

    monkeypatch.setattr(processes_module, "spawn_process", observe_spawn)

    assert runner_module.run(str(tmp_path), run_id) == 0
    assert len(observed) == 1
    assert "parent_death" not in observed[0]
    assert observed[0]["preexec_fn"] is None


def test_trusted_run_payload_survives_runner_process_death(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    pid_path = tmp_path / ".aros" / "payload.pid"
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            (
                "import os,pathlib,sys,time;"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                "time.sleep(30)"
            ),
            str(pid_path),
        ],
        key="ordinary-runner-death",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    runner_pid = os.fork()
    if runner_pid == 0:
        try:
            os._exit(runner_module.run(str(tmp_path), run_id))
        except BaseException:
            os._exit(2)

    payload_pid: int | None = None
    runner_waited = False
    try:
        deadline = time.monotonic() + 5
        while not pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.is_file()
        payload_pid = int(pid_path.read_text(encoding="utf-8"))

        os.kill(runner_pid, signal.SIGKILL)
        waited, wait_status = os.waitpid(runner_pid, 0)
        runner_waited = True
        assert waited == runner_pid
        assert os.waitstatus_to_exitcode(wait_status) == -signal.SIGKILL
        time.sleep(0.1)

        raw = Path(f"/proc/{payload_pid}/stat").read_text(encoding="utf-8")
        state = raw.rsplit(")", 1)[1].split()[0]
        assert state not in {"Z", "X", "x"}
    finally:
        if not runner_waited:
            try:
                os.kill(runner_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(runner_pid, 0)
            except ChildProcessError:
                pass
        if payload_pid is not None:
            try:
                os.killpg(payload_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    "mutable_status",
    ("missing", "launched", "lost", "forged-terminal"),
)
@pytest.mark.parametrize("operation", ("status", "start", "reconcile"))
def test_terminal_status_operation_rebuilds_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutable_status: str,
    operation: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        key=f"terminal-status-{operation}-{mutable_status}",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    launched_status = json.loads(status_path.read_text(encoding="utf-8"))
    prelaunch_path = (
        tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
    )
    prelaunch = json.loads(prelaunch_path.read_text(encoding="utf-8"))
    prelaunch["actor"] = "launch-principal"
    prelaunch["receipt_sha256"] = record_sha256(prelaunch, "receipt_sha256")
    atomic_write_json(prelaunch_path, prelaunch)
    launched_status["actor"] = prelaunch["actor"]
    launched_status["launch_receipt_sha256"] = prelaunch["receipt_sha256"]
    atomic_write_json(status_path, launched_status)
    assert runner_module.run(str(tmp_path), run_id) == 0
    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )
    assert prelaunch["actor"] != manifest["actor"]
    expected = {
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
    if mutable_status == "missing":
        status_path.unlink()
    elif mutable_status == "launched":
        atomic_write_json(status_path, launched_status)
    elif mutable_status == "lost":
        atomic_write_json(
            status_path,
            {
                **launched_status,
                "state": "lost",
                "reason": "forged mutable loss",
                "updated_at": final["finished_at"],
            },
        )
    else:
        atomic_write_json(
            status_path,
            {
                **expected,
                "state": "cancelled",
                "actor": "forged-terminal-actor",
                "host": "forged-terminal-host",
                "tmux_session": "forged-terminal-session",
                "launch_receipt_sha256": "f" * 64,
                "exit_code": None,
                "finished_at": prelaunch["created_at"],
                "heartbeat_at": prelaunch["created_at"],
                "updated_at": prelaunch["created_at"],
                "mutable_only": "forged-terminal-field",
            },
        )

    def forbidden_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("terminal operation must not spawn a process")

    monkeypatch.setattr(runs_module.subprocess, "run", forbidden_process)

    observed = getattr(service, operation)(run_id)

    assert observed == expected
    assert json.loads(status_path.read_text(encoding="utf-8")) == expected


def test_read_verified_output_returns_exact_terminal_log_bytes(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'exact metric bytes\\n')",
        ],
        key="verified-output-happy-path",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0

    assert service.read_verified_output(run_id, "stdout") == b"exact metric bytes\n"
    assert service.read_verified_output(run_id, "stderr") == b""


def test_run_terminal_commit_request_is_derived_only_after_final(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('terminal intent')"],
        key="terminal-direct-commit",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)

    assert service.terminal_with_commit(run_id) is None
    assert runner_module.run(str(tmp_path), run_id) == 0
    final = service.read_validated_final(run_id)

    request = service.terminal_with_commit(run_id)

    assert request is not None
    record, paths, message = request
    assert record == final
    assert paths == (f"runs/{run_id}/final.json",)
    assert message == f"Record run {run_id} final"


def test_immutable_final_readers_ignore_missing_mutable_status_and_fail_closed(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('immutable final')"],
        key="immutable-final-without-status",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    final_path = tmp_path / "runs" / run_id / "final.json"
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    status_path.unlink()

    assert service.read_validated_final(run_id) == final
    assert not status_path.exists()
    assert service.read_verified_output(run_id, "stdout") == b"immutable final\n"
    assert not status_path.exists()

    corrupt_final = {**final, "state": "forged-terminal"}
    atomic_write_json(final_path, corrupt_final)

    with pytest.raises(RunError, match="final"):
        service.status(run_id)
    assert not status_path.exists()


def test_verify_output_streams_large_log_without_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 200_003)",
        ],
        key="stream-verified-output",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    real_read = runs_module.os.read
    read_sizes: list[int] = []

    def bounded_read(descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return real_read(descriptor, size)

    def forbidden_capture(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("stream verification must not call the capture API")

    monkeypatch.setattr(runs_module.os, "read", bounded_read)
    monkeypatch.setattr(service, "read_verified_output", forbidden_capture)

    assert service.verify_output(run_id, "stdout") is None
    assert len(read_sizes) > 3
    assert max(read_sizes) <= 65_536


def test_tail_bytes_returns_only_the_bounded_regular_log_tail(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="bounded-tail-bytes")
    run_id = str(manifest["run_id"])
    log = tmp_path / ".aros" / "runs" / run_id / "stdout.log"

    assert service._tail_bytes(run_id, stream="stdout", max_bytes=8) == b""

    log.write_bytes(b"prefix:bounded-tail")

    assert service._tail_bytes(run_id, stream="stdout", max_bytes=7) == b"ed-tail"


@pytest.mark.parametrize(
    "tamper",
    ("symlink", "hardlink", "replacement", "concurrent-append"),
)
def test_tail_bytes_rejects_alias_replacement_and_concurrent_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key=f"adversarial-tail-{tamper}")
    run_id = str(manifest["run_id"])
    log = tmp_path / ".aros" / "runs" / run_id / "stdout.log"
    log.write_bytes(b"authority-tail")

    if tamper == "symlink":
        backing = log.with_name("outside.log")
        backing.write_bytes(b"outside-secret")
        log.unlink()
        log.symlink_to(backing.name)
    elif tamper == "hardlink":
        log.with_name("stdout-alias.log").hardlink_to(log)
    elif tamper == "replacement":
        replacement = log.with_name("stdout-replacement.log")
        replacement.write_bytes(log.read_bytes())
        real_open = runs_module.os.open

        def replace_before_open(
            path: str | bytes | Path,
            flags: int,
            *args: object,
            **kwargs: object,
        ) -> int:
            if Path(path) == log and replacement.exists():
                replacement.replace(log)
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(runs_module.os, "open", replace_before_open)
    else:
        real_lseek = runs_module.os.lseek
        appended = False

        def append_after_seek(descriptor: int, offset: int, whence: int) -> int:
            nonlocal appended
            position = real_lseek(descriptor, offset, whence)
            if whence == runs_module.os.SEEK_SET and not appended:
                with log.open("ab") as handle:
                    handle.write(b"-concurrent-append")
                appended = True
            return position

        monkeypatch.setattr(runs_module.os, "lseek", append_after_seek)

    with pytest.raises(RunError, match="tail|regular|link|identity|changed"):
        service._tail_bytes(run_id, stream="stdout", max_bytes=8)

    if tamper == "concurrent-append":
        assert appended is True


@pytest.mark.parametrize(
    "authority",
    ("manifest", "status", "prelaunch", "final"),
)
def test_run_read_only_loaders_preserve_crash_aliases(
    tmp_path: Path,
    authority: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key=f"run-read-only-{authority}")
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    path = {
        "manifest": tmp_path / "runs" / run_id / "manifest.json",
        "status": tmp_path / ".aros" / "runs" / run_id / "status.json",
        "prelaunch": tmp_path
        / ".aros"
        / "receipts"
        / f"{run_id}-prelaunch.json",
        "final": tmp_path / "runs" / run_id / "final.json",
    }[authority]
    alias = _install_json_crash_alias(path)
    before = {
        item: (item.lstat().st_ino, item.lstat().st_nlink, item.read_bytes())
        for item in (path, alias)
    }

    with pytest.raises(RunError):
        if authority in {"manifest", "status"}:
            service.status(
                run_id,
                reconcile=False,
                reader=store_module.read_json_strict_no_repair,
            )
        else:
            service.read_validated_final(
                run_id,
                reader=store_module.read_json_strict_no_repair,
            )

    assert {
        item: (item.lstat().st_ino, item.lstat().st_nlink, item.read_bytes())
        for item in (path, alias)
    } == before
    if authority in {"manifest", "status"}:
        service.status(run_id, reconcile=False)
    else:
        service.read_validated_final(run_id)
    assert not alias.exists()
    assert path.stat().st_nlink == 1


def test_read_run_inventory_preserves_crash_alias_without_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="inventory-crash-alias")
    run_id = str(manifest["run_id"])
    status = tmp_path / ".aros" / "runs" / run_id / "status.json"
    alias = _install_json_crash_alias(status)
    before = {
        path: (path.lstat().st_ino, path.lstat().st_nlink, path.read_bytes())
        for path in (status, alias)
    }
    synced: list[Path] = []
    monkeypatch.setattr(store_module, "_fsync_directory", synced.append)

    with pytest.raises(RunError, match="inventory|status|single-link"):
        runs_module.read_run_inventory(tmp_path)

    assert synced == []
    assert {
        path: (path.lstat().st_ino, path.lstat().st_nlink, path.read_bytes())
        for path in (status, alias)
    } == before


def test_read_run_inventory_rejects_invalid_state_instead_of_hiding_run(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="inventory-invalid-state")
    run_id = str(manifest["run_id"])
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    status = service.status(run_id, reconcile=False)
    status["state"] = "scientific_negative"
    atomic_write_json(status_path, status)

    with pytest.raises(RunError, match="state|status"):
        runs_module.read_run_inventory(tmp_path)


def test_read_run_inventory_is_deterministic_in_run_id_order(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    service, first = _prepare(tmp_path, key="inventory-order-first")
    second = service.prepare(
        [sys.executable, "-c", "pass"],
        idempotency_key="inventory-order-second",
        actor="test-principal",
        security_profile="trusted-local",
    )

    inventory = runs_module.read_run_inventory(tmp_path)

    assert inventory == runs_module.read_run_inventory(tmp_path)
    assert [item["run_id"] for item in inventory] == sorted(
        [str(first["run_id"]), str(second["run_id"])]
    )


@pytest.mark.parametrize("tamper", ["invalid_id", "nan"])
def test_read_run_inventory_rejects_invalid_identity_and_nonfinite_json(
    tmp_path: Path,
    tamper: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key=f"inventory-{tamper}")
    run_id = str(manifest["run_id"])
    if tamper == "invalid_id":
        invalid = tmp_path / "runs" / "RUN-invalid_id"
        invalid.mkdir()
        (invalid / "manifest.json").write_text("{}\n", encoding="utf-8")
    else:
        status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
        status = service.status(run_id, reconcile=False)
        status["updated_at"] = float("nan")
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")

    with pytest.raises(RunError, match="inventory|identity|JSON|status"):
        runs_module.read_run_inventory(tmp_path)


@pytest.mark.parametrize("shape", ["symlink", "file"])
def test_read_run_inventory_rejects_valid_looking_non_directory_identity(
    tmp_path: Path,
    shape: str,
) -> None:
    _init_clean_repo(tmp_path)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    invalid = runs_root / f"RUN-{shape}"
    if shape == "symlink":
        backing = tmp_path / "run-backing"
        backing.mkdir()
        invalid.symlink_to(backing, target_is_directory=True)
    else:
        invalid.write_text("not a Run directory\n", encoding="utf-8")

    with pytest.raises(RunError, match="identity|directory|inventory"):
        runs_module.read_run_inventory(tmp_path)


@pytest.mark.parametrize("tamper", ["base_only_running", "wrong_optional_type"])
def test_read_run_inventory_rejects_incomplete_or_mistyped_running_status(
    tmp_path: Path,
    tamper: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key=f"inventory-{tamper}")
    run_id = str(manifest["run_id"])
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    status = service.status(run_id, reconcile=False)
    status["state"] = "running"
    if tamper == "wrong_optional_type":
        status["process_pid"] = "123"
    atomic_write_json(status_path, status)

    with pytest.raises(RunError, match="running|status|process_pid"):
        runs_module.read_run_inventory(tmp_path)


@pytest.mark.parametrize(
    "tamper",
    (
        "path",
        "symlink",
        "hardlink",
        "size",
        "hash",
        "replacement",
        "read-race",
    ),
)
def test_verified_run_output_rejects_symlink_hardlink_hash_size_and_read_race(
    tmp_path: Path,
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('receipt-bound')"],
        key=f"verified-output-{tamper}",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    log = tmp_path / ".aros" / "runs" / run_id / "stdout.log"
    if tamper in {"path", "size", "hash"}:
        final_path = tmp_path / "runs" / run_id / "final.json"
        final = json.loads(final_path.read_text(encoding="utf-8"))
        if tamper == "path":
            final["stdout"]["path"] = f".aros/runs/{run_id}/decoy.log"
        elif tamper == "size":
            final["stdout"]["bytes"] += 1
        else:
            final["stdout"]["sha256"] = "0" * 64
        atomic_write_json(final_path, final)
    elif tamper == "symlink":
        backing = log.with_name("stdout-real.log")
        log.rename(backing)
        log.symlink_to(backing.name)
    elif tamper == "hardlink":
        log.with_name("stdout-alias.log").hardlink_to(log)
    elif tamper == "replacement":
        replacement = log.with_name("stdout-replacement.log")
        replacement.write_bytes(log.read_bytes())
        real_open = runs_module.os.open

        def replace_before_open(
            path: str | bytes | Path,
            flags: int,
            *args: object,
            **kwargs: object,
        ) -> int:
            if Path(path) == log and replacement.exists():
                replacement.replace(log)
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(runs_module.os, "open", replace_before_open)
    elif tamper == "read-race":
        replacement = log.with_name("stdout-post-read.log")
        replacement.write_bytes(log.read_bytes())
        real_read = runs_module.os.read

        def replace_after_read(descriptor: int, size: int) -> bytes:
            chunk = real_read(descriptor, size)
            if chunk and replacement.exists():
                replacement.replace(log)
            return chunk

        monkeypatch.setattr(runs_module.os, "read", replace_after_read)

    with pytest.raises(RunError, match="final|path|regular|link|verified"):
        service.read_verified_output(run_id, "stdout")


@pytest.mark.parametrize("tamper", ("launch-lineage", "schema", "timestamp"))
def test_read_verified_output_rejects_invalid_final_semantics(
    tmp_path: Path,
    tamper: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('final semantics')"],
        key=f"verified-final-{tamper}",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    final_path = tmp_path / "runs" / run_id / "final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    if tamper == "launch-lineage":
        final["launch_receipt_sha256"] = "b" * 64
    elif tamper == "schema":
        final["schema_version"] = 2
    else:
        final["finalized_at"] = "2026-08-04T00:00:00.000Z"
        assert final["finalized_at"] != final["finished_at"]
    atomic_write_json(final_path, final)

    with pytest.raises(RunError, match="final|lineage|timestamp"):
        service.read_verified_output(run_id, "stdout")


@pytest.mark.parametrize(
    "forgery",
    ("runner_invocation", "host"),
)
def test_validated_final_rejects_forged_prelaunch_provenance(
    tmp_path: Path,
    forgery: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('provenance')"],
        key=f"forged-final-{forgery}",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    receipt_path = tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    final_path = tmp_path / "runs" / run_id / "final.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    final = json.loads(final_path.read_text(encoding="utf-8"))
    if forgery == "runner_invocation":
        receipt[forgery] = ["/forged/runner", run_id]
    else:
        receipt[forgery] = f"forged-{forgery}"
    receipt["receipt_sha256"] = record_sha256(receipt, "receipt_sha256")
    status["launch_receipt_sha256"] = receipt["receipt_sha256"]
    final["launch_receipt_sha256"] = receipt["receipt_sha256"]
    atomic_write_json(receipt_path, receipt)
    atomic_write_json(status_path, status)
    atomic_write_json(final_path, final)

    with pytest.raises(RunError, match="prelaunch|host|provenance"):
        service.read_validated_final(run_id)


def test_mutable_status_actor_cannot_invalidate_final_and_is_reconciled(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('mutable status')"],
        key="forged-mutable-status-actor",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    prelaunch_path = (
        tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
    )
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    final_path = tmp_path / "runs" / run_id / "final.json"
    prelaunch = json.loads(prelaunch_path.read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final_bytes = final_path.read_bytes()
    status["actor"] = "forged-mutable-actor"
    atomic_write_json(status_path, status)
    forged_status_bytes = status_path.read_bytes()

    assert service.read_validated_final(run_id) == final
    assert status_path.read_bytes() == forged_status_bytes

    reconciled = service.status(run_id)

    assert reconciled["actor"] == prelaunch["actor"]
    assert json.loads(status_path.read_text(encoding="utf-8"))["actor"] == prelaunch[
        "actor"
    ]
    assert final_path.read_bytes() == final_bytes


@pytest.mark.parametrize(
    "tamper",
    ("empty-actor", "empty-host", "missing-final-host", "empty-final-host"),
)
def test_validated_final_requires_nonempty_attributed_provenance(
    tmp_path: Path,
    tamper: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('attributed')"],
        key=f"attributed-final-{tamper}",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    prelaunch_path = tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    final_path = tmp_path / "runs" / run_id / "final.json"
    prelaunch = json.loads(prelaunch_path.read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    final = json.loads(final_path.read_text(encoding="utf-8"))
    if tamper == "empty-actor":
        prelaunch["actor"] = ""
        status["actor"] = ""
    elif tamper == "empty-host":
        prelaunch["host"] = ""
        status["host"] = ""
        final.pop("host")
    elif tamper == "missing-final-host":
        final.pop("host")
    else:
        final["host"] = ""
    if tamper in {"empty-actor", "empty-host"}:
        prelaunch["receipt_sha256"] = record_sha256(
            prelaunch,
            "receipt_sha256",
        )
        status["launch_receipt_sha256"] = prelaunch["receipt_sha256"]
        final["launch_receipt_sha256"] = prelaunch["receipt_sha256"]
    atomic_write_json(prelaunch_path, prelaunch)
    atomic_write_json(status_path, status)
    atomic_write_json(final_path, final)

    with pytest.raises(RunError, match="prelaunch|provenance|host|final"):
        service.read_validated_final(run_id)


def test_prepare_defaults_to_isolated_linux_and_freezes_capability_policy(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / ".keep").write_text("", encoding="utf-8")
    _git(tmp_path, "add", "scratch/.keep")
    _git(tmp_path, "commit", "-qm", "add isolated output directory")
    service = RunService(tmp_path)

    manifest = service.prepare(
        ["/usr/bin/python3", "-c", "print('isolated')"],
        idempotency_key="isolated-default",
        actor="principal",
        writable_paths=["scratch"],
    )

    assert manifest["security_profile"] == "isolated-linux"
    assert manifest["writable_paths"] == ["scratch"]
    assert manifest["network_policy"] == NETWORK_POLICY
    assert manifest["environment_policy"]["kind"] == ENVIRONMENT_POLICY

    with pytest.raises(RunError, match="idempotency key.*different manifest"):
        service.prepare(
            ["/usr/bin/python3", "-c", "print('isolated')"],
            idempotency_key="isolated-default",
            actor="principal",
            security_profile="trusted-local",
        )


def test_prepare_requires_clean_git_and_contained_real_cwd(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("not checkpointed", encoding="utf-8")
    service = RunService(tmp_path)

    with pytest.raises(RunError, match="clean Git"):
        service.prepare(
            ["true"],
            idempotency_key="dirty",
            actor="principal",
            security_profile="trusted-local",
        )
    assert not (tmp_path / "runs").exists()

    (tmp_path / "dirty.txt").unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunError, match="cwd.*workspace"):
        service.prepare(
            ["true"],
            cwd="escape",
            idempotency_key="escape",
            actor="principal",
            security_profile="trusted-local",
        )


def test_prepare_idempotency_is_locked_and_never_creates_a_second_run(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)

    def prepare_once() -> dict[str, object]:
        return service.prepare(
            [sys.executable, "-c", "print('once')"],
            idempotency_key="one-logical-launch",
            actor="principal",
            security_profile="trusted-local",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: prepare_once(), range(2)))

    assert first == second
    manifests = list((tmp_path / "runs").glob("*/manifest.json"))
    assert len(manifests) == 1
    with pytest.raises(RunError, match="idempotency key.*already belongs"):
        service.prepare(
            [sys.executable, "-c", "print('different')"],
            idempotency_key="one-logical-launch",
            actor="principal",
            security_profile="trusted-local",
        )
    assert list((tmp_path / "runs").glob("*/manifest.json")) == manifests


def test_prepare_allows_other_valid_uncheckpointed_run_artifacts(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    service, first = _prepare(tmp_path, key="first")

    second = service.prepare(
        [sys.executable, "-c", "print('second')"],
        idempotency_key="second",
        actor="principal",
        security_profile="trusted-local",
    )

    assert first["run_id"] != second["run_id"]
    assert len(list((tmp_path / "runs").glob("*/manifest.json"))) == 2


def test_environment_fingerprint_does_not_derive_from_unlisted_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_clean_repo(tmp_path)
    monkeypatch.setenv("AROS_SECRET_SENTINEL", "first-secret-value")
    service, first = _prepare(tmp_path, key="first-environment")
    monkeypatch.setenv("AROS_SECRET_SENTINEL", "different-secret-value")

    second = service.prepare(
        [sys.executable, "-c", "print('second')"],
        idempotency_key="second-environment",
        actor="test-principal",
        security_profile="trusted-local",
    )

    assert first["environment_sha256"] == second["environment_sha256"]
    assert "AROS_SECRET_SENTINEL" not in first["environment_ref"]["keys"]


def test_start_fails_closed_when_tmux_is_missing(tmp_path: Path, monkeypatch) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = str(manifest["run_id"])
    monkeypatch.setattr("arbor.aros.runs.shutil.which", lambda _name: None)

    with pytest.raises(RunError, match="tmux.*required"):
        service.start(run_id)

    assert service.status(run_id, reconcile=False)["state"] == "prepared"
    assert list((tmp_path / ".aros" / "receipts").glob("*.json")) == []
    assert list((tmp_path / ".aros" / "events").glob("*.json")) == []


def test_carrier_launch_failure_uses_immutable_lineage_for_terminal_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="carrier-launch-failure")
    run_id = str(manifest["run_id"])
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    real_run = subprocess.run

    def fail_tmux(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] != "/test/tmux":
            return real_run(command, **kwargs)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        atomic_write_json(
            status_path,
            {
                **status,
                "actor": "forged-status-actor",
                "host": "forged-status-host",
                "launch_receipt_sha256": "f" * 64,
            },
        )
        return subprocess.CompletedProcess(command, 7, "", "carrier refused")

    monkeypatch.setattr(runs_module.shutil, "which", lambda _name: "/test/tmux")
    monkeypatch.setattr(runs_module.subprocess, "run", fail_tmux)

    with pytest.raises(RunError, match="tmux launch failed"):
        service.start(run_id)

    prelaunch = json.loads(
        (
            tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
        ).read_text(encoding="utf-8")
    )
    final = service.read_validated_final(run_id)
    expected_status = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "failed_process",
        "manifest_sha256": manifest["manifest_sha256"],
        "actor": prelaunch["actor"],
        "carrier": prelaunch["carrier"],
        "tmux_session": prelaunch["tmux_session"],
        "host": prelaunch["host"],
        "launch_receipt_sha256": prelaunch["receipt_sha256"],
        "launched_at": prelaunch["created_at"],
        "started_at": final["started_at"],
        "exit_code": None,
        "finished_at": final["finished_at"],
        "heartbeat_at": final["finished_at"],
        "final_ref": f"runs/{run_id}/final.json",
        "updated_at": final["finished_at"],
    }

    assert final["host"] == prelaunch["host"]
    assert final["launch_receipt_sha256"] == prelaunch["receipt_sha256"]
    assert json.loads(status_path.read_text(encoding="utf-8")) == expected_status


def test_carrier_failure_preserves_runner_winner_final_and_reattaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    attempt_path = tmp_path / "attempts.txt"
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "path = Path('attempts.txt')\n"
                "prior = path.read_text() if path.exists() else ''\n"
                "path.write_text(prior + 'attempt\\n')\n"
            ),
        ],
        key="runner-wins-carrier-failure",
    )
    run_id = str(manifest["run_id"])
    final_path = tmp_path / "runs" / run_id / "final.json"
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    event_path = (
        tmp_path / ".aros" / "events" / f"EVT-{run_id}-completed.json"
    )
    real_run = subprocess.run
    real_create_json = runs_module.create_json
    final_create_results: list[bool] = []
    event_create_results: list[bool] = []
    tmux_calls = 0
    winner_final_bytes: bytes | None = None

    def track_create_json(path: str | Path, value: object) -> bool:
        created = real_create_json(path, value)
        if Path(path) == final_path:
            final_create_results.append(created)
        elif Path(path) == event_path:
            event_create_results.append(created)
        return created

    def seal_runner_before_tmux_failure(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal tmux_calls, winner_final_bytes
        if command[0] != "/test/tmux":
            return real_run(command, **kwargs)
        tmux_calls += 1
        assert runner_module.run(str(tmp_path), run_id) == 0
        assert event_path.is_file()
        event_path.unlink()
        winner_final_bytes = final_path.read_bytes()
        return subprocess.CompletedProcess(command, 7, "", "carrier refused")

    monkeypatch.setattr(runs_module, "create_json", track_create_json)
    monkeypatch.setattr(runs_module.shutil, "which", lambda _name: "/test/tmux")
    monkeypatch.setattr(
        runs_module.subprocess,
        "run",
        seal_runner_before_tmux_failure,
    )

    with pytest.raises(RunError, match="tmux launch failed"):
        service.start(run_id)

    assert winner_final_bytes is not None
    assert final_create_results == [False]
    assert event_create_results == [True]
    assert final_path.read_bytes() == winner_final_bytes
    final = service.read_validated_final(run_id)
    prelaunch = json.loads(
        (
            tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
        ).read_text(encoding="utf-8")
    )
    expected_status = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "completed",
        "manifest_sha256": manifest["manifest_sha256"],
        "actor": prelaunch["actor"],
        "carrier": prelaunch["carrier"],
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
    event = json.loads(event_path.read_text(encoding="utf-8"))

    assert final["state"] == "completed"
    assert json.loads(status_path.read_text(encoding="utf-8")) == expected_status
    assert event["created_at"] == final["finished_at"]
    assert event["summary"] == "Run reached process state completed."
    assert "failed_process" not in json.dumps(event)
    assert attempt_path.read_text(encoding="utf-8").splitlines() == ["attempt"]

    replayed = service.start(run_id)

    assert replayed == expected_status
    assert tmux_calls == 1
    assert attempt_path.read_text(encoding="utf-8").splitlines() == ["attempt"]
    assert final_path.read_bytes() == winner_final_bytes


def test_isolated_start_probe_failure_precedes_launch_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_clean_repo(tmp_path)
    service = RunService(tmp_path)
    manifest = service.prepare(
        ["/usr/bin/python3", "-c", "print('must not launch')"],
        idempotency_key="isolation-unavailable",
        actor="principal",
    )
    run_id = str(manifest["run_id"])

    def unavailable() -> None:
        raise IsolationError("test kernel capability is unavailable")

    monkeypatch.setattr("arbor.aros.runs.shutil.which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr("arbor.aros.runs.probe_isolated_linux", unavailable)

    with pytest.raises(RunError, match="isolated-linux.*unavailable"):
        service.start(run_id)

    assert service.status(run_id, reconcile=False)["state"] == "prepared"
    assert list((tmp_path / ".aros" / "receipts").glob("*.json")) == []
    assert list((tmp_path / ".aros" / "events").glob("*.json")) == []


def test_real_isolated_tmux_run_enforces_manifest_capabilities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _require_tmux()
    try:
        probe_isolated_linux()
    except IsolationError as error:
        pytest.skip(f"isolated-linux is unavailable: {error}")
    _init_clean_repo(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / ".keep").write_text("", encoding="utf-8")
    _git(tmp_path, "add", "scratch/.keep")
    _git(tmp_path, "commit", "-qm", "add isolated output directory")
    outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    outside.write_text("host secret", encoding="utf-8")
    monkeypatch.setenv("AROS_SECRET_SENTINEL", "must-not-cross-boundary")
    code = r"""
import json, os, pathlib, socket, sys
root = pathlib.Path.cwd()
def attempt(operation):
    try:
        return {"ok": True, "value": operation()}
    except OSError as error:
        return {"ok": False, "errno": error.errno}
print(json.dumps({
    "outside_read": attempt(lambda: pathlib.Path(sys.argv[1]).read_text()),
    "root_write": attempt(lambda: (root / "forbidden.txt").write_text("bad")),
    "scratch_write": attempt(lambda: (root / "scratch" / "result.txt").write_text("ok")),
    "network": attempt(socket.socket),
    "secret": os.environ.get("AROS_SECRET_SENTINEL"),
    "profile": os.environ.get("AROS_SECURITY_PROFILE"),
}))
"""
    service = RunService(tmp_path)
    manifest = service.prepare(
        ["/usr/bin/python3", "-c", code, str(outside)],
        idempotency_key="real-isolated-run",
        actor="principal",
        writable_paths=["scratch"],
    )
    run_id = str(manifest["run_id"])

    service.start(run_id)
    status = _wait_for_state(service, run_id)
    observed = json.loads(service.tail(run_id))

    assert status["state"] == "completed"
    assert observed["outside_read"]["ok"] is False
    assert observed["root_write"]["ok"] is False
    assert observed["scratch_write"]["ok"] is True
    assert observed["network"]["ok"] is False
    assert observed["secret"] is None
    assert observed["profile"] == "isolated-linux"
    assert (scratch / "result.txt").read_text(encoding="utf-8") == "ok"
    assert not (tmp_path / "forbidden.txt").exists()


def test_bundle_run_reads_candidate_and_apparatus_but_cannot_write_them(
    tmp_path: Path,
) -> None:
    try:
        probe_isolated_linux()
    except IsolationError as error:
        pytest.skip(f"isolated-linux is unavailable: {error}")
    scorer = r"""
import json, pathlib
apparatus = pathlib.Path(__file__).resolve().parent
bundle = apparatus.parent
candidate = bundle / "candidate"
temporary = bundle / "tmp"
def attempt(operation):
    try:
        return {"ok": True, "value": operation()}
    except OSError as error:
        return {"ok": False, "errno": error.errno}
print(json.dumps({
    "candidate_read": attempt(lambda: (candidate / "candidate.txt").read_text()),
    "apparatus_read": attempt(lambda: (apparatus / "score.py").read_text()),
    "candidate_write": attempt(lambda: (candidate / "blocked.txt").write_text("bad")),
    "apparatus_write": attempt(lambda: (apparatus / "blocked.txt").write_text("bad")),
    "bundle_write": attempt(lambda: (bundle / "blocked.txt").write_text("bad")),
    "temp_write": attempt(lambda: (temporary / "allowed.txt").write_text("ok")),
}))
"""
    _repository, bundle = _create_test_execution_bundle(
        tmp_path,
        "EVAL-isolation",
        candidate_files={"candidate.txt": "candidate bytes\n"},
        apparatus_files={"score.py": scorer},
    )
    service = RunService(tmp_path)
    manifest = service.prepare_bundle(
        bundle,
        ["/usr/bin/python3", "../apparatus/score.py"],
        cwd=".",
        timeout_seconds=10,
        success_exit_codes=[0],
        idempotency_key="bundle-isolation",
        actor="test-principal",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    status = service.status(run_id)
    observed = json.loads(service.tail(run_id))

    assert status["state"] == "completed"
    assert observed["candidate_read"] == {
        "ok": True,
        "value": "candidate bytes\n",
    }
    assert observed["apparatus_read"]["ok"] is True
    assert observed["candidate_write"]["ok"] is False
    assert observed["apparatus_write"]["ok"] is False
    assert observed["bundle_write"]["ok"] is False
    assert observed["temp_write"] == {"ok": True, "value": 2}
    assert (bundle.temp / "allowed.txt").read_text(encoding="utf-8") == "ok"
    assert not (bundle.candidate.path / "blocked.txt").exists()
    assert not (bundle.apparatus.path / "blocked.txt").exists()
    assert not (bundle.root / "blocked.txt").exists()
    assert (tmp_path / ".aros" / "runs" / run_id / "stdout.log").is_file()
    assert not (bundle.root / ".aros").exists()


def test_runner_revalidates_both_trees_immediately_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, bundle = _create_test_execution_bundle(
        tmp_path,
        "EVAL-pre-spawn",
        candidate_files={"candidate.txt": "candidate\n"},
    )
    service = RunService(tmp_path)
    manifest = service.prepare_bundle(
        bundle,
        ["/usr/bin/python3", "-c", "pass"],
        cwd=".",
        timeout_seconds=10,
        success_exit_codes=[0],
        idempotency_key="bundle-pre-spawn",
        actor="test-principal",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    events: list[str] = []

    def observe_validation(observed_repository, observed_bundle) -> None:
        assert observed_repository == repository
        assert observed_bundle.candidate == bundle.candidate
        assert observed_bundle.apparatus == bundle.apparatus
        validate_execution_bundle(observed_repository, observed_bundle)
        events.append("validated-both-trees")

    real_spawn_process = processes_module.spawn_process

    def observe_spawn(*args: object, **kwargs: object):
        if args and args[0] == manifest["argv"]:
            events.append("spawn")
            assert events == ["validated-both-trees", "spawn"]
            assert "parent_death" not in kwargs
            assert callable(kwargs.get("preexec_fn"))
        return real_spawn_process(*args, **kwargs)

    monkeypatch.setattr(
        runner_module,
        "validate_execution_bundle",
        observe_validation,
        raising=False,
    )
    monkeypatch.setattr(processes_module, "spawn_process", observe_spawn)

    assert runner_module.run(str(tmp_path), run_id) == 0
    assert events == ["validated-both-trees", "spawn"]


@pytest.mark.parametrize("missing_checkout", ("candidate", "apparatus"))
def test_bundle_runner_finalizes_missing_checkout_marker_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_checkout: str,
) -> None:
    _repository, bundle = _create_test_execution_bundle(
        tmp_path,
        f"EVAL-missing-{missing_checkout}-marker",
    )
    service = RunService(tmp_path)
    manifest = service.prepare_bundle(
        bundle,
        ["/usr/bin/python3", "-c", "pass"],
        cwd=".",
        timeout_seconds=10,
        success_exit_codes=[0],
        idempotency_key=f"bundle-missing-{missing_checkout}-marker",
        actor="test-principal",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    checkout = getattr(bundle, missing_checkout)
    (checkout.path / ".git").unlink()
    spawned = False
    real_spawn_process = processes_module.spawn_process

    def observe_spawn(*args: object, **kwargs: object):
        nonlocal spawned
        if args and args[0] == manifest["argv"]:
            spawned = True
        return real_spawn_process(*args, **kwargs)

    monkeypatch.setattr(processes_module, "spawn_process", observe_spawn)

    assert runner_module.run(str(tmp_path), run_id) == 1

    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )
    assert final["state"] == "failed_process"
    assert "process launch failed" in final["error"]
    assert service.status(run_id, reconcile=False)["state"] == "failed_process"
    assert spawned is False
    assert bundle.candidate.path.is_dir()
    assert bundle.apparatus.path.is_dir()
    assert bundle.temp.is_dir()
    registrations = _git(tmp_path, "worktree", "list", "--porcelain")
    assert str(bundle.candidate.path) in registrations
    assert str(bundle.apparatus.path) in registrations


def test_bundle_runner_finalizes_symlink_loop_cwd_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository, bundle = _create_test_execution_bundle(
        tmp_path,
        "EVAL-cwd-symlink-loop",
        candidate_files={"work/.keep": ""},
    )
    service = RunService(tmp_path)
    manifest = service.prepare_bundle(
        bundle,
        ["/usr/bin/python3", "-c", "pass"],
        cwd="work",
        timeout_seconds=10,
        success_exit_codes=[0],
        idempotency_key="bundle-cwd-symlink-loop",
        actor="test-principal",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    cwd = bundle.candidate.path / "work"
    preserved = bundle.candidate.path / "work-preserved"
    cwd.rename(preserved)
    cwd.symlink_to("work", target_is_directory=True)
    spawned = False
    real_spawn_process = processes_module.spawn_process

    def observe_spawn(*args: object, **kwargs: object):
        nonlocal spawned
        if args and args[0] == manifest["argv"]:
            spawned = True
        return real_spawn_process(*args, **kwargs)

    monkeypatch.setattr(processes_module, "spawn_process", observe_spawn)

    assert runner_module.run(str(tmp_path), run_id) == 1

    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )
    assert final["state"] == "failed_process"
    assert "process launch failed" in final["error"]
    assert service.status(run_id, reconcile=False)["state"] == "failed_process"
    assert spawned is False
    assert cwd.is_symlink()
    assert preserved.is_dir()
    assert bundle.apparatus.path.is_dir()
    assert bundle.temp.is_dir()
    registrations = _git(tmp_path, "worktree", "list", "--porcelain")
    assert str(bundle.candidate.path) in registrations
    assert str(bundle.apparatus.path) in registrations


@pytest.mark.parametrize(
    "drift",
    (
        "path",
        "symlink",
        "head",
        "tree",
        "filter",
        "candidate-filter",
        "apparatus-filter",
    ),
)
def test_bundle_run_rejects_path_symlink_head_tree_or_filter_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    _repository, bundle = _create_test_execution_bundle(
        tmp_path,
        f"EVAL-drift-{drift}",
        candidate_files={
            "candidate.txt": "candidate\n",
            ".gitattributes": (
                "candidate.txt filter=runtime\n"
                "apparatus.txt filter=runtime\n"
            ),
        },
    )
    service = RunService(tmp_path)
    manifest = service.prepare_bundle(
        bundle,
        ["/usr/bin/python3", "-c", "pass"],
        cwd=".",
        timeout_seconds=10,
        success_exit_codes=[0],
        idempotency_key=f"bundle-drift-{drift}",
        actor="test-principal",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    driver_marker: Path | None = None
    if drift == "path":
        (bundle.root / "unexpected").mkdir()
    elif drift == "symlink":
        bundle.temp.rmdir()
        outside = tmp_path.parent / f"{tmp_path.name}-outside-temp"
        outside.mkdir()
        bundle.temp.symlink_to(outside, target_is_directory=True)
    elif drift == "head":
        _git(bundle.candidate.path, "reset", "--hard", bundle.apparatus.commit)
    elif drift == "tree":
        (bundle.apparatus.path / "apparatus.txt").write_text(
            "drifted apparatus bytes\n",
            encoding="utf-8",
        )
    elif drift == "filter":
        _git(tmp_path, "config", "filter.runtime.smudge", "cat")
    else:
        checkout_name = drift.removesuffix("-filter")
        checkout = getattr(bundle, checkout_name)
        driver_marker = tmp_path / f"{checkout_name}-filter-driver-ran"
        _git(tmp_path, "config", "extensions.worktreeConfig", "true")
        _git(
            checkout.path,
            "config",
            "--worktree",
            "filter.runtime.clean",
            f"sh -c 'touch {driver_marker}; cat'",
        )
    spawned = False
    real_spawn_process = processes_module.spawn_process

    def observe_spawn(*args: object, **kwargs: object):
        nonlocal spawned
        if args and args[0] == manifest["argv"]:
            spawned = True
        return real_spawn_process(*args, **kwargs)

    monkeypatch.setattr(processes_module, "spawn_process", observe_spawn)

    assert runner_module.run(str(tmp_path), run_id) == 1

    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )
    assert final["state"] == "failed_process"
    assert final["manifest_sha256"] == manifest["manifest_sha256"]
    assert "process launch failed" in final["error"]
    assert spawned is False
    assert driver_marker is None or not driver_marker.exists()
    assert bundle.candidate.path.is_dir()
    assert bundle.apparatus.path.is_dir()
    registrations = _git(tmp_path, "worktree", "list", "--porcelain")
    assert str(bundle.candidate.path) in registrations
    assert str(bundle.apparatus.path) in registrations


def test_start_rejects_manifest_tamper_and_unrelated_dirty_work(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = str(manifest["run_id"])
    manifest_path = tmp_path / "runs" / run_id / "manifest.json"
    tampered = dict(manifest)
    tampered["timeout_seconds"] = 999
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(RunError, match="manifest hash"):
        service.start(run_id)
    with pytest.raises(RunError, match="manifest hash"):
        service.status(run_id)
    with pytest.raises(RunError, match="manifest hash"):
        service.list()

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(RunError, match="clean Git"):
        service.start(run_id)


@pytest.mark.parametrize("forged_field", ("runner_invocation", "actor", "host"))
def test_start_rejects_forged_self_hashed_prelaunch_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged_field: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        key=f"forged-prelaunch-{forged_field}",
    )
    run_id = str(manifest["run_id"])
    receipt = _test_prelaunch_receipt(tmp_path, manifest)
    if forged_field == "runner_invocation":
        receipt[forged_field] = ["/forged/runner", run_id]
    else:
        receipt[forged_field] = f"forged-{forged_field}"
    receipt["receipt_sha256"] = record_sha256(receipt, "receipt_sha256")
    atomic_write_json(
        tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json",
        receipt,
    )
    real_run = runs_module.subprocess.run
    tmux_calls = 0

    def reject_tmux(command: list[str], **kwargs: object):
        nonlocal tmux_calls
        if command[0] == "/test/tmux":
            tmux_calls += 1
            return subprocess.CompletedProcess(command, 1, "", "must not launch")
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runs_module.shutil, "which", lambda _name: "/test/tmux")
    monkeypatch.setattr(runs_module.subprocess, "run", reject_tmux)

    with pytest.raises(RunError, match="prelaunch"):
        service.start(run_id)

    assert tmux_calls == 0
    assert service.status(run_id, reconcile=False)["state"] == "prepared"


def test_real_tmux_run_survives_client_and_writes_final_receipts(tmp_path: Path) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            "import time; print('begin', flush=True); time.sleep(.25); print('end', flush=True)",
        ],
    )
    run_id = str(manifest["run_id"])

    launched = service.start(run_id)
    del launched
    del service  # no in-memory Principal/service state is needed for completion
    recovered = RunService(tmp_path)
    status = _wait_for_state(recovered, run_id)

    assert status["state"] == "completed"
    final_path = tmp_path / "runs" / run_id / "final.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    assert final["state"] == "completed"
    assert final["exit_code"] == 0
    for field in (
        "base_commit",
        "argv",
        "cwd",
        "timeout_seconds",
        "idempotency_key",
        "security_profile",
        "manifest_sha256",
        "actor",
        "started_at",
        "finished_at",
    ):
        assert final[field] == manifest[field] if field in manifest else final[field]
    assert final["stdout"]["sha256"]
    assert final["stdout"]["bytes"] > 0
    assert final["finalized_at"] == final["finished_at"]
    assert final["resource_usage"]["wall_seconds"] == final["duration_seconds"]
    assert "begin\nend\n" == recovered.tail(run_id, stream="stdout")
    assert (tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json").is_file()
    launch_receipt = json.loads(
        (tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json").read_text(
            encoding="utf-8"
        )
    )
    assert launch_receipt["host"]
    assert launch_receipt["runner_version"] == 1
    assert launch_receipt["runner_invocation"][1:4] == [
        "-B",
        "-m",
        "arbor.aros.runner",
    ]
    assert len(launch_receipt["receipt_sha256"]) == 64
    assert final["launch_receipt_sha256"] == launch_receipt["receipt_sha256"]
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".aros" / "events").glob("*.json")
    ]
    assert {event["kind"] for event in events} == {
        "run_launch_requested",
        "run_completed",
    }
    assert all(event["source_ref"] == f"runs/{run_id}" for event in events)
    later = recovered.prepare(
        [sys.executable, "-c", "print('later')"],
        idempotency_key="later-run",
        actor="test-principal",
        security_profile="trusted-local",
    )
    assert later["run_id"] != run_id


def test_concurrent_start_reattaches_without_a_second_tmux_launch(tmp_path: Path) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(.3)"],
        key="concurrent-start",
    )
    run_id = str(manifest["run_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: service.start(run_id), range(2)))

    _wait_for_state(service, run_id)
    assert {status["run_id"] for status in statuses} == {run_id}
    assert len(
        list((tmp_path / ".aros" / "receipts").glob(f"{run_id}-prelaunch.json"))
    ) == 1
    launch_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".aros" / "events").glob("*.json")
        if "launch-requested" in path.name
    ]
    assert len(launch_events) == 1


def test_repeated_start_reconciles_stale_launched_status_to_lost(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="stale-repeated-start")
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status.update(
        {
            "state": "launched",
            "actor": "test-principal",
            "carrier": "tmux",
            "tmux_session": f"aros-{run_id.lower()}",
            "host": "test-host",
            "launch_receipt_sha256": "a" * 64,
            "launched_at": manifest["created_at"],
        }
    )
    atomic_write_json(runtime / "status.json", status)
    monkeypatch.setattr("arbor.aros.runs._tmux_session_exists", lambda _name: False)

    repeated = service.start(run_id)

    assert repeated["state"] == "lost"
    assert repeated["reason"] == "process_absent_without_final_receipt"


def test_repeated_start_reconciles_existing_final_before_reattach(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="completed-repeated-start")
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status["state"] = "launched"
    atomic_write_json(runtime / "status.json", status)

    repeated = service.start(run_id)

    assert repeated["state"] == "completed"
    assert repeated["final_ref"] == f"runs/{run_id}/final.json"
    assert service.status(run_id, reconcile=False)["state"] == "completed"


@pytest.mark.parametrize("operation", ("status", "reconcile"))
def test_reconcile_waits_for_inflight_launch_lock(tmp_path, monkeypatch, operation):
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_run = subprocess.run

    def paused_tmux(command, **_kwargs):
        if command[0] == "/test/tmux" and "new-session" in command:
            entered.set()
            assert release.wait(5)
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(command, **_kwargs)

    import arbor.aros.runs as runs_module

    monkeypatch.setattr(runs_module.shutil, "which", lambda _name: "/test/tmux")
    monkeypatch.setattr(runs_module.subprocess, "run", paused_tmux)
    monkeypatch.setattr(runs_module, "_tmux_session_exists", lambda _name: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        launch = pool.submit(service.start, manifest["run_id"])
        assert entered.wait(5)
        observer = pool.submit(getattr(service, operation), manifest["run_id"])
        try:
            with pytest.raises(TimeoutError):
                observer.result(timeout=0.2)
        finally:
            release.set()
        launch.result(timeout=5)
        assert observer.result(timeout=5)["state"] != "lost"


def test_timeout_is_a_process_state_with_automatic_final(tmp_path: Path) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.2,
    )
    run_id = str(manifest["run_id"])

    service.start(run_id)
    status = _wait_for_state(service, run_id)
    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(
            encoding="utf-8"
        )
    )

    assert status["state"] == "timed_out"
    assert final["state"] == "timed_out"
    assert final["timeout_seconds"] == 0.2
    assert "scientific" not in json.dumps(final).lower()


def test_runner_waits_for_same_group_descendant_and_late_stdout(
    tmp_path: Path,
) -> None:
    if not Path("/proc/self/stat").is_file():
        pytest.skip("Linux /proc is unavailable")
    _init_clean_repo(tmp_path)
    descendant = (
        "import time\n"
        "from pathlib import Path\n"
        "Path('descendant.ready').touch()\n"
        "while not Path('descendant.release').exists(): time.sleep(0.01)\n"
        "print('late descendant stdout',flush=True)\n"
    )
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", _leader_with_descendant(descendant)],
        key="run-late-descendant-output",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    leader_release = tmp_path / "leader.release"
    descendant_release = tmp_path / "descendant.release"
    descendant_pid: int | None = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        runner = pool.submit(runner_module.run, str(tmp_path), run_id)
        try:
            running = _wait_for_running_status(service, run_id)
            deadline = time.monotonic() + 5
            while not (tmp_path / "leader.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            descendant_pid = int((tmp_path / "descendant.pid").read_text())
            leader_release.touch()
            _wait_for_process_exit(int(running["process_pid"]))
            assert _linux_process_is_live(descendant_pid)
            assert not runner.done()
            assert not (tmp_path / "runs" / run_id / "final.json").exists()
            descendant_release.touch()
            assert runner.result(timeout=5) == 0
        finally:
            leader_release.touch(exist_ok=True)
            descendant_release.touch(exist_ok=True)
            if descendant_pid is not None and _linux_process_is_live(descendant_pid):
                os.kill(descendant_pid, signal.SIGKILL)
    final = service.read_validated_final(run_id)
    assert final["state"] == "completed"
    assert service.read_verified_output(run_id, "stdout") == (
        b"leader stdout\nlate descendant stdout\n"
    )


def test_timeout_after_leader_exit_drains_same_group_descendant(
    tmp_path: Path,
) -> None:
    if not Path("/proc/self/stat").is_file():
        pytest.skip("Linux /proc is unavailable")
    _init_clean_repo(tmp_path)
    descendant = (
        "import signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path('descendant.ready').touch()\n"
        "time.sleep(30)\n"
    )
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", _leader_with_descendant(descendant)],
        timeout=0.3,
        key="run-timeout-after-leader-exit",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    descendant_pid: int | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            runner = pool.submit(runner_module.run, str(tmp_path), run_id)
            running = _wait_for_running_status(service, run_id)
            deadline = time.monotonic() + 5
            while not (tmp_path / "leader.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            descendant_pid = int((tmp_path / "descendant.pid").read_text())
            (tmp_path / "leader.release").touch()
            _wait_for_process_exit(int(running["process_pid"]))
            assert runner.result(timeout=5) == 0
        final = service.read_validated_final(run_id)
        assert final["state"] == "timed_out"
        assert final["signal_sequence"] == ["TERM", "KILL"]
        _wait_for_process_exit(descendant_pid)
    finally:
        (tmp_path / "leader.release").touch(exist_ok=True)
        if descendant_pid is not None and _linux_process_is_live(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_stop_after_leader_exit_drains_same_group_descendant(
    tmp_path: Path,
) -> None:
    if not Path("/proc/self/stat").is_file():
        pytest.skip("Linux /proc is unavailable")
    _init_clean_repo(tmp_path)
    descendant = (
        "import signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path('descendant.ready').touch()\n"
        "time.sleep(30)\n"
    )
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", _leader_with_descendant(descendant)],
        key="run-stop-after-leader-exit",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    descendant_pid: int | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            runner = pool.submit(runner_module.run, str(tmp_path), run_id)
            running = _wait_for_running_status(service, run_id)
            deadline = time.monotonic() + 5
            while not (tmp_path / "leader.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            descendant_pid = int((tmp_path / "descendant.pid").read_text())
            (tmp_path / "leader.release").touch()
            _wait_for_process_exit(int(running["process_pid"]))
            receipt = service.stop(run_id, actor="owner", reason="stop remaining group")
            assert runner.result(timeout=5) == 0
        final = service.read_validated_final(run_id)
        assert receipt["delivered"] is True
        assert final["state"] == "cancelled"
        assert final["signal_sequence"] == ["TERM", "KILL"]
        _wait_for_process_exit(descendant_pid)
    finally:
        (tmp_path / "leader.release").touch(exist_ok=True)
        if descendant_pid is not None and _linux_process_is_live(descendant_pid):
            os.kill(descendant_pid, signal.SIGKILL)


def test_public_stop_after_leader_exit_drains_adopted_descendant(
    tmp_path: Path,
) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    descendant = (
        "import signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path('descendant.ready').touch()\n"
        "time.sleep(30)\n"
    )
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            _leader_with_descendant(descendant, escaped=True),
        ],
        timeout=30,
        key="run-stop-adopted-descendant",
    )
    run_id = str(manifest["run_id"])
    descendant_pid: int | None = None
    try:
        service.start(run_id)
        running = _wait_for_running_status(service, run_id)
        deadline = time.monotonic() + 5
        while not (tmp_path / "leader.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        descendant_pid = int((tmp_path / "descendant.pid").read_text())
        (tmp_path / "leader.release").touch()
        _wait_for_process_exit(int(running["process_pid"]))
        assert service.status(run_id)["state"] == "running"
        assert _linux_process_is_live(descendant_pid)

        receipt = service.stop(run_id, actor="owner", reason="stop adopted child")
        final = service.read_validated_final(run_id)

        assert receipt["delivered"] is True
        assert final["state"] == "cancelled"
        assert final["signal_sequence"] == ["TERM", "KILL"]
        _wait_for_process_exit(descendant_pid)
    finally:
        (tmp_path / "leader.release").touch(exist_ok=True)
        if descendant_pid is not None and _linux_process_is_live(descendant_pid):
            os.killpg(descendant_pid, signal.SIGKILL)


def test_stop_repeated_kill_drains_nested_adopted_descendant(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            _leader_with_descendant(_nested_adopted_descendant(), escaped=True),
        ],
        timeout=30,
        key="run-stop-nested-adopted-descendant",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    descendants: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            runner = pool.submit(runner_module.run, str(tmp_path), run_id)
            running = _wait_for_running_status(service, run_id)
            deadline = time.monotonic() + 5
            while not (tmp_path / "leader.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            descendants = [
                int((tmp_path / "descendant.pid").read_text()),
                int((tmp_path / "grandchild.pid").read_text()),
            ]
            (tmp_path / "leader.release").touch()
            _wait_for_process_exit(int(running["process_pid"]))
            assert all(_linux_process_is_live(pid) for pid in descendants)
            atomic_write_json(
                tmp_path / ".aros/runs" / run_id / "stop-request.json",
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "kind": "run_stop_request",
                    "actor": "owner",
                    "reason": "stop nested tree",
                    "signal": "TERM",
                    "signal_sequence": ["TERM"],
                    "process_pid": running["process_pid"],
                    "process_start_token": running["process_start_token"],
                    "requested_at": runs_module._utc_now(),
                },
            )
            assert runner.result(timeout=5) == 0
        receipt = json.loads(
            (tmp_path / ".aros/receipts" / f"{run_id}-stop.json").read_text()
        )
        final = service.read_validated_final(run_id)

        assert receipt["signal_sequence"] == ["TERM", "KILL"]
        assert final["state"] == "cancelled"
        assert final["signal_sequence"] == ["TERM", "KILL"]
        for pid in descendants:
            _wait_for_process_exit(pid)
    finally:
        (tmp_path / "leader.release").touch(exist_ok=True)
        for pid in descendants:
            if _linux_process_is_live(pid):
                os.killpg(pid, signal.SIGKILL)


def test_timeout_repeated_kill_drains_nested_adopted_descendant(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            _leader_with_descendant(_nested_adopted_descendant(), escaped=True),
        ],
        timeout=0.5,
        key="run-timeout-nested-adopted-descendant",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    descendants: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            runner = pool.submit(runner_module.run, str(tmp_path), run_id)
            deadline = time.monotonic() + 5
            while not (tmp_path / "leader.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            descendants = [
                int((tmp_path / "descendant.pid").read_text()),
                int((tmp_path / "grandchild.pid").read_text()),
            ]
            assert runner.result(timeout=5) == 0
        final = service.read_validated_final(run_id)

        assert final["state"] == "timed_out"
        assert final["signal_sequence"] == ["TERM", "KILL"]
        for pid in descendants:
            _wait_for_process_exit(pid)
    finally:
        (tmp_path / "leader.release").touch(exist_ok=True)
        for pid in descendants:
            if _linux_process_is_live(pid):
                os.killpg(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "forgery",
    ("dead-session", "host", "session", "runner-pid"),
)
def test_dead_leader_stop_fallback_rejects_unbound_carrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key=f"dead-leader-{forgery}")
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text())
    status.update(
        {
            "state": "running",
            "runner_pid": 12345,
            "process_pid": 12346,
            "process_pgid": 12346,
            "process_start_token": "linux-proc-start:1",
            "started_at": status["launched_at"],
            "heartbeat_at": status["launched_at"],
        }
    )
    if forgery == "host":
        status["host"] = "forged-host"
    elif forgery == "session":
        status["tmux_session"] = "forged-session"
    elif forgery == "runner-pid":
        status["runner_pid"] = 0
    atomic_write_json(runtime / "status.json", status)
    monkeypatch.setattr(processes_module, "identity_is_live", lambda _identity: False)
    monkeypatch.setattr(processes_module, "process_group_is_live", lambda _identity: False)
    monkeypatch.setattr(
        runs_module,
        "_tmux_session_exists",
        lambda _session: forgery != "dead-session",
    )

    with pytest.raises(RunError):
        service.stop(run_id, actor="owner", reason="must fail closed")

    assert not (runtime / "stop-request.json").exists()


def test_timeout_drains_adopted_new_session_descendant_without_early_completion(
    tmp_path: Path,
) -> None:
    if not Path("/proc/self/stat").is_file():
        pytest.skip("Linux /proc is unavailable")
    _init_clean_repo(tmp_path)
    descendant = (
        "import signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "Path('descendant.ready').touch()\n"
        "time.sleep(30)\n"
    )
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            _leader_with_descendant(descendant, escaped=True),
        ],
        timeout=0.3,
        key="run-timeout-adopted-descendant",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    descendant_pid: int | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            runner = pool.submit(runner_module.run, str(tmp_path), run_id)
            running = _wait_for_running_status(service, run_id)
            deadline = time.monotonic() + 5
            while not (tmp_path / "leader.ready").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            descendant_pid = int((tmp_path / "descendant.pid").read_text())
            (tmp_path / "leader.release").touch()
            _wait_for_process_exit(int(running["process_pid"]))
            assert not runner.done()
            assert not (tmp_path / "runs" / run_id / "final.json").exists()
            assert runner.result(timeout=5) == 0
        final = service.read_validated_final(run_id)
        assert final["state"] == "timed_out"
        assert final["signal_sequence"] == ["TERM", "KILL"]
        _wait_for_process_exit(descendant_pid)
    finally:
        (tmp_path / "leader.release").touch(exist_ok=True)
        if descendant_pid is not None and _linux_process_is_live(descendant_pid):
            os.killpg(descendant_pid, signal.SIGKILL)


def test_process_group_observation_failure_is_not_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_scan(_path: str) -> None:
        raise PermissionError("injected /proc observation failure")

    monkeypatch.setattr(processes_module._os, "scandir", fail_scan)

    with pytest.raises(processes_module.ProcessObservationError):
        processes_module.process_group_is_live(
            processes_module.ProcessIdentity(999_999, 999_999, "absent-token")
        )


def test_runner_fails_closed_before_spawn_when_subreaper_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="run-subreaper-unavailable")
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    monkeypatch.setattr(
        processes_module,
        "enable_child_subreaper",
        lambda: (_ for _ in ()).throw(OSError("subreaper unavailable")),
    )
    monkeypatch.setattr(
        processes_module,
        "spawn_process",
        lambda *_args, **_kwargs: pytest.fail("process must not spawn"),
    )

    assert runner_module.run(str(tmp_path), run_id) == 1
    final = service.read_validated_final(run_id)
    assert final["state"] == "failed_process"
    assert "subreaper unavailable" in str(final["error"])


def test_nonzero_exit_is_failed_process_not_scientific_negative(tmp_path: Path) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "raise SystemExit(7)"],
        key="nonzero-exit",
    )
    run_id = str(manifest["run_id"])

    service.start(run_id)
    status = _wait_for_state(service, run_id)
    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )

    assert status["state"] == "failed_process"
    assert final["state"] == "failed_process"
    assert final["exit_code"] == 7
    assert "scientific" not in json.dumps(final).lower()


def test_stop_on_terminal_run_returns_existing_final_without_new_signal(
    tmp_path: Path,
) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "print('already done')"],
        key="terminal-stop",
    )
    run_id = str(manifest["run_id"])
    service.start(run_id)
    assert _wait_for_state(service, run_id)["state"] == "completed"

    final = service.stop(run_id, actor="human", reason="no-op after completion")

    assert final["state"] == "completed"
    assert not (tmp_path / ".aros" / "receipts" / f"{run_id}-stop.json").exists()


def test_stop_locked_does_not_reenter_run_flock(tmp_path: Path, monkeypatch) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="locked-stop")
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status.update(
        {
            "state": "running",
            "process_pid": 12345,
            "process_pgid": 12345,
            "process_start_token": "stable-process",
            "launch_receipt_sha256": "a" * 64,
        }
    )
    atomic_write_json(runtime / "status.json", status)
    monkeypatch.setattr(processes_module, "identity_is_live", lambda _identity: True)
    monkeypatch.setattr(
        processes_module,
        "signal_process_group",
        lambda _identity, _signal: pytest.fail(
            "RunService must not own process signalling"
        ),
    )
    monkeypatch.setattr(
        runs_module,
        "_STOP_ACK_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    def fail_public_reconcile(_run_id: str) -> dict[str, object]:
        raise AssertionError("stop must use the already-locked reconcile helper")

    monkeypatch.setattr(service, "reconcile", fail_public_reconcile)

    with pytest.raises(RunError, match="stop acknowledgement timed out"):
        service.stop(run_id, actor="owner", reason="locked stop")

    assert (runtime / "stop-request.json").is_file()


def test_stop_signal_false_ack_does_not_cancel_successful_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    release_path = tmp_path / ".aros" / "release-payload"
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            (
                "import pathlib,sys,time;"
                "release=pathlib.Path(sys.argv[1]);"
                "exec('while not release.exists(): time.sleep(0.01)')"
            ),
            str(release_path),
        ],
        key="signal-false-race",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    signal_callers: list[int] = []
    service_thread = threading.get_ident()

    def refuse_signal(
        _identity: processes_module.ProcessIdentity,
        _signal_number: int,
    ) -> bool:
        signal_callers.append(threading.get_ident())
        return False

    monkeypatch.setattr(processes_module, "signal_process_group", refuse_signal)
    monkeypatch.setattr(runs_module, "_STOP_ACK_TIMEOUT_SECONDS", 0.2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        runner = pool.submit(runner_module.run, str(tmp_path), run_id)
        try:
            deadline = time.monotonic() + 5
            running: dict[str, object] = {}
            while time.monotonic() < deadline:
                running = service.status(run_id, reconcile=False)
                if running["state"] == "running":
                    break
                time.sleep(0.02)
            assert running["state"] == "running"

            receipt = service.stop(
                run_id,
                actor="race-owner",
                reason="signal refused",
            )
            assert not runner.done()
        finally:
            release_path.touch()
        assert runner.result(timeout=5) == 0

    runtime = tmp_path / ".aros" / "runs" / run_id
    request = json.loads((runtime / "stop-request.json").read_text(encoding="utf-8"))
    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )

    assert signal_callers and len(signal_callers) == 1
    assert signal_callers[0] != service_thread
    assert receipt["delivered"] is False
    assert receipt["signal_sequence"] == []
    assert receipt["actor"] == request["actor"] == "race-owner"
    assert receipt["reason"] == request["reason"] == "signal refused"
    assert receipt["signal"] == request["signal"] == "TERM"
    assert receipt["process_pid"] == running["process_pid"]
    assert receipt["process_pgid"] == running["process_pgid"]
    assert receipt["process_start_token"] == running["process_start_token"]
    assert final["state"] == "completed"
    assert final["exit_code"] == 0
    assert final["stop"] == request
    assert "signal_sequence" not in final


def test_delivered_stop_waits_for_final_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        key="stop-waits-for-final",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    finish_entered = threading.Event()
    release_finish = threading.Event()
    original_finish = runner_module._finish

    def pause_finish(**kwargs: object) -> None:
        finish_entered.set()
        assert release_finish.wait(timeout=5)
        original_finish(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_module, "_finish", pause_finish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        runner = pool.submit(runner_module.run, str(tmp_path), run_id)
        _wait_for_running_status(service, run_id)
        stopping = pool.submit(
            service.stop,
            run_id,
            actor="owner",
            reason="wait for final",
        )
        try:
            assert finish_entered.wait(timeout=5)
            with pytest.raises(TimeoutError):
                stopping.result(timeout=0.2)
            assert (tmp_path / ".aros/receipts" / f"{run_id}-stop.json").is_file()
            assert not (tmp_path / "runs" / run_id / "final.json").exists()
        finally:
            release_finish.set()
        receipt = stopping.result(timeout=5)
        assert runner.result(timeout=5) == 0

    final = service.read_validated_final(run_id)
    request = json.loads(
        (tmp_path / ".aros/runs" / run_id / "stop-request.json").read_text()
    )
    assert receipt["delivered"] is True
    assert final["state"] == "cancelled"
    assert final["stop"] == request


def test_identity_wait_does_not_consume_stop_final_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        key="identity-wait-before-stop-timeout",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    identity_read = threading.Event()
    release_identity = threading.Event()
    original_status = service.status

    def delayed_status(*args: object, **kwargs: object) -> dict[str, object]:
        identity_read.set()
        assert release_identity.wait(timeout=5)
        return original_status(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "status", delayed_status)
    monkeypatch.setattr(runs_module, "_tmux_session_exists", lambda _name: True)
    monkeypatch.setattr(runs_module, "_STOP_ACK_TIMEOUT_SECONDS", 0.5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopping = pool.submit(
            service.stop,
            run_id,
            actor="owner",
            reason="identity delay",
        )
        assert identity_read.wait(timeout=5)
        runner = pool.submit(runner_module.run, str(tmp_path), run_id)
        runtime_status = tmp_path / ".aros/runs" / run_id / "status.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if json.loads(runtime_status.read_text()).get("process_pid") is not None:
                break
            time.sleep(0.02)
        time.sleep(0.9)
        release_identity.set()
        receipt = stopping.result(timeout=5)
        assert runner.result(timeout=5) == 0

    assert receipt["delivered"] is True
    assert service.read_validated_final(run_id)["state"] == "cancelled"


def test_delivered_stop_final_timeout_preserves_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        key="stop-final-timeout-retry",
    )
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    finish_entered = threading.Event()
    release_finish = threading.Event()
    original_finish = runner_module._finish

    def pause_finish(**kwargs: object) -> None:
        finish_entered.set()
        assert release_finish.wait(timeout=5)
        original_finish(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_module, "_finish", pause_finish)
    monkeypatch.setattr(runs_module, "_STOP_ACK_TIMEOUT_SECONDS", 1.0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        runner = pool.submit(runner_module.run, str(tmp_path), run_id)
        _wait_for_running_status(service, run_id)
        stopping = pool.submit(
            service.stop,
            run_id,
            actor="owner",
            reason="retry final wait",
        )
        try:
            assert finish_entered.wait(timeout=5)
            with pytest.raises(RunError, match="stop finalization timed out"):
                stopping.result(timeout=5)
            runtime = tmp_path / ".aros/runs" / run_id
            assert (runtime / "stop-request.json").is_file()
            assert (tmp_path / ".aros/receipts" / f"{run_id}-stop.json").is_file()
            retrying = pool.submit(
                service.stop,
                run_id,
                actor="owner",
                reason="retry final wait",
            )
            with pytest.raises(TimeoutError):
                retrying.result(timeout=0.2)
        finally:
            release_finish.set()
        receipt = retrying.result(timeout=5)
        assert runner.result(timeout=5) == 0

    final = service.read_validated_final(run_id)
    assert receipt["delivered"] is True
    assert final["state"] == "cancelled"


def test_stop_request_remains_when_runner_acknowledgement_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="missing-runner-ack")
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status.update(
        {
            "state": "running",
            "process_pid": 12345,
            "process_pgid": 12345,
            "process_start_token": "stable-process",
            "launch_receipt_sha256": "a" * 64,
        }
    )
    atomic_write_json(runtime / "status.json", status)
    signal_calls = 0

    def refuse_service_signal(
        _identity: processes_module.ProcessIdentity,
        _signal_number: int,
    ) -> bool:
        nonlocal signal_calls
        signal_calls += 1
        return False

    monkeypatch.setattr(processes_module, "identity_is_live", lambda _identity: True)
    monkeypatch.setattr(processes_module, "signal_process_group", refuse_service_signal)
    monkeypatch.setattr(
        runs_module,
        "_STOP_ACK_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    with pytest.raises(RunError, match="stop acknowledgement timed out"):
        service.stop(run_id, actor="owner", reason="runner absent")

    request = json.loads((runtime / "stop-request.json").read_text(encoding="utf-8"))
    assert request["actor"] == "owner"
    assert request["reason"] == "runner absent"
    assert signal_calls == 0
    assert not (tmp_path / ".aros" / "receipts" / f"{run_id}-stop.json").exists()


def test_stop_persists_actor_reason_signal_request_and_receipt(tmp_path: Path) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    run_id = str(manifest["run_id"])
    service.start(run_id)

    service.stop(
        run_id,
        actor="human-owner",
        reason="safety review",
        signal_name="TERM",
    )
    status = _wait_for_state(service, run_id)
    request = json.loads(
        (tmp_path / ".aros" / "runs" / run_id / "stop-request.json").read_text(
            encoding="utf-8"
        )
    )
    receipt = json.loads(
        (tmp_path / ".aros" / "receipts" / f"{run_id}-stop.json").read_text(
            encoding="utf-8"
        )
    )

    assert status["state"] == "cancelled"
    assert request["actor"] == receipt["actor"] == "human-owner"
    assert request["reason"] == receipt["reason"] == "safety review"
    assert request["signal"] == receipt["signal"] == "TERM"
    assert receipt["delivered"] is True
    assert receipt["process_start_token"]
    assert receipt["signal_sequence"] == ["TERM"]
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".aros" / "events").glob("*.json")
    ]
    assert any(event["kind"] == "run_stop_requested" for event in events)


def test_stop_fails_closed_when_process_identity_token_changed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        key="identity-stop",
    )
    run_id = str(manifest["run_id"])
    service.start(run_id)
    original = processes_module.identity_is_live
    monkeypatch.setattr(
        processes_module,
        "identity_is_live",
        lambda _identity: False,
    )
    with pytest.raises(RunError, match="process identity"):
        service.stop(run_id, actor="owner", reason="must not hit reused PID")
    assert not (tmp_path / ".aros" / "runs" / run_id / "stop-request.json").exists()

    monkeypatch.setattr(processes_module, "identity_is_live", original)
    service.stop(run_id, actor="owner", reason="test cleanup")
    assert _wait_for_state(service, run_id)["state"] == "cancelled"


def test_stop_escalates_to_kill_when_child_ignores_term(tmp_path: Path) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(30)"
            ),
        ],
        key="ignore-term",
    )
    run_id = str(manifest["run_id"])
    service.start(run_id)
    deadline = time.monotonic() + 5
    while "ready" not in service.tail(run_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "ready" in service.tail(run_id)

    service.stop(run_id, actor="owner", reason="bounded shutdown", signal_name="TERM")
    assert _wait_for_state(service, run_id, timeout=5)["state"] == "cancelled"
    final = json.loads(
        (tmp_path / "runs" / run_id / "final.json").read_text(encoding="utf-8")
    )
    assert final["signal_sequence"] == ["TERM", "KILL"]


def test_concurrent_identical_stop_requests_share_one_receipt(tmp_path: Path) -> None:
    _require_tmux()
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(
        tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        key="concurrent-stop",
    )
    run_id = str(manifest["run_id"])
    service.start(run_id)

    def stop_once() -> dict[str, object]:
        return service.stop(run_id, actor="owner", reason="same request")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _index: stop_once(), range(2)))

    assert first == second
    assert len(list((tmp_path / ".aros" / "receipts").glob(f"{run_id}-stop.json"))) == 1
    assert _wait_for_state(service, run_id)["state"] == "cancelled"


def test_stop_retries_after_request_was_persisted_before_ack(
    tmp_path: Path, monkeypatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path, key="partial-stop")
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status.update(
        {
            "state": "running",
            "process_pid": 12345,
            "process_pgid": 12345,
            "process_start_token": "stable-process",
            "launch_receipt_sha256": "a" * 64,
        }
    )
    atomic_write_json(runtime / "status.json", status)
    monkeypatch.setattr(processes_module, "identity_is_live", lambda _identity: True)

    def fail_after_request(**_kwargs: object) -> None:
        raise RuntimeError("simulated crash after durable stop request")

    monkeypatch.setattr(service, "_write_event", fail_after_request)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.stop(run_id, actor="owner", reason="same request")
    request = json.loads((runtime / "stop-request.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(service, "_write_event", lambda **_kwargs: None)
    monkeypatch.setattr(
        runs_module,
        "_STOP_ACK_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    signal_calls = 0

    def refuse_service_signal(
        _identity: processes_module.ProcessIdentity,
        _signal_number: int,
    ) -> bool:
        nonlocal signal_calls
        signal_calls += 1
        return False

    monkeypatch.setattr(
        processes_module,
        "signal_process_group",
        refuse_service_signal,
    )
    with pytest.raises(RunError, match="stop acknowledgement timed out"):
        service.stop(run_id, actor="owner", reason="same request")
    retried = json.loads((runtime / "stop-request.json").read_text(encoding="utf-8"))

    assert retried["requested_at"] == request["requested_at"]
    assert signal_calls == 0
    assert not (tmp_path / ".aros" / "receipts" / f"{run_id}-stop.json").exists()


def test_reconcile_absent_process_without_final_is_lost_not_failed(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    atomic_write_json(
        runtime / "status.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "state": "running",
            "manifest_sha256": manifest["manifest_sha256"],
            "process_pid": 999_999_999,
            "updated_at": manifest["created_at"],
        },
    )

    status = service.reconcile(run_id)

    assert status["state"] == "lost"
    assert status["reason"] == "process_absent_without_final_receipt"
    assert not (tmp_path / "runs" / run_id / "final.json").exists()
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".aros" / "events").glob("*.json")
    ]
    assert any(event["kind"] == "anomaly" for event in events)


def test_reconcile_prefers_existing_final_receipt_over_stale_status(
    tmp_path: Path,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    atomic_write_json(
        runtime / "status.json",
        {
            **status,
            "state": "running",
            "process_pid": 999_999_999,
        },
    )

    status = service.reconcile(run_id)

    assert status["state"] == "completed"
    assert status["exit_code"] == 0
    completion_event = tmp_path / ".aros" / "events" / f"EVT-{run_id}-completed.json"
    assert completion_event.is_file()
    assert (
        json.loads(completion_event.read_text(encoding="utf-8"))["kind"]
        == "run_completed"
    )


def test_list_fails_closed_on_unreadable_run_status(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = str(manifest["run_id"])
    (tmp_path / ".aros" / "runs" / run_id / "status.json").write_text(
        "{broken", encoding="utf-8"
    )

    with pytest.raises(RunError, match="run status"):
        service.list()


def test_status_retries_atomic_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = str(manifest["run_id"])
    status_path = tmp_path / ".aros" / "runs" / run_id / "status.json"
    replacement_path = status_path.with_name("replacement.json")
    replacement = json.loads(status_path.read_text(encoding="utf-8"))
    replacement["updated_at"] = "2026-08-03T12:34:56.789Z"
    atomic_write_json(replacement_path, replacement)
    real_lstat = Path.lstat
    status_lstats = 0

    def replace_before_final_lstat(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal status_lstats
        if path == status_path:
            status_lstats += 1
            if status_lstats == 2:
                replacement_path.replace(status_path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", replace_before_final_lstat)

    assert service.status(run_id, reconcile=False) == replacement
    assert status_lstats >= 4


def test_reconcile_restores_missing_completion_event(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    runtime = tmp_path / ".aros" / "runs" / run_id
    event = tmp_path / ".aros" / "events" / f"EVT-{run_id}-completed.json"
    event.unlink()
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status["state"] = "running"
    atomic_write_json(runtime / "status.json", status)

    assert service.reconcile(run_id)["state"] == "completed"
    assert event.is_file()
    assert service.reconcile(run_id)["state"] == "completed"
    assert len(list((tmp_path / ".aros" / "events").glob("*.json"))) == 1


def test_reconcile_prefers_final_while_mutable_status_process_is_alive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = _mark_runner_launched(tmp_path, service, manifest)
    assert runner_module.run(str(tmp_path), run_id) == 0
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status.update(
        {
            "state": "running",
            "process_pid": 12345,
            "process_start_token": "same-process",
        }
    )
    atomic_write_json(runtime / "status.json", status)
    monkeypatch.setattr(
        processes_module,
        "identity_is_live",
        lambda _identity: pytest.fail(
            "terminal reconciliation read mutable process state"
        ),
    )

    reconciled = service.reconcile(run_id)

    assert reconciled["state"] == "completed"
    assert "process_pid" not in reconciled
