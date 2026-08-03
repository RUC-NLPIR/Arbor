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

from .isolation import IsolationError, IsolationLimits, build_isolated_linux
from .receipts import content_receipt, digest_chunks
from .store import (
    atomic_write_json,
    create_json,
    environment_sha256 as _environment_sha256,
    final_identity as _final_identity,
    json_sha256 as _json_sha256,
    manifest_sha256 as _manifest_sha256,
    process_start_token as _process_start_token,
    read_json,
    utc_now as _utc_now,
)
from .worktrees import (
    CheckoutBinding,
    ExecutionBundle,
    RepositoryBinding,
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


def _read_stop_receipt(root: Path, run_id: str) -> dict[str, Any] | None:
    path = root / ".aros" / "receipts" / f"{run_id}-stop.json"
    return _read_object(path) if path.is_file() else None


def _signal_process_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        pass


def _terminate_for_timeout(process: subprocess.Popen[bytes]) -> list[str]:
    sequence = ["TERM"]
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        sequence.append("KILL")
        _signal_process_group(process, signal.SIGKILL)
        process.wait()
    return sequence


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

    execution_root = root
    bundle_binding: tuple[RepositoryBinding, ExecutionBundle] | None = None
    if "execution_bundle" in manifest:
        execution_root, repository, bundle = _execution_bundle_binding(root, manifest)
        bundle_binding = repository, bundle
    cwd = (execution_root / str(manifest["cwd"])).resolve()
    cwd.relative_to(execution_root)
    if not cwd.is_dir():
        raise ValueError("manifest cwd is unavailable")
    stdout_path = runtime / "stdout.log"
    stderr_path = runtime / "stderr.log"
    runtime.mkdir(parents=True, exist_ok=True)
    stdout_path.touch(exist_ok=True)
    stderr_path.touch(exist_ok=True)
    started_at = _utc_now()
    started_monotonic = time.monotonic()

    stop_request = _read_stop_request(runtime)
    if stop_request is not None:
        _finish(
            root=root,
            runtime=runtime,
            manifest=manifest,
            prior_status=status,
            state="cancelled",
            exit_code=None,
            started_at=started_at,
            started_monotonic=started_monotonic,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stop_request=stop_request,
        )
        return 0

    popen_options: dict[str, Any] = {}
    actual_environment_sha256 = _environment_sha256()
    try:
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
            popen_options = {
                "env": launch.env,
                "preexec_fn": launch.preexec_fn,
            }
            actual_environment_sha256 = _json_sha256(launch.env)
        elif profile != "trusted-local":
            raise ValueError("manifest has an unsupported security profile")
        with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open(
            "ab", buffering=0
        ) as stderr:
            if bundle_binding is not None:
                validate_execution_bundle(*bundle_binding)
            process = subprocess.Popen(
                list(manifest["argv"]),
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                **popen_options,
            )
    except (
        IsolationError,
        OSError,
        subprocess.SubprocessError,
        TypeError,
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
        "process_pid": process.pid,
        "process_pgid": process.pid,
        "process_start_token": _process_start_token(process.pid),
        "started_at": started_at,
        "heartbeat_at": started_at,
        "updated_at": started_at,
    }
    atomic_write_json(runtime / "status.json", running_status)
    timeout_seconds = float(manifest["timeout_seconds"])
    timeout_hit = False
    timeout_signal_sequence: list[str] = []
    delivered_stop = False
    stop_signal_sequence: list[str] = []
    stop_started_monotonic: float | None = None
    last_heartbeat = time.monotonic()

    while process.poll() is None:
        stop_request = _read_stop_request(runtime)
        if stop_request is not None and not delivered_stop:
            requested_name = str(stop_request.get("signal", "TERM"))
            requested_signal = {
                "TERM": signal.SIGTERM,
                "KILL": signal.SIGKILL,
                "INT": signal.SIGINT,
            }.get(requested_name, signal.SIGTERM)
            _signal_process_group(process, requested_signal)
            stop_signal_sequence.append(requested_name)
            stop_started_monotonic = time.monotonic()
            delivered_stop = True
        elif (
            stop_request is not None
            and stop_started_monotonic is not None
            and stop_signal_sequence[-1] != "KILL"
            and time.monotonic() - stop_started_monotonic >= 1
        ):
            _signal_process_group(process, signal.SIGKILL)
            stop_signal_sequence.append("KILL")
        if stop_request is None and time.monotonic() - started_monotonic >= timeout_seconds:
            timeout_hit = True
            timeout_signal_sequence = _terminate_for_timeout(process)
        now = time.monotonic()
        if now - last_heartbeat >= 0.2:
            heartbeat_at = _utc_now()
            running_status["heartbeat_at"] = heartbeat_at
            running_status["updated_at"] = heartbeat_at
            atomic_write_json(runtime / "status.json", running_status)
            last_heartbeat = now
        if process.poll() is None:
            time.sleep(0.02)

    exit_code = process.wait()
    stop_request = _read_stop_request(runtime)
    stop_receipt = _read_stop_receipt(root, run_id)
    if stop_request is not None and not delivered_stop and stop_receipt is None:
        deadline = time.monotonic() + 0.5
        while stop_receipt is None and time.monotonic() < deadline:
            time.sleep(0.01)
            stop_receipt = _read_stop_receipt(root, run_id)
    externally_delivered = bool(stop_receipt and stop_receipt.get("delivered"))
    if stop_request is not None and (delivered_stop or externally_delivered):
        state = "cancelled"
    elif timeout_hit:
        state = "timed_out"
    elif exit_code in manifest.get("success_exit_codes", [0]):
        state = "completed"
    else:
        state = "failed_process"
    if stop_request is not None and (delivered_stop or externally_delivered):
        final_signal_sequence = stop_signal_sequence or (
            list(stop_receipt.get("signal_sequence", []))
            if stop_receipt is not None
            else []
        )
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
