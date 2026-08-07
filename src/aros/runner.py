"""Detached process wrapper invoked as ``python -m arbor.aros.runner``."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import processes
from .isolation import IsolationError, IsolationLimits, build_isolated_linux
from .receipts import content_receipt, digest_chunks
from .store import (
    atomic_write_json,
    create_json,
    environment_sha256 as _environment_sha256,
    final_identity as _final_identity,
    json_sha256 as _json_sha256,
    manifest_sha256 as _manifest_sha256,
    read_json,
    utc_now as _utc_now,
)
from .worktrees import (
    CheckoutBinding,
    ExecutionBundle,
    RepositoryBinding,
    WorktreeError,
    bind_repository,
    validate_execution_bundle,
)


def _file_receipt(path: Path, relative: str) -> dict[str, object]:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
        size, sha256 = digest_chunks(
            iter(lambda: handle.read(1024 * 1024), b"")
        )
    return content_receipt(relative, size, sha256)


def _read_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_completion_event(
    root: Path,
    run_id: str,
    state: str,
    finished_at: str,
) -> None:
    event_id = f"EVT-{run_id}-completed"
    create_json(
        root / ".aros" / "events" / f"{event_id}.json",
        {
            "schema_version": 1,
            "event_id": event_id,
            "kind": "run_completed",
            "created_at": finished_at,
            "source_ref": f"runs/{run_id}",
            "summary": f"Run reached process state {state}.",
            "artifact_refs": [f"runs/{run_id}/final.json"],
            "acknowledged_by": None,
            "acknowledged_at": None,
        },
    )


def _finish(
    *,
    root: Path,
    runtime: Path,
    manifest: dict[str, Any],
    prior_status: dict[str, Any],
    state: str,
    exit_code: int | None,
    started_at: str,
    started_monotonic: float,
    stdout_path: Path,
    stderr_path: Path,
    stop_request: dict[str, Any] | None = None,
    signal_sequence: list[str] | None = None,
    error: str | None = None,
    actual_environment_sha256: str | None = None,
) -> None:
    run_id = str(manifest["run_id"])
    finished_at = _utc_now()
    final = _final_identity(manifest)
    duration_seconds = round(time.monotonic() - started_monotonic, 6)
    launch_receipt_sha256 = prior_status.get("launch_receipt_sha256")
    if not isinstance(launch_receipt_sha256, str):
        raise ValueError("status is missing launch receipt lineage")
    final.update(
        {
            "schema_version": 1,
            "state": state,
            "exit_code": exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "finalized_at": finished_at,
            "duration_seconds": duration_seconds,
            "resource_usage": {"wall_seconds": duration_seconds},
            "host": socket.gethostname(),
            "actual_environment_sha256": (
                actual_environment_sha256 or _environment_sha256()
            ),
            "launch_receipt_sha256": launch_receipt_sha256,
            "stdout": _file_receipt(
                stdout_path, f".aros/runs/{run_id}/stdout.log"
            ),
            "stderr": _file_receipt(
                stderr_path, f".aros/runs/{run_id}/stderr.log"
            ),
        }
    )
    if stop_request is not None:
        final["stop"] = stop_request
    if signal_sequence:
        final["signal_sequence"] = signal_sequence
    if error is not None:
        final["error"] = error
    final_path = root / "runs" / run_id / "final.json"
    if not create_json(final_path, final):
        final = _read_object(final_path)
        state = str(final["state"])
        exit_code = final.get("exit_code")
        finished_at = str(final["finished_at"])
    atomic_write_json(
        runtime / "status.json",
        {
            **prior_status,
            "state": state,
            "exit_code": exit_code,
            "finished_at": finished_at,
            "heartbeat_at": finished_at,
            "final_ref": f"runs/{run_id}/final.json",
            "updated_at": finished_at,
        },
    )
    _write_completion_event(root, run_id, state, finished_at)


def _read_stop_request(runtime: Path) -> dict[str, Any] | None:
    path = runtime / "stop-request.json"
    return _read_object(path) if path.is_file() else None


def _write_stop_receipt(
    root: Path,
    run_id: str,
    stop_request: dict[str, Any],
    identity: processes.ProcessIdentity,
    signal_sequence: list[str],
    delivered: bool,
) -> None:
    path = root / ".aros" / "receipts" / f"{run_id}-stop.json"
    receipt = {
        "schema_version": 1,
        "receipt_id": f"{run_id}-stop",
        "kind": "run_stop",
        "run_id": run_id,
        "actor": stop_request["actor"],
        "reason": stop_request["reason"],
        "signal": stop_request["signal"],
        "signal_sequence": signal_sequence,
        "requested_at": stop_request["requested_at"],
        "recorded_at": _utc_now(),
        "delivered": delivered,
        "process_pid": identity.pid,
        "process_pgid": identity.pgid,
        "process_start_token": identity.start_token,
    }
    if not create_json(path, receipt):
        _read_object(path)


def _execution_bundle_binding(
    root: Path,
    manifest: dict[str, Any],
) -> tuple[Path, RepositoryBinding, ExecutionBundle]:
    payload = manifest.get("execution_bundle")
    if not isinstance(payload, dict) or set(payload) != {
        "candidate",
        "apparatus",
        "temp",
        "bundle_sha256",
    }:
        raise ValueError("manifest execution_bundle has an invalid shape")
    repository_ref = manifest.get("repository_ref")
    if not isinstance(repository_ref, str):
        raise ValueError("bundle repository_ref must be workspace-relative")
    relative_root = Path(repository_ref)
    if (
        relative_root.is_absolute()
        or len(relative_root.parts) != 3
        or relative_root.parts[:2] != (".worktree", "eval")
    ):
        raise ValueError("bundle repository_ref must be .worktree/eval/<eval-id>")
    bundle_root = root / relative_root
    repository = bind_repository(root)

    def checkout(name: str) -> CheckoutBinding:
        record = payload.get(name)
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "commit", "tree"}
            or record.get("path") != name
            or not isinstance(record.get("commit"), str)
            or not isinstance(record.get("tree"), str)
        ):
            raise ValueError(f"manifest execution_bundle {name} is invalid")
        checkout_repository = bind_repository(bundle_root / name)
        return CheckoutBinding(
            path=bundle_root / name,
            git_dir=checkout_repository.git_dir,
            commit=str(record["commit"]),
            tree=str(record["tree"]),
        )

    if payload.get("temp") != "tmp" or not isinstance(
        payload.get("bundle_sha256"), str
    ):
        raise ValueError("manifest execution_bundle temp or hash is invalid")
    bundle = ExecutionBundle(
        root=bundle_root,
        candidate=checkout("candidate"),
        apparatus=checkout("apparatus"),
        temp=bundle_root / "tmp",
        bundle_sha256=str(payload["bundle_sha256"]),
    )
    return bundle_root, repository, bundle


def run(workspace: str, run_id: str) -> int:
    root = Path(workspace).expanduser().resolve()
    manifest_path = root / "runs" / run_id / "manifest.json"
    runtime = root / ".aros" / "runs" / run_id
    manifest = _read_object(manifest_path)
    if manifest.get("run_id") != run_id:
        raise ValueError("manifest run identity mismatch")
    if manifest.get("manifest_sha256") != _manifest_sha256(manifest):
        raise ValueError("manifest hash mismatch")
    status = _read_object(runtime / "status.json")
    if status.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise ValueError("status manifest hash mismatch")
    final_path = root / "runs" / run_id / "final.json"
    if final_path.exists():
        return 0

    stdout_path = runtime / "stdout.log"
    stderr_path = runtime / "stderr.log"
    runtime.mkdir(parents=True, exist_ok=True)
    stdout_path.touch(exist_ok=True)
    stderr_path.touch(exist_ok=True)
    started_at = _utc_now()
    started_monotonic = time.monotonic()

    child_environment: dict[str, str] | None = None
    child_preexec_fn = None
    actual_environment_sha256 = _environment_sha256()
    try:
        execution_root = root
        bundle_binding: tuple[RepositoryBinding, ExecutionBundle] | None = None
        if "execution_bundle" in manifest:
            execution_root, repository, bundle = _execution_bundle_binding(
                root, manifest
            )
            bundle_binding = repository, bundle
        try:
            cwd = (execution_root / str(manifest["cwd"])).resolve()
            cwd.relative_to(execution_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("manifest cwd is unavailable") from error
        if not cwd.is_dir():
            raise ValueError("manifest cwd is unavailable")
        profile = manifest.get("security_profile")
        if profile == "isolated-linux":
            raw_limits = manifest.get("isolation_limits")
            if not isinstance(raw_limits, dict):
                raise ValueError("isolated manifest is missing isolation_limits")
            limits = IsolationLimits(**raw_limits)
            launch = build_isolated_linux(
                execution_root,
                manifest.get("writable_paths", []),
                limits=limits,
            )
            frozen_policy = {
                "writable_paths": list(launch.writable_paths),
                "network_policy": launch.network_policy,
                "process_policy": launch.process_policy,
                "environment_policy": launch.environment_policy,
                "isolation_limits": asdict(launch.limits),
            }
            if any(manifest.get(key) != value for key, value in frozen_policy.items()):
                raise ValueError("isolated launch policy differs from frozen manifest")
            child_environment = launch.env
            child_preexec_fn = launch.preexec_fn
            actual_environment_sha256 = _json_sha256(launch.env)
        elif profile != "trusted-local":
            raise ValueError("manifest has an unsupported security profile")
        with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open(
            "ab", buffering=0
        ) as stderr:
            if bundle_binding is not None:
                validate_execution_bundle(*bundle_binding)
            processes.enable_child_subreaper()
            handle = processes.spawn_process(
                list(manifest["argv"]),
                cwd=cwd,
                stdin=None,
                stdout=stdout,
                stderr=stderr,
                env=child_environment,
                pass_fds=(),
                preexec_fn=child_preexec_fn,
            )
    except (
        IsolationError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        WorktreeError,
        ValueError,
    ) as error:
        _finish(
            root=root,
            runtime=runtime,
            manifest=manifest,
            prior_status=status,
            state="failed_process",
            exit_code=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            error=f"process launch failed: {error}",
        )
        return 1

    running_status = {
        **status,
        "state": "running",
        "runner_pid": os.getpid(),
        "process_pid": handle.identity.pid,
        "process_pgid": handle.identity.pgid,
        "process_start_token": handle.identity.start_token,
        "started_at": started_at,
        "heartbeat_at": started_at,
        "updated_at": started_at,
    }
    atomic_write_json(runtime / "status.json", running_status)
    timeout_seconds = float(manifest["timeout_seconds"])
    timeout_hit = False
    timeout_signal_sequence: list[str] = []
    exit_code: int | None = None
    delivered_stop = False
    stop_attempted = False
    stop_escalated = False
    stop_signal_sequence: list[str] = []
    stop_started_monotonic: float | None = None
    timeout_escalated = False
    timeout_started_monotonic: float | None = None
    drain_deadline: float | None = None
    last_heartbeat = time.monotonic()
    runner_pid = os.getpid()

    while True:
        polled = handle.process.poll()
        if polled is not None and exit_code is None:
            exit_code = polled
        tree_is_live = processes.process_tree_is_live(handle, runner_pid)
        if exit_code is not None and not tree_is_live:
            break
        stop_request = _read_stop_request(runtime)
        if stop_request is not None and not stop_attempted:
            requested_name = str(stop_request.get("signal", "TERM"))
            requested_signal = {
                "TERM": signal.SIGTERM,
                "KILL": signal.SIGKILL,
                "INT": signal.SIGINT,
            }.get(requested_name, signal.SIGTERM)
            delivered_stop = processes.signal_process_tree(
                handle,
                runner_pid,
                requested_signal,
            )
            if delivered_stop:
                stop_signal_sequence.append(requested_name)
            stop_started_monotonic = time.monotonic()
            stop_attempted = True
            stop_escalated = requested_name == "KILL"
            if stop_escalated and delivered_stop:
                drain_deadline = time.monotonic() + 2
            if not delivered_stop:
                _write_stop_receipt(
                    root,
                    run_id,
                    stop_request,
                    handle.identity,
                    stop_signal_sequence,
                    False,
                )
        elif (
            delivered_stop
            and not stop_escalated
            and stop_started_monotonic is not None
            and time.monotonic() - stop_started_monotonic >= 1
        ):
            if processes.signal_process_tree(handle, runner_pid, signal.SIGKILL):
                stop_signal_sequence.append("KILL")
            stop_escalated = True
            drain_deadline = time.monotonic() + 2
        if (
            not delivered_stop
            and not timeout_hit
            and time.monotonic() - started_monotonic >= timeout_seconds
        ):
            if processes.signal_process_tree(handle, runner_pid, signal.SIGTERM):
                timeout_hit = True
                timeout_signal_sequence.append("TERM")
                timeout_started_monotonic = time.monotonic()
        elif (
            timeout_hit
            and not timeout_escalated
            and timeout_started_monotonic is not None
            and time.monotonic() - timeout_started_monotonic >= 1
        ):
            if processes.signal_process_tree(handle, runner_pid, signal.SIGKILL):
                timeout_signal_sequence.append("KILL")
            timeout_escalated = True
            drain_deadline = time.monotonic() + 2
        if drain_deadline is not None and tree_is_live and time.monotonic() >= drain_deadline:
            raise processes.ProcessObservationError("Run descendants did not drain")
        now = time.monotonic()
        if now - last_heartbeat >= 0.2:
            heartbeat_at = _utc_now()
            running_status["heartbeat_at"] = heartbeat_at
            running_status["updated_at"] = heartbeat_at
            atomic_write_json(runtime / "status.json", running_status)
            last_heartbeat = now
        if exit_code is None or tree_is_live:
            time.sleep(0.02)

    if exit_code is None:
        exit_code = processes.reap_leader(handle)
    stop_request = _read_stop_request(runtime)
    if stop_request is not None:
        _write_stop_receipt(
            root,
            run_id,
            stop_request,
            handle.identity,
            stop_signal_sequence,
            delivered_stop,
        )
    if stop_request is not None and delivered_stop:
        state = "cancelled"
    elif timeout_hit:
        state = "timed_out"
    elif exit_code in manifest.get("success_exit_codes", [0]):
        state = "completed"
    else:
        state = "failed_process"
    if stop_request is not None and delivered_stop:
        final_signal_sequence = stop_signal_sequence
    else:
        final_signal_sequence = timeout_signal_sequence
    _finish(
        root=root,
        runtime=runtime,
        manifest=manifest,
        prior_status=running_status,
        state=state,
        exit_code=exit_code,
        started_at=started_at,
        started_monotonic=started_monotonic,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stop_request=stop_request,
        signal_sequence=final_signal_sequence,
        actual_environment_sha256=actual_environment_sha256,
    )
    return 0 if state in {"completed", "cancelled", "timed_out"} else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    return run(args.workspace, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
