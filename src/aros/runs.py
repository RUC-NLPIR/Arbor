"""Durable, tmux-carried experiment control for AROS.

The versioned manifest defines intent.  Runtime status, logs, receipts, and
events live under ``.aros``.  tmux is only the first local carrier and is never
treated as the source of process truth.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .isolation import IsolationError, isolated_linux_policy, probe_isolated_linux
from .store import (
    atomic_write_json,
    create_json,
    environment_fingerprint as _environment_fingerprint,
    file_lock,
    final_identity as _final_identity,
    json_sha256 as _sha256,
    manifest_sha256 as _manifest_sha256,
    process_start_token as _process_start_token,
    read_json,
    utc_now as _utc_now,
)


_RUN_ID = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$")
_TERMINAL_STATES = {
    "completed",
    "failed_process",
    "timed_out",
    "cancelled",
    "lost",
}
_ACTIVE_STATES = {"launched", "running"}
_ALLOWED_SIGNALS = {
    "TERM": signal.SIGTERM,
    "KILL": signal.SIGKILL,
    "INT": signal.SIGINT,
}
class RunError(ValueError):
    """Raised when a durable run request is invalid or unsafe."""


class RunService:
    """Operate durable runs rooted in one embedded AROS Git workspace."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self._require_git_root()

    def prepare(
        self,
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_seconds: float = 3600,
        idempotency_key: str,
        actor: str,
        label: str | None = None,
        security_profile: str = "isolated-linux",
        writable_paths: list[str] | None = None,
    ) -> dict[str, object]:
        """Freeze a launch manifest without starting a process."""
        normalized_argv = _validate_argv(argv)
        normalized_cwd = self._normalize_cwd(cwd)
        timeout = _validate_timeout(timeout_seconds)
        key = _validate_text(idempotency_key, "idempotency_key")
        run_actor = _validate_text(actor, "actor")
        run_label = _normalize_label(label)
        profile = _validate_security_profile(security_profile)
        if profile == "isolated-linux":
            try:
                policy = isolated_linux_policy(self.root, writable_paths or [])
            except IsolationError as error:
                raise RunError(f"invalid isolated-linux policy: {error}") from error
            normalized_writable_paths = list(policy.writable_paths)
            network_policy = policy.network_policy
            process_policy = policy.process_policy
            environment_policy = policy.environment_policy
            isolation_limits: dict[str, int] | None = asdict(policy.limits)
        else:
            if writable_paths:
                raise RunError(
                    "trusted-local is unrestricted; writable_paths require isolated-linux"
                )
            normalized_writable_paths = []
            network_policy = "host"
            process_policy = "host"
            environment_policy = {"kind": "inherit"}
            isolation_limits = None
        environment_ref, environment_sha256 = _environment_fingerprint()
        requested = {
            "argv": normalized_argv,
            "cwd": normalized_cwd,
            "timeout_seconds": timeout,
            "idempotency_key": key,
            "security_profile": profile,
            "writable_paths": normalized_writable_paths,
            "network_policy": network_policy,
            "process_policy": process_policy,
            "environment_policy": environment_policy,
            "isolation_limits": isolation_limits,
            "environment_ref": environment_ref,
            "environment_sha256": environment_sha256,
            "actor": run_actor,
            "label": run_label,
        }
        request_sha256 = _sha256(requested)
        lock_path = self._idempotency_lock_path(key)

        with file_lock(lock_path):
            existing = self._manifest_for_idempotency_key(key)
            if existing is not None:
                if _request_sha256(existing) != request_sha256:
                    raise RunError(
                        "idempotency key already belongs to run "
                        f"{existing['run_id']} with a different manifest"
                    )
                return existing

            self._require_clean_git_for_prepare()
            base_commit = self._git_output("rev-parse", "--verify", "HEAD")
            if not base_commit:
                raise RunError("durable runs require a committed Git HEAD")
            created_at = _utc_now()
            run_id = self._new_run_id(run_label)
            manifest: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "repository_ref": ".",
                "base_commit": base_commit,
                "candidate_commit": None,
                "question_refs": [],
                "experiment_ref": None,
                "prediction_ref": None,
                "evaluator_ref": None,
                "evaluator_version": None,
                "seed": None,
                "dataset_ref": None,
                "resource_request": {},
                "budget": {},
                "output_paths": [
                    f".aros/runs/{run_id}/stdout.log",
                    f".aros/runs/{run_id}/stderr.log",
                    f"runs/{run_id}/final.json",
                ],
                "success_exit_codes": [0],
                "container_ref": None,
                "hard_safety_stop": {"timeout_seconds": timeout},
                **requested,
                "created_at": created_at,
            }
            manifest["manifest_sha256"] = _manifest_sha256(manifest)

            manifest_path = self._manifest_path(run_id)
            runtime = self._runtime_path(run_id)
            if not create_json(manifest_path, manifest):
                raise RunError(f"run manifest already exists: {manifest_path}")
            atomic_write_json(
                runtime / "status.json",
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "state": "prepared",
                    "manifest_sha256": manifest["manifest_sha256"],
                    "updated_at": created_at,
                },
            )
            create_json(
                self._idempotency_index_path(key),
                {
                    "schema_version": 1,
                    "idempotency_key_sha256": hashlib.sha256(
                        key.encode("utf-8")
                    ).hexdigest(),
                    "request_sha256": request_sha256,
                    "run_id": run_id,
                    "manifest_sha256": manifest["manifest_sha256"],
                    "created_at": created_at,
                },
            )
            return manifest

    def start(
        self,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        """Start a prepared run, or reattach to its existing state."""
        self._validate_run_id(run_id)
        with file_lock(self._run_lock_path(run_id)):
            manifest = self._load_manifest(run_id)
            return self._start_locked(run_id, manifest, actor)

    def _start_locked(
        self,
        run_id: str,
        manifest: dict[str, object],
        actor: str | None,
    ) -> dict[str, object]:
        status = self.status(run_id)
        if status["state"] != "prepared":
            return status
        self._validate_manifest_against_status(manifest, status)
        self._require_clean_git_for_start(manifest)
        self._normalize_cwd(str(manifest["cwd"]))
        if manifest.get("security_profile") == "isolated-linux":
            try:
                probe_isolated_linux()
            except IsolationError as error:
                raise RunError(f"isolated-linux is unavailable: {error}") from error
        elif manifest.get("security_profile") != "trusted-local":
            raise RunError("run manifest has an unsupported security profile")
        tmux = shutil.which("tmux")
        if tmux is None:
            raise RunError("tmux is required for durable local runs")
        launch_actor = _validate_text(actor or str(manifest["actor"]), "actor")
        launched_at = _utc_now()
        session_name = f"aros-{run_id.lower()}"
        runner_argv = [
            sys.executable,
            "-m",
            "arbor.aros.runner",
            "--workspace",
            str(self.root),
            "--run-id",
            run_id,
        ]

        receipt: dict[str, object] = {
            "schema_version": 1,
            "receipt_id": f"{run_id}-prelaunch",
            "kind": "run_prelaunch",
            "run_id": run_id,
            "actor": launch_actor,
            "created_at": launched_at,
            "base_commit": manifest["base_commit"],
            "manifest_sha256": manifest["manifest_sha256"],
            "carrier": "tmux",
            "tmux_session": session_name,
            "host": socket.gethostname(),
            "runner_version": 1,
            "runner_invocation": runner_argv,
        }
        receipt["receipt_sha256"] = _receipt_sha256(receipt)
        receipt_path = self._receipts_path() / f"{run_id}-prelaunch.json"
        if not create_json(receipt_path, receipt):
            receipt = _read_object(receipt_path, "prelaunch receipt")
            recorded_receipt_hash = receipt.get("receipt_sha256")
            if (
                not isinstance(recorded_receipt_hash, str)
                or recorded_receipt_hash != _receipt_sha256(receipt)
                or receipt.get("manifest_sha256") != manifest.get("manifest_sha256")
            ):
                raise RunError(f"invalid existing prelaunch receipt: {run_id}")
            launched_at = str(receipt["created_at"])
        launch_receipt_sha256 = str(receipt["receipt_sha256"])
        self._write_event(
            event_id=f"EVT-{run_id}-launch-requested",
            kind="run_launch_requested",
            run_id=run_id,
            created_at=launched_at,
            summary="A validated durable run launch was requested.",
            artifact_refs=[
                f"runs/{run_id}/manifest.json",
                f".aros/receipts/{run_id}-prelaunch.json",
            ],
        )
        launched_status: dict[str, object] = {
            **status,
            "state": "launched",
            "actor": launch_actor,
            "carrier": "tmux",
            "tmux_session": session_name,
            "host": socket.gethostname(),
            "launch_receipt_sha256": launch_receipt_sha256,
            "launched_at": launched_at,
            "updated_at": launched_at,
        }
        atomic_write_json(self._runtime_path(run_id) / "status.json", launched_status)

        try:
            result = subprocess.run(
                [
                    tmux,
                    "new-session",
                    "-d",
                    "-s",
                    session_name,
                    shlex.join(runner_argv),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._record_launch_failure(manifest, launched_at, str(error))
            raise RunError(f"tmux launch failed for {run_id}: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "unknown tmux error"
            self._record_launch_failure(manifest, launched_at, detail)
            raise RunError(f"tmux launch failed for {run_id}: {detail}")

        deadline = time.monotonic() + 2
        latest = launched_status
        while time.monotonic() < deadline:
            latest = self.status(run_id, reconcile=False)
            if latest["state"] != "launched":
                break
            time.sleep(0.02)
        return latest

    def status(
        self,
        run_id: str,
        *,
        reconcile: bool = True,
    ) -> dict[str, object]:
        manifest = self._load_manifest(run_id)
        status_path = self._runtime_path(run_id) / "status.json"
        if not status_path.is_file():
            raise RunError(f"run status does not exist: {run_id}")
        status = _read_object(status_path, "run status")
        if status.get("run_id") != run_id:
            raise RunError(f"run status identity mismatch: {run_id}")
        if status.get("manifest_sha256") != manifest.get("manifest_sha256"):
            raise RunError(f"run status manifest hash mismatch: {run_id}")
        if reconcile and status.get("state") in (_ACTIVE_STATES | {"lost"}):
            return self.reconcile(run_id)
        return status

    def list(self, *, reconcile: bool = True) -> list[dict[str, object]]:
        runs_root = self._contained_path("runs")
        if not runs_root.is_dir():
            return []
        results: list[dict[str, object]] = []
        for manifest_path in sorted(runs_root.glob("RUN-*/manifest.json")):
            run_id = manifest_path.parent.name
            results.append(self.status(run_id, reconcile=reconcile))
        return results

    def tail(
        self,
        run_id: str,
        *,
        stream: str = "stdout",
        max_bytes: int = 65_536,
    ) -> str:
        self._validate_run_id(run_id)
        if stream not in {"stdout", "stderr"}:
            raise RunError("stream must be 'stdout' or 'stderr'")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise RunError("max_bytes must be a positive integer")
        path = self._runtime_path(run_id) / f"{stream}.log"
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")

    def stop(
        self,
        run_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str = "TERM",
    ) -> dict[str, object]:
        """Request one attributed stop, serialized with launch and other stops."""
        self._validate_run_id(run_id)
        with file_lock(self._run_lock_path(run_id)):
            return self._stop_locked(
                run_id,
                actor=actor,
                reason=reason,
                signal_name=signal_name,
            )

    def _stop_locked(
        self,
        run_id: str,
        *,
        actor: str,
        reason: str,
        signal_name: str,
    ) -> dict[str, object]:
        run_actor = _validate_text(actor, "actor")
        stop_reason = _validate_text(reason, "reason")
        normalized_signal = signal_name.upper()
        if normalized_signal not in _ALLOWED_SIGNALS:
            raise RunError("signal_name must be TERM, INT, or KILL")
        status = self.status(run_id)
        receipt_path = self._receipts_path() / f"{run_id}-stop.json"
        if receipt_path.is_file():
            return _read_object(receipt_path, "stop receipt")
        if status["state"] in (_TERMINAL_STATES - {"lost"}):
            final_path = self._final_path(run_id)
            if not final_path.is_file():
                raise RunError(f"terminal run is missing its final receipt: {run_id}")
            return _read_object(final_path, "final receipt")
        if status["state"] not in _ACTIVE_STATES:
            raise RunError(f"run is not active and cannot be stopped: {run_id}")

        deadline = time.monotonic() + 1
        while status.get("process_pid") is None and time.monotonic() < deadline:
            time.sleep(0.02)
            status = self.status(run_id, reconcile=False)
            if status["state"] not in _ACTIVE_STATES:
                break
        pid = status.get("process_pid")
        pgid = status.get("process_pgid")
        expected_start_token = status.get("process_start_token")
        if not isinstance(pid, int) or pid <= 1 or not isinstance(expected_start_token, str):
            raise RunError(f"run process identity is unavailable; refusing stop: {run_id}")
        actual_start_token = _process_start_token(pid)
        if actual_start_token != expected_start_token:
            raise RunError(f"run process identity changed; refusing stop: {run_id}")

        requested_at = _utc_now()
        request = {
            "schema_version": 1,
            "run_id": run_id,
            "kind": "run_stop_request",
            "actor": run_actor,
            "reason": stop_reason,
            "signal": normalized_signal,
            "signal_sequence": [normalized_signal],
            "process_pid": pid,
            "process_start_token": expected_start_token,
            "requested_at": requested_at,
        }
        request_path = self._runtime_path(run_id) / "stop-request.json"
        if not create_json(request_path, request):
            prior = _read_object(request_path, "stop request")
            semantic_fields = (
                "schema_version",
                "run_id",
                "kind",
                "actor",
                "reason",
                "signal",
                "signal_sequence",
                "process_pid",
                "process_start_token",
            )
            if any(prior.get(field) != request.get(field) for field in semantic_fields):
                raise RunError(f"run already has a different stop request: {run_id}")
            request = prior
            requested_at = str(prior["requested_at"])
        self._write_event(
            event_id=f"EVT-{run_id}-stop-requested",
            kind="run_stop_requested",
            run_id=run_id,
            created_at=requested_at,
            summary=f"{run_actor} requested process stop: {stop_reason}",
            artifact_refs=[f".aros/runs/{run_id}/stop-request.json"],
        )

        delivered = False
        try:
            if isinstance(pgid, int) and pgid > 1:
                os.killpg(pgid, _ALLOWED_SIGNALS[normalized_signal])
                delivered = True
            elif isinstance(pid, int) and pid > 1:
                os.kill(pid, _ALLOWED_SIGNALS[normalized_signal])
                delivered = True
        except ProcessLookupError:
            delivered = False
        except PermissionError as error:
            raise RunError(f"permission denied stopping run {run_id}") from error

        receipt = {
            "schema_version": 1,
            "receipt_id": f"{run_id}-stop",
            "kind": "run_stop",
            "run_id": run_id,
            "actor": run_actor,
            "reason": stop_reason,
            "signal": normalized_signal,
            "signal_sequence": [normalized_signal],
            "requested_at": requested_at,
            "recorded_at": _utc_now(),
            "delivered": delivered,
            "process_pid": pid,
            "process_pgid": pgid,
            "process_start_token": expected_start_token,
        }
        create_json(receipt_path, receipt)
        return receipt

    def reconcile(self, run_id: str) -> dict[str, object]:
        manifest = self._load_manifest(run_id)
        runtime = self._runtime_path(run_id)
        status = _read_object(runtime / "status.json", "run status")
        if status.get("manifest_sha256") != manifest.get("manifest_sha256"):
            raise RunError(f"run status manifest hash mismatch: {run_id}")
        final_path = self._final_path(run_id)
        if final_path.is_file():
            final = _read_object(final_path, "final receipt")
            if final.get("run_id") != run_id:
                raise RunError(f"final receipt identity mismatch: {run_id}")
            if final.get("manifest_sha256") != status.get("manifest_sha256"):
                raise RunError(f"final receipt manifest hash mismatch: {run_id}")
            if final.get("launch_receipt_sha256") != status.get("launch_receipt_sha256"):
                raise RunError(f"final receipt launch lineage mismatch: {run_id}")
            state = final.get("state")
            if state not in _TERMINAL_STATES - {"lost"}:
                raise RunError(f"invalid final process state for {run_id}: {state}")
            pid = status.get("process_pid")
            token = status.get("process_start_token")
            if (
                status.get("state") in _ACTIVE_STATES
                and isinstance(pid, int)
                and isinstance(token, str)
                and _process_start_token(pid) == token
            ):
                raise RunError(
                    f"integrity conflict: final receipt exists while process is alive: {run_id}"
                )
            reconciled = {
                **status,
                "state": state,
                "exit_code": final.get("exit_code"),
                "finished_at": final.get("finished_at"),
                "final_ref": f"runs/{run_id}/final.json",
                "updated_at": final.get("finished_at") or _utc_now(),
            }
            if reconciled != status:
                atomic_write_json(runtime / "status.json", reconciled)
            self._write_event(
                event_id=f"EVT-{run_id}-completed",
                kind="run_completed",
                run_id=run_id,
                created_at=str(final.get("finished_at") or _utc_now()),
                summary=f"Run reached process state {state}.",
                artifact_refs=[f"runs/{run_id}/final.json"],
            )
            return reconciled

        if status.get("state") not in _ACTIVE_STATES:
            return status
        pid = status.get("process_pid")
        token = status.get("process_start_token")
        if (
            isinstance(pid, int)
            and isinstance(token, str)
            and _process_start_token(pid) == token
        ):
            return status
        session_name = status.get("tmux_session")
        if isinstance(session_name, str) and _tmux_session_exists(session_name):
            return status

        lost_at = _utc_now()
        lost = {
            **status,
            "state": "lost",
            "reason": "process_absent_without_final_receipt",
            "updated_at": lost_at,
        }
        atomic_write_json(runtime / "status.json", lost)
        self._write_event(
            event_id=f"EVT-{run_id}-lost",
            kind="anomaly",
            run_id=run_id,
            created_at=lost_at,
            summary="Run process is absent and no final receipt exists; state is lost.",
            artifact_refs=[f".aros/runs/{run_id}/status.json"],
        )
        return lost

    def _record_launch_failure(
        self,
        manifest: dict[str, object],
        started_at: str,
        detail: str,
    ) -> None:
        run_id = str(manifest["run_id"])
        finished_at = _utc_now()
        runtime = self._runtime_path(run_id)
        status = _read_object(runtime / "status.json", "run status")
        final = _final_identity(manifest)
        final.update(
            {
                "schema_version": 1,
                "state": "failed_process",
                "exit_code": None,
                "started_at": started_at,
                "finished_at": finished_at,
                "finalized_at": finished_at,
                "resource_usage": {"wall_seconds": 0.0},
                "error": f"carrier launch failed: {detail}",
                "launch_receipt_sha256": status["launch_receipt_sha256"],
                "stdout": _empty_output(f".aros/runs/{run_id}/stdout.log"),
                "stderr": _empty_output(f".aros/runs/{run_id}/stderr.log"),
            }
        )
        create_json(self._final_path(run_id), final)
        atomic_write_json(
            runtime / "status.json",
            {
                **status,
                "state": "failed_process",
                "exit_code": None,
                "finished_at": finished_at,
                "final_ref": f"runs/{run_id}/final.json",
                "updated_at": finished_at,
            },
        )
        self._write_event(
            event_id=f"EVT-{run_id}-completed",
            kind="run_completed",
            run_id=run_id,
            created_at=finished_at,
            summary="Run reached process state failed_process.",
            artifact_refs=[f"runs/{run_id}/final.json"],
        )

    def _write_event(
        self,
        *,
        event_id: str,
        kind: str,
        run_id: str,
        created_at: str,
        summary: str,
        artifact_refs: list[str],
    ) -> None:
        create_json(
            self._events_path() / f"{event_id}.json",
            {
                "schema_version": 1,
                "event_id": event_id,
                "kind": kind,
                "created_at": created_at,
                "source_ref": f"runs/{run_id}",
                "summary": summary,
                "artifact_refs": artifact_refs,
                "acknowledged_by": None,
                "acknowledged_at": None,
            },
        )

    def _load_manifest(self, run_id: str) -> dict[str, object]:
        self._validate_run_id(run_id)
        path = self._manifest_path(run_id)
        if not path.is_file():
            raise RunError(f"run manifest does not exist: {run_id}")
        manifest = _read_object(path, "run manifest")
        if manifest.get("run_id") != run_id:
            raise RunError(f"run manifest identity mismatch: {run_id}")
        recorded = manifest.get("manifest_sha256")
        if not isinstance(recorded, str) or recorded != _manifest_sha256(manifest):
            raise RunError(f"run manifest hash mismatch: {run_id}")
        return manifest

    def _validate_manifest_against_status(
        self,
        manifest: dict[str, object],
        status: dict[str, object],
    ) -> None:
        if manifest.get("manifest_sha256") != status.get("manifest_sha256"):
            raise RunError(f"run manifest hash differs from prepared status: {manifest['run_id']}")

    def _manifest_for_idempotency_key(
        self,
        key: str,
    ) -> dict[str, object] | None:
        index = self._idempotency_index_path(key)
        if index.is_file():
            entry = _read_object(index, "idempotency index")
            run_id = entry.get("run_id")
            if not isinstance(run_id, str):
                raise RunError("invalid idempotency index")
            return self._load_manifest(run_id)
        runs_root = self._contained_path("runs")
        if not runs_root.is_dir():
            return None
        matches: list[dict[str, object]] = []
        for path in runs_root.glob("RUN-*/manifest.json"):
            try:
                candidate = _read_object(path, "run manifest")
            except (OSError, ValueError, RunError):
                continue
            if candidate.get("idempotency_key") == key:
                matches.append(candidate)
        if len(matches) > 1:
            raise RunError(f"duplicate manifests already use idempotency key: {key}")
        return matches[0] if matches else None

    def _require_clean_git_for_prepare(self) -> None:
        changes = [
            line
            for line in self._git_changes()
            if not _is_runtime_change(line)
            and not self._is_valid_run_artifact_change(line)
        ]
        if changes:
            raise RunError(
                "prepare requires a clean Git workspace; checkpoint or preserve changes first"
            )

    def _require_clean_git_for_start(self, manifest: dict[str, object]) -> None:
        changes = [
            line
            for line in self._git_changes()
            if not _is_runtime_change(line)
            and not self._is_valid_run_artifact_change(line)
        ]
        if changes:
            raise RunError(
                "start requires clean Git state except for its frozen run manifest"
            )
        head = self._git_output("rev-parse", "--verify", "HEAD")
        base = str(manifest["base_commit"])
        if head == base:
            return
        result = self._git(
            "diff",
            "--name-only",
            base,
            str(head),
            "--",
        )
        changed_since_base = result.stdout.splitlines() if result.returncode == 0 else []
        if not changed_since_base or not all(
            self._is_valid_run_artifact_path(path) for path in changed_since_base
        ):
            raise RunError("Git HEAD no longer matches the manifest base_commit")

    def _is_valid_run_artifact_change(self, line: str) -> bool:
        if len(line) < 4 or "D" in line[:2] or "R" in line[:2]:
            return False
        return self._is_valid_run_artifact_path(_porcelain_path(line))

    def _is_valid_run_artifact_path(self, relative: str) -> bool:
        match = re.fullmatch(r"runs/(RUN-[A-Za-z0-9][A-Za-z0-9-]*)/(manifest|final)\.json", relative)
        if match is None:
            return False
        run_id, kind = match.groups()
        try:
            manifest = self._load_manifest(run_id)
            status_path = self._runtime_path(run_id) / "status.json"
            if not status_path.is_file():
                return False
            status = _read_object(status_path, "run status")
            if status.get("manifest_sha256") != manifest.get("manifest_sha256"):
                return False
            if kind == "manifest":
                return True
            final = _read_object(self._final_path(run_id), "final receipt")
            return bool(
                final.get("run_id") == run_id
                and final.get("manifest_sha256") == manifest.get("manifest_sha256")
                and final.get("state") in (_TERMINAL_STATES - {"lost"})
            )
        except (OSError, RunError, ValueError):
            return False

    def _normalize_cwd(self, cwd: str) -> str:
        if not isinstance(cwd, str) or not cwd.strip():
            raise RunError("cwd must be a non-empty workspace-relative path")
        candidate = Path(cwd)
        if candidate.is_absolute():
            resolved = candidate.expanduser().resolve()
        else:
            resolved = (self.root / candidate).resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as error:
            raise RunError("cwd must remain inside the workspace") from error
        if not resolved.is_dir():
            raise RunError(f"cwd must be an existing directory inside the workspace: {cwd}")
        return "." if relative == Path(".") else relative.as_posix()

    def _require_git_root(self) -> None:
        result = self._git("rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise RunError(f"workspace must be a Git repository root: {self.root}")
        try:
            top = Path(result.stdout.strip()).resolve()
        except OSError as error:
            raise RunError("unable to resolve Git repository root") from error
        if top != self.root:
            raise RunError(f"workspace must be the Git repository root: {self.root}")

    def _git_changes(self) -> list[str]:
        result = self._git("status", "--porcelain=v1", "--untracked-files=all")
        if result.returncode != 0:
            raise RunError("unable to inspect Git workspace state")
        return result.stdout.splitlines()

    def _git_output(self, *args: str) -> str | None:
        result = self._git(*args)
        return result.stdout.strip() if result.returncode == 0 else None

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RunError(f"Git command failed: {' '.join(args)}") from error

    def _new_run_id(self, label: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for _ in range(100):
            run_id = f"RUN-{timestamp}-{label}-{secrets.token_hex(2)}"
            if not self._manifest_path(run_id).exists():
                return run_id
        raise RunError("unable to allocate a unique run ID")

    def _validate_run_id(self, run_id: str) -> None:
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise RunError(f"invalid run ID: {run_id!r}")

    def _contained_path(self, relative: str) -> Path:
        path = self.root / relative
        try:
            path.resolve(strict=False).relative_to(self.root)
        except (OSError, ValueError) as error:
            raise RunError(f"AROS path escapes the workspace: {relative}") from error
        return path

    def _manifest_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self._contained_path(f"runs/{run_id}/manifest.json")

    def _final_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self._contained_path(f"runs/{run_id}/final.json")

    def _runtime_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self._contained_path(f".aros/runs/{run_id}")

    def _receipts_path(self) -> Path:
        return self._contained_path(".aros/receipts")

    def _events_path(self) -> Path:
        return self._contained_path(".aros/events")

    def _idempotency_lock_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._contained_path(f".aros/locks/run-idempotency-{digest}.lock")

    def _run_lock_path(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self._contained_path(f".aros/locks/run-{run_id}.lock")

    def _idempotency_index_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._contained_path(f".aros/runs/idempotency/{digest}.json")


def _receipt_sha256(receipt: dict[str, object]) -> str:
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    return _sha256(payload)


def _request_sha256(manifest: dict[str, object]) -> str:
    return _sha256(
        {
            field: manifest.get(field)
            for field in (
                "argv",
                "cwd",
                "timeout_seconds",
                "idempotency_key",
                "security_profile",
                "writable_paths",
                "network_policy",
                "process_policy",
                "environment_policy",
                "isolation_limits",
                "environment_ref",
                "environment_sha256",
                "actor",
                "label",
            )
        }
    )


def _validate_argv(argv: list[str]) -> list[str]:
    if not isinstance(argv, list) or not argv:
        raise RunError("argv must be a non-empty list of strings")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        raise RunError("argv must contain only non-empty strings without NUL bytes")
    return list(argv)


def _validate_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise RunError("timeout_seconds must be a positive number")
    return value


def _validate_security_profile(value: str) -> str:
    if value not in {"isolated-linux", "trusted-local"}:
        raise RunError("security_profile must be isolated-linux or trusted-local")
    return value


def _validate_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunError(f"{field} must be a non-empty string")
    if len(value) > 1000:
        raise RunError(f"{field} is too long")
    return value.strip()


def _normalize_label(label: str | None) -> str:
    raw = "run" if label is None else _validate_text(label, "label")
    normalized = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:32]
    return normalized or "run"


def _read_object(path: Path, description: str) -> dict[str, object]:
    try:
        value = read_json(path)
    except (OSError, ValueError) as error:
        raise RunError(f"unable to read {description}: {path}") from error
    if not isinstance(value, dict):
        raise RunError(f"invalid {description}: {path}")
    return value


def _porcelain_path(line: str) -> str:
    return line[3:] if len(line) >= 4 else ""


def _is_runtime_change(line: str) -> bool:
    path = _porcelain_path(line)
    return path == ".aros" or path.startswith(".aros/")


def _process_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def _tmux_session_exists(session_name: str) -> bool:
    tmux = shutil.which("tmux")
    if tmux is None:
        return False
    try:
        result = subprocess.run(
            [tmux, "has-session", "-t", f"={session_name}"],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _empty_output(path: str) -> dict[str, object]:
    return {
        "path": path,
        "bytes": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
