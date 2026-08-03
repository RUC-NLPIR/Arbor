"""Detached exact-argv runner for one durable AROS child task."""

from __future__ import annotations

import argparse
import math
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .store import (
    atomic_write_json,
    create_json,
    file_lock,
    process_start_token,
    utc_now,
)
from .tasks import (
    TaskError,
    TaskService,
    _TASK_RUNNER_BOOTSTRAP,
    _file_receipt,
    _path_exists,
    _read_object,
    _record_sha256,
    _tmux_socket_name,
    _validate_hash,
    _validate_text,
    _validate_timestamp,
)


_ADAPTER_ENVIRONMENT_KEYS = (
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
_TERMINAL_STATES = {"completed", "failed_process", "timed_out", "cancelled"}
_EXECUTION_STATUS_FIELDS = {
    "schema_version",
    "task_id",
    "state",
    "brief_sha256",
    "ownership_sha256",
    "launch_sha256",
    "actor",
    "carrier",
    "tmux_session",
    "host",
    "launched_at",
    "runner_pid",
    "runner_pgid",
    "runner_start_token",
    "adapter_pid",
    "adapter_pgid",
    "adapter_start_token",
    "started_at",
    "heartbeat_at",
    "exit_code",
    "finished_at",
    "final_ref",
    "updated_at",
}
_LAUNCHED_STATUS_FIELDS = _EXECUTION_STATUS_FIELDS - {
    "runner_pid",
    "runner_pgid",
    "runner_start_token",
    "adapter_pid",
    "adapter_pgid",
    "adapter_start_token",
    "started_at",
    "heartbeat_at",
    "exit_code",
    "finished_at",
    "final_ref",
}
_RUNNING_STATUS_FIELDS = _EXECUTION_STATUS_FIELDS - {
    "exit_code",
    "finished_at",
    "final_ref",
}
_LOST_STATUS_FIELDS = _RUNNING_STATUS_FIELDS | {"lost_at", "reason"}
_LAUNCH_FIELDS = {
    "schema_version",
    "task_id",
    "actor",
    "brief_sha256",
    "ownership_sha256",
    "base_commit",
    "security_profile",
    "isolation_scope",
    "capabilities_enforced",
    "carrier",
    "tmux_session",
    "tmux_socket",
    "host",
    "runner_version",
    "runner_cwd",
    "runner_invocation",
    "launched_at",
    "launch_sha256",
}
_FINAL_FIELDS = {
    "schema_version",
    "task_id",
    "state",
    "brief_sha256",
    "ownership_sha256",
    "launch_sha256",
    "security_profile",
    "isolation_scope",
    "capabilities_enforced",
    "host",
    "runner_pid",
    "runner_pgid",
    "runner_start_token",
    "adapter_pid",
    "adapter_pgid",
    "adapter_start_token",
    "started_at",
    "finished_at",
    "duration_seconds",
    "exit_code",
    "timeout",
    "stop",
    "signal_sequence",
    "stdout",
    "stderr",
    "error",
    "final_sha256",
}
_STOP_FIELDS = {
    "schema_version",
    "task_id",
    "actor",
    "reason",
    "signal",
    "requested_at",
    "brief_sha256",
    "ownership_sha256",
    "launch_sha256",
    "host",
    "adapter_pid",
    "adapter_pgid",
    "adapter_start_token",
    "adapter_sha256",
    "stop_sha256",
}
_STOP_RESULT_FIELDS = _STOP_FIELDS | {
    "delivered",
    "signal_sequence",
    "recorded_at",
    "stop_result_sha256",
}
_EXECUTION_FIELDS = {
    "schema_version",
    "task_id",
    "brief_sha256",
    "ownership_sha256",
    "launch_sha256",
    "host",
    "runner_pid",
    "runner_pgid",
    "runner_start_token",
    "claimed_at",
    "execution_sha256",
}
_ADAPTER_FIELDS = {
    "schema_version",
    "task_id",
    "brief_sha256",
    "ownership_sha256",
    "launch_sha256",
    "execution_sha256",
    "host",
    "adapter_pid",
    "adapter_pgid",
    "adapter_start_token",
    "started_at",
    "adapter_sha256",
}
_ALLOWED_SIGNALS = {
    "TERM": signal.SIGTERM,
    "INT": signal.SIGINT,
    "KILL": signal.SIGKILL,
}
_PROCESS_START_TOKEN = re.compile(r"^linux-proc-start:[0-9]+$")
_ADAPTER_LAUNCH_GATE = (
    "import os,sys;"
    "fd=int(sys.argv[1]);"
    "argv=sys.argv[2:];"
    "token=os.read(fd,1);"
    "os.close(fd);"
    "token==b'\\x01' or sys.exit(125);"
    "os.execvpe(argv[0],argv,os.environ)"
)


def adapter_environment(
    runtime: Path,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    ambient = os.environ if source is None else source
    environment = {
        key: ambient[key] for key in _ADAPTER_ENVIRONMENT_KEYS if key in ambient
    }
    environment["HOME"] = str(runtime / "home")
    environment["TMPDIR"] = str(runtime / "tmp")
    return environment


def runner_environment(runtime: Path) -> dict[str, str]:
    return adapter_environment(runtime)


def _timestamp_age(timestamp: object) -> float:
    value = _validate_timestamp(timestamp, "task timestamp")
    recorded = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return max(0.0, (datetime.now(timezone.utc) - recorded).total_seconds())


def load_launch(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    launch = _read_object(service._launch_path(task_id), "task launch")
    if set(launch) != _LAUNCH_FIELDS or type(launch.get("schema_version")) is not int:
        raise TaskError(f"invalid task launch schema: {task_id}")
    if launch["schema_version"] != 1 or launch["task_id"] != task_id:
        raise TaskError(f"task launch identity mismatch: {task_id}")
    for field in ("brief_sha256", "ownership_sha256", "launch_sha256"):
        _validate_hash(launch[field], f"task launch {field}")
    if (
        launch["brief_sha256"] != brief["brief_sha256"]
        or launch["ownership_sha256"] != ownership["ownership_sha256"]
        or launch["base_commit"] != brief["base_commit"]
    ):
        raise TaskError(f"task launch lineage mismatch: {task_id}")
    if launch["launch_sha256"] != _record_sha256(launch, "launch_sha256"):
        raise TaskError(f"task launch hash mismatch: {task_id}")
    actor = _validate_text(launch["actor"], "task launch actor")
    if actor != launch["actor"] or actor != ownership["actor"]:
        raise TaskError(f"task launch actor mismatch: {task_id}")
    if (
        launch["security_profile"] != "trusted-local"
        or launch["isolation_scope"] != "application"
        or launch["capabilities_enforced"] is not False
        or launch["carrier"] != "tmux"
        or launch["runner_version"] != 1
        or launch["host"] != socket.gethostname()
    ):
        raise TaskError(f"invalid task launch execution profile: {task_id}")
    expected_invocation = [
        sys.executable,
        "-I",
        "-c",
        _TASK_RUNNER_BOOTSTRAP,
        str(service._runtime_path(task_id) / "runner-import"),
        "--workspace",
        str(service.root),
        "--task-id",
        task_id,
    ]
    if (
        launch["tmux_session"] != f"aros-task-{task_id.lower()}"
        or launch["tmux_socket"] != _tmux_socket_name(service.root, task_id)
        or launch["runner_cwd"] != str(service._runtime_path(task_id) / "home")
        or launch["runner_invocation"] != expected_invocation
    ):
        raise TaskError(f"invalid task launch carrier binding: {task_id}")
    _validate_timestamp(launch["launched_at"], "task launched_at")
    return launch


def load_execution_claim(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    claim = _read_object(service._execution_path(task_id), "task execution claim")
    if set(claim) != _EXECUTION_FIELDS or claim.get("schema_version") != 1:
        raise TaskError(f"invalid task execution claim schema: {task_id}")
    if claim["task_id"] != task_id:
        raise TaskError(f"task execution claim identity mismatch: {task_id}")
    for field in (
        "brief_sha256",
        "ownership_sha256",
        "launch_sha256",
        "execution_sha256",
    ):
        _validate_hash(claim[field], f"task execution claim {field}")
    if (
        claim["brief_sha256"] != brief["brief_sha256"]
        or claim["ownership_sha256"] != ownership["ownership_sha256"]
        or claim["launch_sha256"] != launch["launch_sha256"]
        or claim["host"] != launch["host"]
        or claim["execution_sha256"] != _record_sha256(claim, "execution_sha256")
    ):
        raise TaskError(f"task execution claim binding mismatch: {task_id}")
    if (
        type(claim["runner_pid"]) is not int
        or int(claim["runner_pid"]) <= 1
        or type(claim["runner_pgid"]) is not int
        or int(claim["runner_pgid"]) <= 1
        or not isinstance(claim["runner_start_token"], str)
        or _PROCESS_START_TOKEN.fullmatch(claim["runner_start_token"]) is None
    ):
        raise TaskError(f"invalid task execution claim process identity: {task_id}")
    _validate_timestamp(claim["claimed_at"], "task execution claimed_at")
    return claim


def create_execution_claim(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    runner_identity: tuple[int, int, str],
) -> dict[str, object] | None:
    task_id = str(brief["task_id"])
    if _path_exists(service._execution_path(task_id)):
        load_execution_claim(service, brief, ownership, launch)
        return None
    claim: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "host": launch["host"],
        "runner_pid": runner_identity[0],
        "runner_pgid": runner_identity[1],
        "runner_start_token": runner_identity[2],
        "claimed_at": utc_now(),
    }
    claim["execution_sha256"] = _record_sha256(claim, "execution_sha256")
    if not create_json(service._execution_path(task_id), claim):
        load_execution_claim(service, brief, ownership, launch)
        return None
    recorded = load_execution_claim(service, brief, ownership, launch)
    if recorded != claim:
        raise TaskError(f"task execution claim differs after write: {task_id}")
    return recorded


def load_adapter_claim(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    execution: dict[str, object],
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    adapter = _read_object(service._adapter_path(task_id), "task adapter claim")
    if set(adapter) != _ADAPTER_FIELDS or adapter.get("schema_version") != 1:
        raise TaskError(f"invalid task adapter claim schema: {task_id}")
    if adapter["task_id"] != task_id:
        raise TaskError(f"task adapter claim identity mismatch: {task_id}")
    for field in (
        "brief_sha256",
        "ownership_sha256",
        "launch_sha256",
        "execution_sha256",
        "adapter_sha256",
    ):
        _validate_hash(adapter[field], f"task adapter claim {field}")
    if (
        adapter["brief_sha256"] != brief["brief_sha256"]
        or adapter["ownership_sha256"] != ownership["ownership_sha256"]
        or adapter["launch_sha256"] != launch["launch_sha256"]
        or adapter["execution_sha256"] != execution["execution_sha256"]
        or adapter["host"] != launch["host"]
        or adapter["adapter_sha256"] != _record_sha256(adapter, "adapter_sha256")
        or type(adapter["adapter_pid"]) is not int
        or int(adapter["adapter_pid"]) <= 1
        or adapter["adapter_pgid"] != adapter["adapter_pid"]
        or not isinstance(adapter["adapter_start_token"], str)
        or _PROCESS_START_TOKEN.fullmatch(adapter["adapter_start_token"]) is None
    ):
        raise TaskError(f"task adapter claim binding mismatch: {task_id}")
    _validate_timestamp(adapter["started_at"], "task adapter started_at")
    return adapter


def create_adapter_claim(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    execution: dict[str, object],
    adapter_identity: tuple[int, int, str],
    started_at: str,
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    adapter: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "execution_sha256": execution["execution_sha256"],
        "host": launch["host"],
        "adapter_pid": adapter_identity[0],
        "adapter_pgid": adapter_identity[1],
        "adapter_start_token": adapter_identity[2],
        "started_at": started_at,
    }
    adapter["adapter_sha256"] = _record_sha256(adapter, "adapter_sha256")
    if not create_json(service._adapter_path(task_id), adapter):
        recorded = load_adapter_claim(
            service,
            brief,
            ownership,
            launch,
            execution,
        )
        if recorded != adapter:
            raise TaskError(f"task adapter claim already differs: {task_id}")
        return recorded
    return load_adapter_claim(service, brief, ownership, launch, execution)


def _validate_process_identity_fields(
    record: dict[str, object],
    *,
    allow_missing: bool,
) -> None:
    for label in ("runner", "adapter"):
        pid = record.get(f"{label}_pid")
        pgid = record.get(f"{label}_pgid")
        token = record.get(f"{label}_start_token")
        if allow_missing and pid is None and pgid is None and token is None:
            continue
        if (
            type(pid) is not int
            or pid <= 1
            or type(pgid) is not int
            or pgid <= 1
            or not isinstance(token, str)
            or _PROCESS_START_TOKEN.fullmatch(token) is None
        ):
            raise TaskError(f"invalid task {label} process identity")
        if label == "adapter" and pgid != pid:
            raise TaskError("invalid task adapter process-group identity")


def _validate_file_receipt(path: Path, value: object, relative: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "bytes", "sha256"}
        or value.get("path") != relative
        or type(value.get("bytes")) is not int
        or int(value["bytes"]) < 0
    ):
        raise TaskError(f"invalid task output receipt: {relative}")
    _validate_hash(value.get("sha256"), "task output receipt sha256")
    if value != _file_receipt(path, relative):
        raise TaskError(f"task output receipt hash or size mismatch: {relative}")


def load_final(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    final = _read_object(service._final_path(task_id), "task final receipt")
    if set(final) != _FINAL_FIELDS or type(final.get("schema_version")) is not int:
        raise TaskError(f"invalid task final receipt schema: {task_id}")
    if final["schema_version"] != 1 or final["task_id"] != task_id:
        raise TaskError(f"task final receipt identity mismatch: {task_id}")
    for field in (
        "brief_sha256",
        "ownership_sha256",
        "launch_sha256",
        "final_sha256",
    ):
        _validate_hash(final[field], f"task final receipt {field}")
    if (
        final["brief_sha256"] != brief["brief_sha256"]
        or final["ownership_sha256"] != ownership["ownership_sha256"]
        or final["launch_sha256"] != launch["launch_sha256"]
        or final["security_profile"] != launch["security_profile"]
        or final["isolation_scope"] != launch["isolation_scope"]
        or final["capabilities_enforced"] != launch["capabilities_enforced"]
        or final["host"] != launch["host"]
    ):
        raise TaskError(f"task final receipt lineage mismatch: {task_id}")
    if final["final_sha256"] != _record_sha256(final, "final_sha256"):
        raise TaskError(f"task final receipt hash mismatch: {task_id}")
    if final["state"] not in _TERMINAL_STATES:
        raise TaskError(f"invalid task final state: {task_id}")
    _validate_process_identity_fields(final, allow_missing=True)
    if _path_exists(service._execution_path(task_id)):
        execution = load_execution_claim(service, brief, ownership, launch)
        if any(
            final[field] != execution[field]
            for field in ("runner_pid", "runner_pgid", "runner_start_token")
        ):
            raise TaskError(f"task final execution identity mismatch: {task_id}")
    elif any(
        final[field] is not None
        for field in ("runner_pid", "runner_pgid", "runner_start_token")
    ):
        raise TaskError(f"task final is missing its execution claim: {task_id}")
    if _path_exists(service._adapter_path(task_id)):
        execution = load_execution_claim(service, brief, ownership, launch)
        adapter = load_adapter_claim(
            service,
            brief,
            ownership,
            launch,
            execution,
        )
        if any(
            final[field] != adapter[field]
            for field in (
                "adapter_pid",
                "adapter_pgid",
                "adapter_start_token",
                "started_at",
            )
        ):
            raise TaskError(f"task final adapter identity mismatch: {task_id}")
    elif any(
        final[field] is not None
        for field in ("adapter_pid", "adapter_pgid", "adapter_start_token")
    ):
        raise TaskError(f"task final is missing its adapter claim: {task_id}")
    started_at = _validate_timestamp(final["started_at"], "task final started_at")
    finished_at = _validate_timestamp(final["finished_at"], "task final finished_at")
    started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    finished = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    if finished < started:
        raise TaskError(f"invalid task final timing: {task_id}")
    duration = final["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise TaskError(f"invalid task final duration: {task_id}")
    if final["exit_code"] is not None and type(final["exit_code"]) is not int:
        raise TaskError(f"invalid task final exit code: {task_id}")
    timeout = final["timeout"]
    if (
        not isinstance(timeout, dict)
        or set(timeout) != {"timeout_seconds", "triggered"}
        or timeout["timeout_seconds"] != brief["timeout_seconds"]
        or type(timeout["triggered"]) is not bool
    ):
        raise TaskError(f"invalid task final timeout: {task_id}")
    if (final["state"] == "timed_out") != timeout["triggered"]:
        raise TaskError(f"task final state and timeout conflict: {task_id}")
    if final["state"] == "completed" and final["exit_code"] != 0:
        raise TaskError(f"task final completed state has a nonzero exit: {task_id}")
    if final["state"] == "failed_process" and final["exit_code"] == 0:
        raise TaskError(f"task final failed state has a zero exit: {task_id}")
    if final["stop"] is not None:
        if not isinstance(final["stop"], dict) or not _path_exists(
            service._stop_result_path(task_id)
        ):
            raise TaskError(f"invalid task final stop attribution: {task_id}")
        stop_result = validate_stop_result(service, brief, ownership, launch)
        if final["stop"] != stop_result:
            raise TaskError(f"task final stop attribution mismatch: {task_id}")
    elif _path_exists(service._stop_result_path(task_id)):
        raise TaskError(f"task final omits its stop result: {task_id}")
    delivered_stop = bool(
        isinstance(final["stop"], dict) and final["stop"].get("delivered") is True
    )
    if final["state"] == "cancelled" and not delivered_stop:
        raise TaskError(f"task final state and stop delivery conflict: {task_id}")
    if delivered_stop and final["state"] not in {"cancelled", "timed_out"}:
        raise TaskError(f"task final stop delivery has invalid state: {task_id}")
    if not isinstance(final["signal_sequence"], list) or any(
        item not in {"TERM", "INT", "KILL"} for item in final["signal_sequence"]
    ):
        raise TaskError(f"invalid task final signal sequence: {task_id}")
    if final["state"] in {"cancelled", "timed_out"} and final[
        "signal_sequence"
    ] not in (["TERM"], ["TERM", "KILL"]):
        raise TaskError(f"invalid task final TERM/KILL sequence: {task_id}")
    if (
        final["state"] == "cancelled"
        and final["signal_sequence"] != final["stop"]["signal_sequence"]
    ):
        raise TaskError(f"task final stop signal sequence mismatch: {task_id}")
    if final["error"] is not None and not isinstance(final["error"], str):
        raise TaskError(f"invalid task final error: {task_id}")
    runtime = service._runtime_path(task_id)
    _validate_file_receipt(
        runtime / "stdout.log",
        final["stdout"],
        f".aros/tasks/{task_id}/stdout.log",
    )
    _validate_file_receipt(
        runtime / "stderr.log",
        final["stderr"],
        f".aros/tasks/{task_id}/stderr.log",
    )
    return final


def launched_status(
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": "launched",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "actor": launch["actor"],
        "carrier": launch["carrier"],
        "tmux_session": launch["tmux_session"],
        "host": launch["host"],
        "launched_at": launch["launched_at"],
        "updated_at": launch["launched_at"],
    }


def running_status_from(status: dict[str, object]) -> dict[str, object]:
    running = {field: status[field] for field in _RUNNING_STATUS_FIELDS}
    running["state"] = "running"
    if status.get("state") != "running":
        running["updated_at"] = utc_now()
    return running


def terminal_status(
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    final: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": final["state"],
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "actor": launch["actor"],
        "carrier": launch["carrier"],
        "tmux_session": launch["tmux_session"],
        "host": launch["host"],
        "launched_at": launch["launched_at"],
        "runner_pid": final["runner_pid"],
        "runner_pgid": final["runner_pgid"],
        "runner_start_token": final["runner_start_token"],
        "adapter_pid": final["adapter_pid"],
        "adapter_pgid": final["adapter_pgid"],
        "adapter_start_token": final["adapter_start_token"],
        "started_at": final["started_at"],
        "heartbeat_at": final["finished_at"],
        "exit_code": final["exit_code"],
        "finished_at": final["finished_at"],
        "final_ref": f".aros/tasks/{brief['task_id']}/final.json",
        "updated_at": final["finished_at"],
    }


def lost_status(
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    prior: dict[str, object] | None,
) -> dict[str, object]:
    lost_at = (
        str(prior["lost_at"])
        if prior is not None and prior.get("state") == "lost"
        else utc_now()
    )
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": "lost",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "actor": launch["actor"],
        "carrier": launch["carrier"],
        "tmux_session": launch["tmux_session"],
        "host": launch["host"],
        "launched_at": launch["launched_at"],
        "runner_pid": prior.get("runner_pid") if prior is not None else None,
        "runner_pgid": prior.get("runner_pgid") if prior is not None else None,
        "runner_start_token": prior.get("runner_start_token")
        if prior is not None
        else None,
        "adapter_pid": prior.get("adapter_pid") if prior is not None else None,
        "adapter_pgid": prior.get("adapter_pgid") if prior is not None else None,
        "adapter_start_token": prior.get("adapter_start_token")
        if prior is not None
        else None,
        "started_at": prior.get("started_at") if prior is not None else None,
        "heartbeat_at": prior.get("heartbeat_at", lost_at)
        if prior is not None
        else lost_at,
        "lost_at": lost_at,
        "reason": "process_absent_without_final_receipt",
        "updated_at": lost_at,
    }


def _process_state_and_token(pid: int) -> tuple[str, str] | None:
    if pid <= 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = raw.rsplit(")", 1)[1].split()
        return fields_after_name[0], f"linux-proc-start:{fields_after_name[19]}"
    except (OSError, IndexError, ValueError):
        return None


def process_status_is_live(status: dict[str, object]) -> bool:
    pid = status.get("adapter_pid")
    pgid = status.get("adapter_pgid")
    token = status.get("adapter_start_token")
    if (
        status.get("host") != socket.gethostname()
        or type(pid) is not int
        or pid <= 1
        or type(pgid) is not int
        or pgid <= 1
        or not isinstance(token, str)
    ):
        return False
    identity = _process_state_and_token(pid)
    if identity is None or identity[0] in {"Z", "X", "x"} or identity[1] != token:
        return False
    try:
        matching_group = os.getpgid(pid) == pgid
    except (OSError, ProcessLookupError):
        return False
    confirmed = _process_state_and_token(pid)
    return bool(
        matching_group
        and confirmed is not None
        and confirmed[0] not in {"Z", "X", "x"}
        and confirmed[1] == token
    )


def adapter_claim_is_live(adapter: dict[str, object]) -> bool:
    return process_status_is_live(
        {
            "host": adapter["host"],
            "adapter_pid": adapter["adapter_pid"],
            "adapter_pgid": adapter["adapter_pgid"],
            "adapter_start_token": adapter["adapter_start_token"],
        }
    )


def running_status_from_claims(
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    execution: dict[str, object],
    adapter: dict[str, object],
) -> dict[str, object]:
    status = _running_status(
        brief,
        ownership,
        launch,
        (
            int(execution["runner_pid"]),
            int(execution["runner_pgid"]),
            str(execution["runner_start_token"]),
        ),
        (
            int(adapter["adapter_pid"]),
            int(adapter["adapter_pgid"]),
            str(adapter["adapter_start_token"]),
        ),
        str(adapter["started_at"]),
    )
    reconciled_at = utc_now()
    status["heartbeat_at"] = reconciled_at
    status["updated_at"] = reconciled_at
    return status


def validate_execution_status(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    status: dict[str, object],
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    state = status.get("state")
    if state == "launched":
        fields = _LAUNCHED_STATUS_FIELDS
    elif state == "running":
        fields = _RUNNING_STATUS_FIELDS
    elif state in _TERMINAL_STATES:
        fields = _EXECUTION_STATUS_FIELDS
    elif state == "lost":
        fields = _LOST_STATUS_FIELDS
    else:
        raise TaskError(f"invalid task status state: {task_id}")
    if set(status) != fields or type(status.get("schema_version")) is not int:
        raise TaskError(f"invalid task status schema: {task_id}")
    if status["schema_version"] != 1 or status["task_id"] != task_id:
        raise TaskError(f"task status identity mismatch: {task_id}")
    _validate_hash(status["brief_sha256"], "task status brief_sha256")
    _validate_hash(status["ownership_sha256"], "task status ownership_sha256")
    _validate_hash(status["launch_sha256"], "task status launch_sha256")
    _validate_timestamp(status["updated_at"], "task status updated_at")
    launch = load_launch(service, brief, ownership)
    if (
        status["brief_sha256"] != brief["brief_sha256"]
        or status["ownership_sha256"] != ownership["ownership_sha256"]
        or status["launch_sha256"] != launch["launch_sha256"]
        or status["actor"] != launch["actor"]
        or status["carrier"] != launch["carrier"]
        or status["tmux_session"] != launch["tmux_session"]
        or status["host"] != launch["host"]
        or status["launched_at"] != launch["launched_at"]
    ):
        raise TaskError(f"task status launch binding mismatch: {task_id}")
    if state == "launched":
        if status["updated_at"] != launch["launched_at"]:
            raise TaskError(f"task launched status timestamp mismatch: {task_id}")
        return status
    _validate_process_identity_fields(status, allow_missing=state != "running")
    if state == "running" or status["adapter_pid"] is not None:
        if not _path_exists(service._execution_path(task_id)) or not _path_exists(
            service._adapter_path(task_id)
        ):
            raise TaskError(
                f"task status is missing immutable process claims: {task_id}"
            )
        execution = load_execution_claim(service, brief, ownership, launch)
        adapter = load_adapter_claim(
            service,
            brief,
            ownership,
            launch,
            execution,
        )
        if any(
            status[field] != execution[field]
            for field in ("runner_pid", "runner_pgid", "runner_start_token")
        ) or any(
            status[field] != adapter[field]
            for field in (
                "adapter_pid",
                "adapter_pgid",
                "adapter_start_token",
                "started_at",
            )
        ):
            raise TaskError(f"task status process claim mismatch: {task_id}")
    _validate_timestamp(status["heartbeat_at"], "task heartbeat_at")
    if status["started_at"] is not None:
        _validate_timestamp(status["started_at"], "task started_at")
    if state in _TERMINAL_STATES:
        final = load_final(service, brief, ownership, launch)
        if status != terminal_status(brief, ownership, launch, final):
            raise TaskError(f"task terminal status differs from final: {task_id}")
    elif state == "lost":
        _validate_timestamp(status["lost_at"], "task lost_at")
        _validate_text(status["reason"], "task lost reason")
    return status


def validate_stop_request(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    status: dict[str, object] | None = None,
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    request = _read_object(service._stop_path(task_id), "task stop request")
    if set(request) != _STOP_FIELDS or request.get("schema_version") != 1:
        raise TaskError(f"invalid task stop request schema: {task_id}")
    if request["task_id"] != task_id:
        raise TaskError(f"task stop request identity mismatch: {task_id}")
    for field in (
        "brief_sha256",
        "ownership_sha256",
        "launch_sha256",
        "adapter_sha256",
        "stop_sha256",
    ):
        _validate_hash(request[field], f"task stop request {field}")
    execution = load_execution_claim(service, brief, ownership, launch)
    adapter = load_adapter_claim(
        service,
        brief,
        ownership,
        launch,
        execution,
    )
    if (
        request["brief_sha256"] != brief["brief_sha256"]
        or request["ownership_sha256"] != ownership["ownership_sha256"]
        or request["launch_sha256"] != launch["launch_sha256"]
        or request["adapter_sha256"] != adapter["adapter_sha256"]
        or request["host"] != launch["host"]
        or request["stop_sha256"] != _record_sha256(request, "stop_sha256")
        or any(
            request[field] != adapter[field]
            for field in ("adapter_pid", "adapter_pgid", "adapter_start_token")
        )
    ):
        raise TaskError(f"task stop request binding mismatch: {task_id}")
    _validate_text(request["actor"], "task stop actor")
    _validate_text(request["reason"], "task stop reason")
    if request["signal"] != "TERM":
        raise TaskError(f"invalid task stop signal: {task_id}")
    _validate_timestamp(request["requested_at"], "task stop requested_at")
    _validate_process_identity_fields(
        {
            "runner_pid": None,
            "runner_pgid": None,
            "runner_start_token": None,
            "adapter_pid": request["adapter_pid"],
            "adapter_pgid": request["adapter_pgid"],
            "adapter_start_token": request["adapter_start_token"],
        },
        allow_missing=True,
    )
    if status is not None and any(
        request[field] != status[field]
        for field in ("host", "adapter_pid", "adapter_pgid", "adapter_start_token")
    ):
        raise TaskError(f"task stop process identity mismatch: {task_id}")
    return request


def validate_stop_result(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    result = _read_object(service._stop_result_path(task_id), "task stop result")
    if set(result) != _STOP_RESULT_FIELDS or result.get("schema_version") != 1:
        raise TaskError(f"invalid task stop result schema: {task_id}")
    request = validate_stop_request(service, brief, ownership, launch)
    if any(result[field] != request[field] for field in _STOP_FIELDS):
        raise TaskError(f"task stop result request binding mismatch: {task_id}")
    if type(result["delivered"]) is not bool:
        raise TaskError(f"invalid task stop delivery result: {task_id}")
    sequence = result["signal_sequence"]
    if not isinstance(sequence, list) or (
        sequence not in ([], ["TERM"], ["TERM", "KILL"])
    ):
        raise TaskError(f"invalid task stop signal sequence: {task_id}")
    if (sequence == []) != (result["delivered"] is False):
        raise TaskError(f"invalid task stop signal sequence: {task_id}")
    _validate_timestamp(result["recorded_at"], "task stop recorded_at")
    _validate_hash(result["stop_result_sha256"], "task stop result sha256")
    if result["stop_result_sha256"] != _record_sha256(
        result,
        "stop_result_sha256",
    ):
        raise TaskError(f"task stop result hash mismatch: {task_id}")
    return result


def request_stop_locked(
    service: TaskService,
    task_id: str,
    *,
    actor: str,
    reason: str,
    signal_name: str,
) -> dict[str, object]:
    stop_actor = _validate_text(actor, "actor")
    stop_reason = _validate_text(reason, "reason")
    normalized_signal = signal_name.upper()
    if normalized_signal != "TERM":
        raise TaskError("signal_name must be TERM")
    brief = service._load_brief(task_id)
    if not _path_exists(service._ownership_path(task_id)) or not _path_exists(
        service._launch_path(task_id)
    ):
        raise TaskError(f"task is not running and cannot be stopped: {task_id}")
    ownership = service._load_ownership(brief)
    launch = service._load_launch(brief, ownership)
    if _path_exists(service._stop_path(task_id)):
        prior = validate_stop_request(service, brief, ownership, launch)
        if (
            prior["actor"] != stop_actor
            or prior["reason"] != stop_reason
            or prior["signal"] != normalized_signal
        ):
            raise TaskError(f"task already has a different stop request: {task_id}")
        return prior
    if _path_exists(service._final_path(task_id)):
        raise TaskError(f"terminal task cannot be stopped: {task_id}")
    status = service._load_task_status(brief, ownership)
    if status["state"] != "running":
        raise TaskError(
            f"task process identity is unavailable; refusing stop: {task_id}"
        )
    pid = status["adapter_pid"]
    pgid = status["adapter_pgid"]
    token = status["adapter_start_token"]
    execution = load_execution_claim(service, brief, ownership, launch)
    adapter = load_adapter_claim(
        service,
        brief,
        ownership,
        launch,
        execution,
    )
    if any(
        status[field] != adapter[field]
        for field in ("adapter_pid", "adapter_pgid", "adapter_start_token")
    ):
        raise TaskError(f"task adapter claim mismatch; refusing stop: {task_id}")
    if (
        status["host"] != socket.gethostname()
        or type(pid) is not int
        or type(pgid) is not int
        or pgid != pid
        or not isinstance(token, str)
        or process_start_token(pid) != token
    ):
        raise TaskError(f"task process identity changed; refusing stop: {task_id}")
    try:
        actual_pgid = os.getpgid(pid)
    except OSError as error:
        raise TaskError(f"task process is absent; refusing stop: {task_id}") from error
    if actual_pgid != pgid:
        raise TaskError(f"task process group changed; refusing stop: {task_id}")
    request: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "actor": stop_actor,
        "reason": stop_reason,
        "signal": normalized_signal,
        "requested_at": utc_now(),
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "adapter_sha256": adapter["adapter_sha256"],
        "host": launch["host"],
        "adapter_pid": pid,
        "adapter_pgid": pgid,
        "adapter_start_token": token,
    }
    request["stop_sha256"] = _record_sha256(request, "stop_sha256")
    if not create_json(service._stop_path(task_id), request):
        prior = validate_stop_request(service, brief, ownership, launch, status)
        if prior != request:
            raise TaskError(f"task already has a different stop request: {task_id}")
        return prior
    return validate_stop_request(service, brief, ownership, launch, status)


def _record_stop_result(
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    request: dict[str, object],
    signal_sequence: list[str],
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    result: dict[str, object] = {
        **request,
        "delivered": bool(signal_sequence),
        "signal_sequence": signal_sequence,
        "recorded_at": utc_now(),
    }
    result["stop_result_sha256"] = _record_sha256(
        result,
        "stop_result_sha256",
    )
    if not create_json(service._stop_result_path(task_id), result):
        recorded = validate_stop_result(service, brief, ownership, launch)
        if recorded != result:
            raise TaskError(f"task stop result already differs: {task_id}")
        return recorded
    return validate_stop_result(service, brief, ownership, launch)


def _terminate_recorded_group(
    request: dict[str, object],
    *,
    grace_seconds: float = 1.0,
) -> list[str]:
    pid = request["adapter_pid"]
    pgid = request["adapter_pgid"]
    token = request["adapter_start_token"]
    if (
        request["host"] != socket.gethostname()
        or type(pid) is not int
        or type(pgid) is not int
        or pid != pgid
        or not isinstance(token, str)
        or process_start_token(pid) != token
    ):
        return []
    try:
        if os.getpgid(pid) != pgid:
            return []
    except OSError:
        return []
    sequence: list[str] = []
    requested_name = str(request["signal"])
    if _signal_group(pgid, _ALLOWED_SIGNALS[requested_name]):
        sequence.append(requested_name)
    deadline = time.monotonic() + grace_seconds
    while _group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _group_exists(pgid) and requested_name != "KILL":
        if _signal_group(pgid, signal.SIGKILL):
            sequence.append("KILL")
    return sequence


def deliver_stop(service: TaskService, task_id: str) -> dict[str, object]:
    """Deliver one persisted stop without relying on the tmux runner."""
    with file_lock(service._stop_delivery_lock_path(task_id)):
        brief = service._load_brief(task_id)
        ownership = service._load_ownership(brief)
        launch = service._load_launch(brief, ownership)
        request = validate_stop_request(service, brief, ownership, launch)
        if _path_exists(service._stop_result_path(task_id)):
            return validate_stop_result(service, brief, ownership, launch)
        sequence = _terminate_recorded_group(request)
        return _record_stop_result(
            service,
            brief,
            ownership,
            launch,
            request,
            sequence,
        )


def _final_record(
    *,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    state: str,
    runner_identity: tuple[int | None, int | None, str | None],
    adapter_identity: tuple[int | None, int | None, str | None],
    started_at: str,
    duration_seconds: float,
    exit_code: int | None,
    timeout_triggered: bool,
    stop: dict[str, object] | None,
    signal_sequence: list[str],
    runtime: Path,
    error: str | None,
) -> dict[str, object]:
    task_id = str(brief["task_id"])
    finished_at = utc_now()
    final: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "state": state,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "security_profile": launch["security_profile"],
        "isolation_scope": launch["isolation_scope"],
        "capabilities_enforced": launch["capabilities_enforced"],
        "host": launch["host"],
        "runner_pid": runner_identity[0],
        "runner_pgid": runner_identity[1],
        "runner_start_token": runner_identity[2],
        "adapter_pid": adapter_identity[0],
        "adapter_pgid": adapter_identity[1],
        "adapter_start_token": adapter_identity[2],
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 6),
        "exit_code": exit_code,
        "timeout": {
            "timeout_seconds": brief["timeout_seconds"],
            "triggered": timeout_triggered,
        },
        "stop": stop,
        "signal_sequence": signal_sequence,
        "stdout": _file_receipt(
            runtime / "stdout.log",
            f".aros/tasks/{task_id}/stdout.log",
        ),
        "stderr": _file_receipt(
            runtime / "stderr.log",
            f".aros/tasks/{task_id}/stderr.log",
        ),
        "error": error,
    }
    final["final_sha256"] = _record_sha256(final, "final_sha256")
    return final


def _open_log(path: Path) -> BinaryIO:
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TaskError(f"unable to open task output log: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        pathname = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != 0
            or (metadata.st_dev, metadata.st_ino) != (pathname.st_dev, pathname.st_ino)
        ):
            raise TaskError(f"task output log must be a restrictive plain file: {path}")
        return os.fdopen(descriptor, "ab", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def _identity(pid: int) -> tuple[int, int, str]:
    token = process_start_token(pid)
    deadline = time.monotonic() + 0.2
    while token is None and time.monotonic() < deadline:
        time.sleep(0.005)
        token = process_start_token(pid)
    if token is None:
        raise TaskError(f"unable to record process start identity: {pid}")
    try:
        pgid = os.getpgid(pid)
    except OSError as error:
        raise TaskError(f"unable to record process group identity: {pid}") from error
    return pid, pgid, token


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, signal_number: int) -> bool:
    try:
        os.killpg(pgid, signal_number)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise TaskError(
            f"permission denied signalling task process group {pgid}"
        ) from error
    return True


def _terminate_group(
    process: subprocess.Popen[bytes],
    *,
    first_name: str = "TERM",
    grace_seconds: float = 1.0,
) -> list[str]:
    pgid = process.pid
    signals = {
        "TERM": signal.SIGTERM,
        "INT": signal.SIGINT,
        "KILL": signal.SIGKILL,
    }
    sequence: list[str] = []
    if _signal_group(pgid, signals[first_name]):
        sequence.append(first_name)
    deadline = time.monotonic() + grace_seconds
    while _group_exists(pgid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.02)
    if _group_exists(pgid) and first_name != "KILL":
        if _signal_group(pgid, signal.SIGKILL):
            sequence.append("KILL")
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise TaskError(
            "task adapter did not reap after process-group termination"
        ) from error
    return sequence


def _running_status(
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    runner_identity: tuple[int, int, str],
    adapter_identity: tuple[int, int, str],
    started_at: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": brief["task_id"],
        "state": "running",
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "actor": launch["actor"],
        "carrier": launch["carrier"],
        "tmux_session": launch["tmux_session"],
        "host": launch["host"],
        "launched_at": launch["launched_at"],
        "runner_pid": runner_identity[0],
        "runner_pgid": runner_identity[1],
        "runner_start_token": runner_identity[2],
        "adapter_pid": adapter_identity[0],
        "adapter_pgid": adapter_identity[1],
        "adapter_start_token": adapter_identity[2],
        "started_at": started_at,
        "heartbeat_at": started_at,
        "updated_at": started_at,
    }


def _write_terminal(
    *,
    service: TaskService,
    brief: dict[str, object],
    ownership: dict[str, object],
    launch: dict[str, object],
    state: str,
    runner_identity: tuple[int | None, int | None, str | None],
    adapter_identity: tuple[int | None, int | None, str | None],
    started_at: str,
    started_monotonic: float,
    exit_code: int | None,
    timeout_triggered: bool,
    signal_sequence: list[str],
    stop: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    task_id = str(brief["task_id"])
    runtime = service._runtime_path(task_id)
    final = _final_record(
        brief=brief,
        ownership=ownership,
        launch=launch,
        state=state,
        runner_identity=runner_identity,
        adapter_identity=adapter_identity,
        started_at=started_at,
        duration_seconds=time.monotonic() - started_monotonic,
        exit_code=exit_code,
        timeout_triggered=timeout_triggered,
        stop=stop,
        signal_sequence=signal_sequence,
        runtime=runtime,
        error=error,
    )
    lifecycle_lock = service._lifecycle_lock_path(task_id)
    with file_lock(lifecycle_lock):
        if not create_json(service._final_path(task_id), final):
            recorded = service._load_final(brief, ownership, launch)
        else:
            recorded = service._load_final(brief, ownership, launch)
        atomic_write_json(
            service._status_path(task_id),
            terminal_status(brief, ownership, launch, recorded),
        )


def record_carrier_failure(
    service: TaskService,
    task_id: str,
    detail: str,
) -> None:
    """Record a proven tmux launch failure without creating another attempt."""
    lifecycle_lock = service._lifecycle_lock_path(task_id)
    with file_lock(lifecycle_lock):
        brief = service._load_brief(task_id)
        ownership = service._load_ownership(brief)
        launch = service._load_launch(brief, ownership)
        if service._final_path(task_id).exists():
            service._load_final(brief, ownership, launch)
            return
        final = _final_record(
            brief=brief,
            ownership=ownership,
            launch=launch,
            state="failed_process",
            runner_identity=(None, None, None),
            adapter_identity=(None, None, None),
            started_at=str(launch["launched_at"]),
            duration_seconds=0.0,
            exit_code=None,
            timeout_triggered=False,
            stop=None,
            signal_sequence=[],
            runtime=service._runtime_path(task_id),
            error=f"carrier launch failed: {detail}",
        )
        if not create_json(service._final_path(task_id), final):
            service._load_final(brief, ownership, launch)
        recorded = service._load_final(brief, ownership, launch)
        atomic_write_json(
            service._status_path(task_id),
            terminal_status(brief, ownership, launch, recorded),
        )


def run(workspace: str | Path, task_id: str) -> int:
    """Run the frozen adapter command and write its create-once process receipt."""
    root = Path(workspace).expanduser().absolute()
    service = TaskService(root)
    lifecycle_lock = service._lifecycle_lock_path(task_id)
    with file_lock(lifecycle_lock):
        brief = service._load_brief(task_id)
        service._load_bound_idempotency_index(brief)
        ownership = service._load_ownership(brief)
        launch = service._load_launch(brief, ownership)
        if launch["host"] != socket.gethostname():
            raise TaskError(f"task launch belongs to a different host: {task_id}")
        if _path_exists(service._final_path(task_id)):
            service._load_final(brief, ownership, launch)
            return 0
        if _path_exists(service._execution_path(task_id)):
            load_execution_claim(service, brief, ownership, launch)
            return 0
        if _path_exists(service._adapter_path(task_id)):
            raise TaskError(
                f"task adapter claim already exists before execution: {task_id}"
            )
        runner_identity = _identity(os.getpid())
        execution = create_execution_claim(
            service,
            brief,
            ownership,
            launch,
            runner_identity,
        )
        if execution is None:
            return 0
        runtime = service._runtime_path(task_id)
        service._prepare_execution_paths(runtime, reuse_logs=True)
        worktree = Path(str(ownership["worktree_path"]))
        environment = adapter_environment(runtime)
        started_at = utc_now()
        started_monotonic = time.monotonic()
        stdout = _open_log(runtime / "stdout.log")
        stderr = _open_log(runtime / "stderr.log")
        process: subprocess.Popen[bytes] | None = None
        launch_error: Exception | None = None
        identity_error: TaskError | None = None
        claim_error: Exception | None = None
        gate_error: OSError | None = None
        adapter_identity: tuple[int, int, str] | None = None
        claim_succeeded = False
        gate_read: int | None = None
        gate_write: int | None = None
        try:
            gate_read, gate_write = os.pipe()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    _ADAPTER_LAUNCH_GATE,
                    str(gate_read),
                    *list(brief["adapter_argv"]),
                ],
                shell=False,
                cwd=worktree,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                pass_fds=(gate_read,),
            )
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
            launch_error = error
        finally:
            if gate_read is not None:
                os.close(gate_read)
                gate_read = None
        if launch_error is not None:
            if gate_write is not None:
                os.close(gate_write)
                gate_write = None
            stdout.close()
            stderr.close()
        if process is not None:
            try:
                adapter_identity = _identity(process.pid)
            except TaskError as error:
                identity_error = error
                if gate_write is not None:
                    os.close(gate_write)
                    gate_write = None
            else:
                try:
                    create_adapter_claim(
                        service,
                        brief,
                        ownership,
                        launch,
                        execution,
                        adapter_identity,
                        started_at,
                    )
                    claim_succeeded = True
                except Exception as error:
                    claim_error = error
                if claim_error is None:
                    try:
                        if gate_write is None or os.write(gate_write, b"\x01") != 1:
                            raise OSError("unable to release task adapter launch gate")
                    except OSError as error:
                        gate_error = error
                if gate_write is not None:
                    os.close(gate_write)
                    gate_write = None
                if claim_error is None and gate_error is None:
                    running = _running_status(
                        brief,
                        ownership,
                        launch,
                        runner_identity,
                        adapter_identity,
                        started_at,
                    )
                    atomic_write_json(service._status_path(task_id), running)

    if launch_error is not None:
        _write_terminal(
            service=service,
            brief=brief,
            ownership=ownership,
            launch=launch,
            state="failed_process",
            runner_identity=runner_identity,
            adapter_identity=(None, None, None),
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=None,
            timeout_triggered=False,
            signal_sequence=[],
            error=f"adapter launch failed: {launch_error}",
        )
        return 1
    if process is None:
        raise TaskError("task adapter launch produced no process")
    adapter_setup_error = identity_error or claim_error or gate_error
    if adapter_setup_error is not None:
        signal_sequence = _terminate_group(process)
        stdout.close()
        stderr.close()
        if not claim_succeeded and _path_exists(service._adapter_path(task_id)):
            try:
                recorded_adapter = load_adapter_claim(
                    service,
                    brief,
                    ownership,
                    launch,
                    execution,
                )
            except TaskError:
                return 1
            adapter_identity = (
                int(recorded_adapter["adapter_pid"]),
                int(recorded_adapter["adapter_pgid"]),
                str(recorded_adapter["adapter_start_token"]),
            )
            claim_succeeded = True
        _write_terminal(
            service=service,
            brief=brief,
            ownership=ownership,
            launch=launch,
            state="failed_process",
            runner_identity=runner_identity,
            adapter_identity=(
                adapter_identity
                if claim_succeeded and adapter_identity is not None
                else (None, None, None)
            ),
            started_at=started_at,
            started_monotonic=started_monotonic,
            exit_code=process.returncode,
            timeout_triggered=False,
            signal_sequence=signal_sequence,
            error=f"adapter setup failed: {adapter_setup_error}",
        )
        return 1

    if adapter_identity is None:
        raise TaskError("task adapter identity is unavailable after gate release")

    timeout_seconds = float(brief["timeout_seconds"])
    timeout_triggered = False
    signal_sequence: list[str] = []
    stop_request: dict[str, object] | None = None
    stop_result: dict[str, object] | None = None
    stop_handled = False
    last_heartbeat = time.monotonic()
    while process.poll() is None:
        if not stop_handled:
            with file_lock(lifecycle_lock):
                if _path_exists(service._stop_path(task_id)):
                    stop_request = validate_stop_request(
                        service,
                        brief,
                        ownership,
                        launch,
                        running,
                    )
                    if _path_exists(service._stop_result_path(task_id)):
                        stop_result = validate_stop_result(
                            service,
                            brief,
                            ownership,
                            launch,
                        )
            if stop_request is not None:
                if (
                    stop_result is None
                    and _timestamp_age(stop_request["requested_at"]) >= 1.5
                ):
                    identity_matches = (
                        stop_request["adapter_pid"] == adapter_identity[0]
                        and stop_request["adapter_pgid"] == adapter_identity[1]
                        and stop_request["adapter_start_token"] == adapter_identity[2]
                        and process_start_token(process.pid) == adapter_identity[2]
                    )
                    try:
                        identity_matches = identity_matches and (
                            os.getpgid(process.pid) == adapter_identity[1]
                        )
                    except OSError:
                        identity_matches = False
                    stop_sequence = (
                        _terminate_group(
                            process,
                            first_name=str(stop_request["signal"]),
                        )
                        if identity_matches
                        else []
                    )
                    with file_lock(lifecycle_lock):
                        stop_result = _record_stop_result(
                            service,
                            brief,
                            ownership,
                            launch,
                            stop_request,
                            stop_sequence,
                        )
                if stop_result is not None:
                    stop_handled = True
                    if stop_result["delivered"] is True:
                        signal_sequence = list(stop_result["signal_sequence"])
        if (
            stop_request is None
            and time.monotonic() - started_monotonic >= timeout_seconds
        ):
            timeout_triggered = True
            signal_sequence = _terminate_group(process)
            break
        now = time.monotonic()
        if now - last_heartbeat >= 0.2:
            heartbeat_at = utc_now()
            running["heartbeat_at"] = heartbeat_at
            running["updated_at"] = heartbeat_at
            with file_lock(lifecycle_lock):
                if not service._final_path(task_id).exists():
                    atomic_write_json(service._status_path(task_id), running)
            last_heartbeat = now
        if process.poll() is None:
            time.sleep(0.02)

    exit_code = process.wait()
    if _path_exists(service._stop_path(task_id)) and stop_result is None:
        if stop_request is None:
            with file_lock(lifecycle_lock):
                stop_request = validate_stop_request(
                    service,
                    brief,
                    ownership,
                    launch,
                )
        result_deadline = time.monotonic() + max(
            0.0,
            1.2 - _timestamp_age(stop_request["requested_at"]),
        )
        while (
            not _path_exists(service._stop_result_path(task_id))
            and time.monotonic() < result_deadline
        ):
            time.sleep(0.02)
        with file_lock(lifecycle_lock):
            if _path_exists(service._stop_result_path(task_id)):
                stop_result = validate_stop_result(
                    service,
                    brief,
                    ownership,
                    launch,
                )
            else:
                stop_result = _record_stop_result(
                    service,
                    brief,
                    ownership,
                    launch,
                    stop_request,
                    [],
                )
    if (
        not timeout_triggered
        and stop_result is not None
        and stop_result["delivered"] is True
    ):
        signal_sequence = list(stop_result["signal_sequence"])
    stdout.close()
    stderr.close()
    if timeout_triggered:
        state = "timed_out"
    elif stop_result is not None and stop_result["delivered"] is True:
        state = "cancelled"
    elif exit_code == 0:
        state = "completed"
    else:
        state = "failed_process"
    _write_terminal(
        service=service,
        brief=brief,
        ownership=ownership,
        launch=launch,
        state=state,
        runner_identity=runner_identity,
        adapter_identity=adapter_identity,
        started_at=started_at,
        started_monotonic=started_monotonic,
        exit_code=exit_code,
        timeout_triggered=timeout_triggered,
        signal_sequence=signal_sequence,
        stop=stop_result,
    )
    return 0 if state in {"completed", "timed_out", "cancelled"} else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--task-id", required=True)
    arguments = parser.parse_args()
    return run(arguments.workspace, arguments.task_id)


if __name__ == "__main__":
    raise SystemExit(main())
