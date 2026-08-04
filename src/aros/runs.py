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
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .isolation import IsolationError, isolated_linux_policy, probe_isolated_linux
from .receipts import record_sha256
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
    read_json_strict,
    utc_now as _utc_now,
)
from .worktrees import (
    ExecutionBundle,
    WorktreeError,
    bind_repository,
    validate_execution_bundle,
)


_RUN_ID = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$"
)
_TERMINAL_STATES = {
    "completed",
    "failed_process",
    "timed_out",
    "cancelled",
    "lost",
}
_ACTIVE_STATES = {"launched", "running"}
_CONTENT_RECEIPT_FIELDS = {"path", "bytes", "sha256"}
_FINAL_REQUIRED_FIELDS = {
    "schema_version",
    "state",
    "exit_code",
    "started_at",
    "finished_at",
    "finalized_at",
    "resource_usage",
    "launch_receipt_sha256",
    "stdout",
    "stderr",
}
_FINAL_OPTIONAL_FIELDS = {
    "duration_seconds",
    "host",
    "actual_environment_sha256",
    "stop",
    "signal_sequence",
    "error",
}
_PRELAUNCH_FIELDS = {
    "schema_version",
    "receipt_id",
    "kind",
    "run_id",
    "actor",
    "created_at",
    "base_commit",
    "manifest_sha256",
    "carrier",
    "tmux_session",
    "host",
    "runner_version",
    "runner_invocation",
    "receipt_sha256",
}
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
        return self._publish_manifest(
            requested,
            repository_ref=".",
            candidate_commit=None,
            success_exit_codes=[0],
        )

    def prepare_bundle(
        self,
        bundle: ExecutionBundle,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: float,
        success_exit_codes: list[int],
        idempotency_key: str,
        actor: str,
        label: str | None = None,
    ) -> dict[str, object]:
        """Freeze a verified two-checkout launch manifest."""
        if not isinstance(bundle, ExecutionBundle):
            raise RunError("bundle must be an ExecutionBundle")
        try:
            repository = bind_repository(self.root)
            validate_execution_bundle(repository, bundle)
        except WorktreeError as error:
            raise RunError(f"invalid execution bundle: {error}") from error
        repository_ref = self._bundle_repository_ref(bundle.root)
        normalized_argv = _validate_argv(argv)
        normalized_cwd = self._normalize_bundle_cwd(bundle, cwd)
        timeout = _validate_timeout(timeout_seconds)
        exit_codes = _validate_success_exit_codes(success_exit_codes)
        key = _validate_text(idempotency_key, "idempotency_key")
        run_actor = _validate_text(actor, "actor")
        run_label = _normalize_label(label)
        try:
            policy = isolated_linux_policy(bundle.root, ["tmp"])
        except IsolationError as error:
            raise RunError(f"invalid isolated-linux policy: {error}") from error
        environment_ref, environment_sha256 = _environment_fingerprint()
        requested = {
            "argv": normalized_argv,
            "cwd": normalized_cwd,
            "timeout_seconds": timeout,
            "idempotency_key": key,
            "security_profile": "isolated-linux",
            "writable_paths": list(policy.writable_paths),
            "network_policy": policy.network_policy,
            "process_policy": policy.process_policy,
            "environment_policy": policy.environment_policy,
            "isolation_limits": asdict(policy.limits),
            "environment_ref": environment_ref,
            "environment_sha256": environment_sha256,
            "actor": run_actor,
            "label": run_label,
        }
        execution_bundle = {
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
        return self._publish_manifest(
            requested,
            repository_ref=repository_ref,
            candidate_commit=bundle.candidate.commit,
            success_exit_codes=exit_codes,
            execution_bundle=execution_bundle,
        )

    def _publish_manifest(
        self,
        requested: dict[str, object],
        *,
        repository_ref: str,
        candidate_commit: str | None,
        success_exit_codes: list[int],
        execution_bundle: dict[str, object] | None = None,
    ) -> dict[str, object]:
        key = str(requested["idempotency_key"])
        request_binding = dict(requested)
        if execution_bundle is not None:
            request_binding.update(
                {
                    "repository_ref": repository_ref,
                    "candidate_commit": candidate_commit,
                    "success_exit_codes": success_exit_codes,
                    "execution_bundle": execution_bundle,
                }
            )
        request_sha256 = _request_sha256(request_binding)
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
            run_id = self._new_run_id(str(requested["label"]))
            manifest: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "repository_ref": repository_ref,
                "base_commit": base_commit,
                "candidate_commit": candidate_commit,
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
                "success_exit_codes": success_exit_codes,
                "container_ref": None,
                "hard_safety_stop": {
                    "timeout_seconds": requested["timeout_seconds"]
                },
                **requested,
                "created_at": created_at,
            }
            if execution_bundle is not None:
                manifest["execution_bundle"] = execution_bundle
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

    def _bundle_repository_ref(self, bundle_root: Path) -> str:
        try:
            relative = bundle_root.relative_to(self.root)
        except ValueError as error:
            raise RunError("execution bundle must remain inside the workspace") from error
        if len(relative.parts) != 3 or relative.parts[:2] != (".worktree", "eval"):
            raise RunError(
                "execution bundle must be rooted at .worktree/eval/<eval-id>"
            )
        return relative.as_posix()

    def _normalize_bundle_cwd(self, bundle: ExecutionBundle, cwd: str) -> str:
        if not isinstance(cwd, str) or not cwd.strip():
            raise RunError("cwd must be a non-empty candidate-relative path")
        candidate = Path(cwd)
        if candidate.is_absolute():
            raise RunError("cwd must be a candidate-relative path")
        resolved = (bundle.candidate.path / candidate).resolve()
        try:
            relative = resolved.relative_to(bundle.candidate.path)
        except ValueError as error:
            raise RunError("cwd must remain inside the candidate checkout") from error
        if not resolved.is_dir():
            raise RunError(f"cwd must be an existing candidate directory: {cwd}")
        return Path("candidate", relative).as_posix()

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
        status = self._reconcile_locked(run_id)
        if status["state"] != "prepared":
            return status
        self._validate_manifest_against_status(manifest, status)
        self._require_clean_git_for_start(manifest)
        if "execution_bundle" in manifest:
            bundle_root = (self.root / str(manifest["repository_ref"])).resolve()
            try:
                bundle_root.relative_to(self.root)
                bundle_cwd = (bundle_root / str(manifest["cwd"])).resolve()
                bundle_cwd.relative_to(bundle_root)
            except ValueError as error:
                raise RunError("bundle cwd must remain inside the workspace") from error
            if not bundle_cwd.is_dir():
                raise RunError("bundle cwd is unavailable")
        else:
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
        launch_host = socket.gethostname()
        runner_argv = _runner_invocation(self.root, run_id)

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
            "host": launch_host,
            "runner_version": 1,
            "runner_invocation": runner_argv,
        }
        receipt["receipt_sha256"] = _receipt_sha256(receipt)
        receipt_path = self._receipts_path() / f"{run_id}-prelaunch.json"
        if not create_json(receipt_path, receipt):
            receipt = _read_strict_object(receipt_path, "prelaunch receipt")
        _validate_prelaunch_receipt(
            receipt,
            manifest,
            expected_actor=launch_actor,
            expected_host=launch_host,
            expected_session=session_name,
            expected_invocation=runner_argv,
        )
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
            "host": launch_host,
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
        status = _read_run_status(status_path)
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
        return self._tail_bytes(
            run_id,
            stream=stream,
            max_bytes=max_bytes,
        ).decode("utf-8", errors="replace")

    def _tail_bytes(
        self,
        run_id: str,
        *,
        stream: str,
        max_bytes: int,
    ) -> bytes:
        self._validate_run_id(run_id)
        if stream not in {"stdout", "stderr"}:
            raise RunError("stream must be 'stdout' or 'stderr'")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise RunError("max_bytes must be a positive integer")
        path = self._runtime_path(run_id) / f"{stream}.log"
        if not path.is_file():
            return b""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read()

    def read_verified_output(
        self,
        run_id: str,
        stream: str,
        max_bytes: int = 65_536,
    ) -> bytes:
        """Read one terminal Run log exactly as bound by its final receipt."""
        self._validate_run_id(run_id)
        if stream not in {"stdout", "stderr"}:
            raise RunError("stream must be 'stdout' or 'stderr'")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise RunError("max_bytes must be a positive integer")
        final = self.read_validated_final(run_id)
        content = final.get(stream)
        canonical = f".aros/runs/{run_id}/{stream}.log"
        if (
            not isinstance(content, dict)
            or set(content) != _CONTENT_RECEIPT_FIELDS
            or content.get("path") != canonical
        ):
            raise RunError(f"invalid {stream} receipt path: {run_id}")
        declared_size = content.get("bytes")
        declared_sha256 = content.get("sha256")
        if (
            type(declared_size) is not int
            or declared_size < 0
            or declared_size > max_bytes
            or not isinstance(declared_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", declared_sha256) is None
        ):
            raise RunError(f"invalid verified {stream} receipt: {run_id}")
        path = self.root / canonical
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RunError(
                    f"verified {stream} must be a single-link regular file: {run_id}"
                )
            if metadata.st_size != declared_size:
                raise RunError(f"verified {stream} size differs from receipt: {run_id}")
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as error:
            raise RunError(f"unable to read verified {stream}: {run_id}") from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != declared_size
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise RunError(f"verified {stream} identity differs: {run_id}")
            chunks: list[bytes] = []
            remaining = declared_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining or os.read(descriptor, 1):
                raise RunError(f"verified {stream} size differs from receipt: {run_id}")
            raw = b"".join(chunks)
            after_open = os.fstat(descriptor)
            after_path = path.lstat()
            for observed in (after_open, after_path):
                if (
                    not stat.S_ISREG(observed.st_mode)
                    or observed.st_nlink != 1
                    or observed.st_size != declared_size
                    or (observed.st_dev, observed.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise RunError(f"verified {stream} identity changed: {run_id}")
        except OSError as error:
            raise RunError(f"unable to read verified {stream}: {run_id}") from error
        finally:
            os.close(descriptor)
        if hashlib.sha256(raw).hexdigest() != declared_sha256:
            raise RunError(f"verified {stream} hash differs from receipt: {run_id}")
        return raw

    def read_validated_final(self, run_id: str) -> dict[str, object]:
        """Load one terminal final with complete manifest and launch lineage."""
        self._validate_run_id(run_id)
        manifest = self._load_manifest(run_id)
        final = _read_strict_object(self._final_path(run_id), "final receipt")
        status = _read_strict_object(
            self._runtime_path(run_id) / "status.json",
            "run status",
        )
        prelaunch = _read_strict_object(
            self._receipts_path() / f"{run_id}-prelaunch.json",
            "prelaunch receipt",
        )
        identity = _final_identity(manifest)
        required_fields = set(identity) | _FINAL_REQUIRED_FIELDS
        if not required_fields <= set(final) or not set(final) <= (
            required_fields | _FINAL_OPTIONAL_FIELDS
        ):
            raise RunError(f"invalid final receipt fields: {run_id}")
        state = final.get("state")
        exit_code = final.get("exit_code")
        started_at = final.get("started_at")
        finished_at = final.get("finished_at")
        resource_usage = final.get("resource_usage")
        launch_sha256 = final.get("launch_receipt_sha256")
        if (
            final.get("schema_version") != 1
            or state not in (_TERMINAL_STATES - {"lost"})
            or type(exit_code) not in {int, type(None)}
            or not _valid_utc_timestamp(started_at)
            or not _valid_utc_timestamp(finished_at)
            or final.get("finalized_at") != finished_at
            or str(started_at) > str(finished_at)
            or not isinstance(resource_usage, dict)
            or set(resource_usage) != {"wall_seconds"}
            or type(resource_usage.get("wall_seconds")) not in {int, float}
            or resource_usage["wall_seconds"] < 0
            or not isinstance(launch_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", launch_sha256) is None
            or any(final.get(field) != value for field, value in identity.items())
        ):
            raise RunError(f"invalid final receipt: {run_id}")
        if state == "completed" and (
            type(exit_code) is not int
            or exit_code not in manifest.get("success_exit_codes", [])
        ):
            raise RunError(f"completed final has an unsuccessful exit code: {run_id}")
        duration = final.get("duration_seconds")
        if duration is not None and (
            type(duration) not in {int, float}
            or duration < 0
            or duration != resource_usage["wall_seconds"]
        ):
            raise RunError(f"invalid final duration: {run_id}")
        final_host = final.get("host")
        if not isinstance(final_host, str) or not final_host:
            raise RunError(f"invalid final host: {run_id}")
        if "actual_environment_sha256" in final and (
            not isinstance(final["actual_environment_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", final["actual_environment_sha256"])
            is None
        ):
            raise RunError(f"invalid final environment hash: {run_id}")
        if "stop" in final and not isinstance(final["stop"], dict):
            raise RunError(f"invalid final stop record: {run_id}")
        signals = final.get("signal_sequence")
        if signals is not None and (
            not isinstance(signals, list)
            or not signals
            or any(signal_name not in _ALLOWED_SIGNALS for signal_name in signals)
        ):
            raise RunError(f"invalid final signal sequence: {run_id}")
        if "error" in final and (
            not isinstance(final["error"], str) or not final["error"]
        ):
            raise RunError(f"invalid final error: {run_id}")
        for stream in ("stdout", "stderr"):
            content = final.get(stream)
            if (
                not isinstance(content, dict)
                or set(content) != _CONTENT_RECEIPT_FIELDS
                or content.get("path") != f".aros/runs/{run_id}/{stream}.log"
                or type(content.get("bytes")) is not int
                or content["bytes"] < 0
                or not isinstance(content.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", content["sha256"]) is None
            ):
                raise RunError(f"invalid final {stream} receipt: {run_id}")
        if (
            status.get("schema_version") != 1
            or status.get("run_id") != run_id
            or status.get("state") != state
            or status.get("manifest_sha256") != manifest.get("manifest_sha256")
            or status.get("launch_receipt_sha256") != launch_sha256
            or status.get("launched_at") != prelaunch.get("created_at")
            or status.get("finished_at") != finished_at
            or status.get("final_ref") != f"runs/{run_id}/final.json"
        ):
            raise RunError(f"final and status lineage mismatch: {run_id}")
        expected_actor = status.get("actor")
        expected_host = status.get("host")
        if (
            not isinstance(expected_actor, str)
            or not expected_actor
            or not isinstance(expected_host, str)
            or not expected_host
        ):
            raise RunError(f"invalid launch provenance in status: {run_id}")
        _validate_prelaunch_receipt(
            prelaunch,
            manifest,
            expected_actor=expected_actor,
            expected_host=expected_host,
            expected_session=f"aros-{run_id.lower()}",
            expected_invocation=_runner_invocation(self.root, run_id),
            status=status,
        )
        if final_host != expected_host:
            raise RunError(f"final and launch host mismatch: {run_id}")
        return final

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
        status = self._reconcile_locked(run_id)
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
        self._validate_run_id(run_id)
        with file_lock(self._run_lock_path(run_id)):
            return self._reconcile_locked(run_id)

    def _reconcile_locked(self, run_id: str) -> dict[str, object]:
        manifest = self._load_manifest(run_id)
        runtime = self._runtime_path(run_id)
        status = _read_run_status(runtime / "status.json")
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
        status = _read_run_status(runtime / "status.json")
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
                "host": status["host"],
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
            status = _read_run_status(status_path)
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
    return record_sha256(receipt, "receipt_sha256")


def _runner_invocation(root: Path, run_id: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "arbor.aros.runner",
        "--workspace",
        str(root),
        "--run-id",
        run_id,
    ]


def _validate_prelaunch_receipt(
    receipt: dict[str, object],
    manifest: dict[str, object],
    *,
    expected_actor: str,
    expected_host: str,
    expected_session: str,
    expected_invocation: list[str],
    status: dict[str, object] | None = None,
) -> None:
    run_id = str(manifest["run_id"])
    receipt_sha256 = receipt.get("receipt_sha256")
    if (
        set(receipt) != _PRELAUNCH_FIELDS
        or not isinstance(expected_actor, str)
        or not expected_actor
        or not isinstance(expected_host, str)
        or not expected_host
        or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != 1
        or receipt.get("receipt_id") != f"{run_id}-prelaunch"
        or receipt.get("kind") != "run_prelaunch"
        or receipt.get("run_id") != run_id
        or not isinstance(receipt.get("actor"), str)
        or receipt.get("actor") != expected_actor
        or not _valid_utc_timestamp(receipt.get("created_at"))
        or receipt.get("base_commit") != manifest.get("base_commit")
        or receipt.get("manifest_sha256") != manifest.get("manifest_sha256")
        or receipt.get("carrier") != "tmux"
        or receipt.get("tmux_session") != expected_session
        or not isinstance(receipt.get("host"), str)
        or receipt.get("host") != expected_host
        or type(receipt.get("runner_version")) is not int
        or receipt.get("runner_version") != 1
        or receipt.get("runner_invocation") != expected_invocation
        or not isinstance(receipt_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt_sha256) is None
        or receipt_sha256 != _receipt_sha256(receipt)
    ):
        raise RunError(f"invalid prelaunch receipt: {run_id}")
    if status is not None and (
        status.get("schema_version") != 1
        or status.get("run_id") != run_id
        or status.get("manifest_sha256") != manifest.get("manifest_sha256")
        or status.get("actor") != expected_actor
        or status.get("host") != expected_host
        or status.get("carrier") != "tmux"
        or status.get("tmux_session") != expected_session
        or status.get("launch_receipt_sha256") != receipt_sha256
        or status.get("launched_at") != receipt.get("created_at")
    ):
        raise RunError(f"prelaunch and status lineage mismatch: {run_id}")


def _request_sha256(manifest: dict[str, object]) -> str:
    requested = {
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
    if "execution_bundle" in manifest:
        requested.update(
            {
                "repository_ref": manifest.get("repository_ref"),
                "candidate_commit": manifest.get("candidate_commit"),
                "success_exit_codes": manifest.get("success_exit_codes"),
                "execution_bundle": manifest.get("execution_bundle"),
            }
        )
    return _sha256(requested)


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


def _validate_success_exit_codes(value: list[int]) -> list[int]:
    if not isinstance(value, list) or not value:
        raise RunError("success_exit_codes must be a non-empty list of integers")
    if any(type(code) is not int for code in value):
        raise RunError("success_exit_codes must contain only plain integers")
    if len(set(value)) != len(value):
        raise RunError("success_exit_codes must not contain duplicates")
    return list(value)


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


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True


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


def _read_strict_object(path: Path, description: str) -> dict[str, object]:
    try:
        value = read_json_strict(path)
    except (OSError, ValueError) as error:
        raise RunError(f"unable to read {description}: {path}") from error
    if not isinstance(value, dict):
        raise RunError(f"invalid {description}: {path}")
    return value


def _read_run_status(path: Path) -> dict[str, object]:
    first_error: RunError | None = None
    for _attempt in range(3):
        try:
            return _read_object(path, "run status")
        except RunError as error:
            if first_error is None:
                first_error = error
    assert first_error is not None
    raise first_error


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
