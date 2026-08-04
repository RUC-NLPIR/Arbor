"""Durable AROS run lifecycle tests.

These tests exercise the filesystem/process contract directly.  tmux tests are
real integration tests when tmux is available; deterministic state-machine
tests do not require it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import arbor.aros.runner as runner_module
import arbor.aros.runs as runs_module
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
    launched_at = str(manifest["created_at"])
    session_name = f"aros-{run_id.lower()}"
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
        "tmux_session": session_name,
        "host": "test-host",
        "runner_version": 1,
        "runner_invocation": [sys.executable, "-m", "arbor.aros.runner"],
    }
    prelaunch["receipt_sha256"] = record_sha256(prelaunch, "receipt_sha256")
    atomic_write_json(
        root / ".aros" / "receipts" / f"{run_id}-prelaunch.json",
        prelaunch,
    )
    launched = service.status(run_id, reconcile=False)
    launched.update(
        {
            "state": "launched",
            "actor": manifest["actor"],
            "carrier": "tmux",
            "tmux_session": session_name,
            "host": "test-host",
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

    real_popen = runner_module.subprocess.Popen

    def observe_spawn(*args: object, **kwargs: object):
        if args and args[0] == manifest["argv"]:
            events.append("spawn")
            assert events == ["validated-both-trees", "spawn"]
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(
        runner_module,
        "validate_execution_bundle",
        observe_validation,
        raising=False,
    )
    monkeypatch.setattr(runner_module.subprocess, "Popen", observe_spawn)

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
    real_popen = runner_module.subprocess.Popen

    def observe_spawn(*args: object, **kwargs: object):
        nonlocal spawned
        if args and args[0] == manifest["argv"]:
            spawned = True
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", observe_spawn)

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
    real_popen = runner_module.subprocess.Popen

    def observe_spawn(*args: object, **kwargs: object):
        nonlocal spawned
        if args and args[0] == manifest["argv"]:
            spawned = True
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", observe_spawn)

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
    real_popen = runner_module.subprocess.Popen

    def observe_spawn(*args: object, **kwargs: object):
        nonlocal spawned
        if args and args[0] == manifest["argv"]:
            spawned = True
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", observe_spawn)

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
    assert launch_receipt["runner_invocation"][1:3] == ["-m", "arbor.aros.runner"]
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
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status.update(
        {
            "state": "launched",
            "launch_receipt_sha256": "a" * 64,
        }
    )
    atomic_write_json(runtime / "status.json", status)
    atomic_write_json(
        tmp_path / "runs" / run_id / "final.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "launch_receipt_sha256": "a" * 64,
            "exit_code": 0,
            "finished_at": manifest["created_at"],
        },
    )

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
    monkeypatch.setattr(
        "arbor.aros.runs._process_start_token", lambda _pid: "stable-process"
    )
    monkeypatch.setattr("arbor.aros.runs.os.killpg", lambda _pgid, _signal: None)

    def fail_public_reconcile(_run_id: str) -> dict[str, object]:
        raise AssertionError("stop must use the already-locked reconcile helper")

    monkeypatch.setattr(service, "reconcile", fail_public_reconcile)

    receipt = service.stop(run_id, actor="owner", reason="locked stop")

    assert receipt["delivered"] is True


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
    from arbor.aros import runs

    original = runs._process_start_token
    monkeypatch.setattr(runs, "_process_start_token", lambda _pid: "reused-pid")
    with pytest.raises(RunError, match="process identity"):
        service.stop(run_id, actor="owner", reason="must not hit reused PID")
    assert not (tmp_path / ".aros" / "runs" / run_id / "stop-request.json").exists()

    monkeypatch.setattr(runs, "_process_start_token", original)
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


def test_stop_retries_after_request_was_persisted_before_signal(
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
    monkeypatch.setattr(
        "arbor.aros.runs._process_start_token", lambda _pid: "stable-process"
    )

    def fail_after_request(**_kwargs: object) -> None:
        raise RuntimeError("simulated crash after durable stop request")

    monkeypatch.setattr(service, "_write_event", fail_after_request)
    with pytest.raises(RuntimeError, match="simulated crash"):
        service.stop(run_id, actor="owner", reason="same request")
    request = json.loads((runtime / "stop-request.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(service, "_write_event", lambda **_kwargs: None)
    monkeypatch.setattr("arbor.aros.runs.os.killpg", lambda _pgid, _signal: None)
    receipt = service.stop(run_id, actor="owner", reason="same request")

    assert receipt["delivered"] is True
    assert receipt["requested_at"] == request["requested_at"]


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


def test_reconcile_prefers_existing_final_receipt_over_stale_status(tmp_path: Path) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    final = {
        "schema_version": 1,
        "run_id": run_id,
        "state": "completed",
        "manifest_sha256": manifest["manifest_sha256"],
        "launch_receipt_sha256": "a" * 64,
        "exit_code": 0,
        "finished_at": manifest["created_at"],
    }
    atomic_write_json(tmp_path / "runs" / run_id / "final.json", final)
    atomic_write_json(
        runtime / "status.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "state": "running",
            "manifest_sha256": manifest["manifest_sha256"],
            "launch_receipt_sha256": "a" * 64,
            "process_pid": 999_999_999,
            "updated_at": manifest["created_at"],
        },
    )

    status = service.reconcile(run_id)

    assert status["state"] == "completed"
    assert status["exit_code"] == 0
    completion_event = (
        tmp_path / ".aros" / "events" / f"EVT-{run_id}-completed.json"
    )
    assert completion_event.is_file()
    assert json.loads(completion_event.read_text(encoding="utf-8"))["kind"] == "run_completed"


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
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status["state"] = "running"
    status["launch_receipt_sha256"] = "a" * 64
    atomic_write_json(runtime / "status.json", status)
    atomic_write_json(
        tmp_path / "runs" / run_id / "final.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "launch_receipt_sha256": "a" * 64,
            "exit_code": 0,
            "finished_at": manifest["created_at"],
        },
    )

    assert service.reconcile(run_id)["state"] == "completed"
    event = tmp_path / ".aros" / "events" / f"EVT-{run_id}-completed.json"
    assert event.is_file()
    assert service.reconcile(run_id)["state"] == "completed"
    assert len(list((tmp_path / ".aros" / "events").glob("*.json"))) == 1


def test_reconcile_rejects_final_while_matching_process_is_alive(
    tmp_path: Path, monkeypatch,
) -> None:
    _init_clean_repo(tmp_path)
    service, manifest = _prepare(tmp_path)
    run_id = str(manifest["run_id"])
    runtime = tmp_path / ".aros" / "runs" / run_id
    status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    status.update(
        {
            "state": "running",
            "process_pid": 12345,
            "process_start_token": "same-process",
            "launch_receipt_sha256": "a" * 64,
        }
    )
    atomic_write_json(runtime / "status.json", status)
    atomic_write_json(
        tmp_path / "runs" / run_id / "final.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "launch_receipt_sha256": "a" * 64,
            "exit_code": 0,
            "finished_at": manifest["created_at"],
        },
    )
    monkeypatch.setattr(
        "arbor.aros.runs._process_start_token", lambda _pid: "same-process"
    )

    with pytest.raises(RunError, match="integrity conflict"):
        service.reconcile(run_id)
