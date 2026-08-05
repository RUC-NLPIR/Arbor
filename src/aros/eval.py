"""Visible evaluator registration and one-attempt request admission."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import eval_records as _eval_records
from . import store as _store
from . import worktrees as _worktrees
from .eval_records import (
    build_measurement_receipt,
    parse_scalar_metric,
    parse_visible_manifest,
    validate_measurement_receipt,
)
from .receipts import record_sha256
from .runs import RunService
from .store import (
    AnchoredReadError,
    AnchoredWorkspaceReader,
    create_json,
    file_lock,
    json_sha256,
    read_json_strict,
    read_json_strict_no_repair,
    utc_now,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVAL_ID = re.compile(r"^EVAL-[0-9a-f]{64}$")
_MAX_OBSERVE_BYTES = 65_536
_JsonReader = Callable[[str | Path], object]
_DESCRIPTOR_METADATA_FIELDS = {
    "manifest_ref",
    "manifest_commit",
    "manifest_blob_sha256",
    "apparatus_tree",
    "registration_actor",
    "registered_at",
    "descriptor_sha256",
}
_execution_lease_close_guard = threading.Lock()


class EvalError(ValueError):
    """Raised when an evaluation registration or request is unsafe."""


def read_validated_eval_receipt(
    root: str | Path,
    eval_id: str,
    *,
    reader: _JsonReader = read_json_strict_no_repair,
) -> dict[str, object]:
    """Strictly read one receipt and its immutable Eval-to-Run lineage."""
    evaluation_id = _evaluation_id(eval_id)
    service = EvalService(root)
    if reader is read_json_strict_no_repair:
        try:
            with AnchoredWorkspaceReader(service.root) as anchored:
                anchored.require_git_marker()
                receipt = _read_validated_eval_receipt(
                    service,
                    evaluation_id,
                    anchored,
                )
                _worktrees._validate_repository_binding(service.repository)
                anchored.revalidate()
                return receipt
        except EvalError:
            raise
        except (
            AnchoredReadError,
            OSError,
            _worktrees.WorktreeError,
        ) as error:
            raise EvalError(f"invalid evaluation workspace: {root}") from error
    return _read_validated_eval_receipt(service, evaluation_id, reader)


def _read_validated_eval_receipt(
    service: EvalService,
    evaluation_id: str,
    reader: _JsonReader,
) -> dict[str, object]:
    request = service._load_request(evaluation_id, reader=reader)
    execution = service._load_execution(request, reader=reader)
    receipt = service._load_bound_receipt(
        request,
        execution,
        reader=reader,
    )
    if receipt is None:
        raise EvalError(f"evaluation receipt does not exist: {evaluation_id}")
    run_link = service._load_run_link(request, execution, reader=reader)
    if run_link is None:
        raise EvalError("existing receipt Run link lineage is missing")
    service._validate_receipt_immutable_run_lineage(
        request,
        execution,
        receipt,
        run_link,
        reader=reader,
    )
    return receipt


@dataclass(frozen=True)
class ExistingEvaluation:
    status: dict[str, object]


@dataclass
class ExecutionLease:
    request: dict[str, object]
    execution: dict[str, object]
    lock_fd: int

    def __enter__(self) -> ExecutionLease:
        if self.lock_fd < 0:
            raise RuntimeError("execution lease is already closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with _execution_lease_close_guard:
            if self.lock_fd < 0:
                return
            descriptor = self.lock_fd
            self.lock_fd = -1
        os.close(descriptor)


class EvalService:
    """Freeze visible evaluator registrations in one exact Git workspace."""

    def __init__(self, root: str | Path):
        self.repository = _worktrees.bind_repository(root)
        self.root = self.repository.root

    def register(self, manifest_ref: str, *, actor: str) -> dict[str, object]:
        """Register one tracked manifest and its exact apparatus blobs."""
        reference = _manifest_reference(manifest_ref)
        registration_actor = _required_text(actor, "actor")
        _require_clean_registration(self.repository)
        manifest_commit = _worktrees._git_text(
            self.repository,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )
        manifest_blob = _read_regular_git_blob(
            self.repository,
            manifest_commit,
            reference,
            "manifest",
        )
        try:
            manifest_value = _store._strict_json_loads(manifest_blob)
        except (UnicodeError, ValueError) as error:
            raise EvalError(
                f"visible evaluator manifest must be strict UTF-8 JSON: {error}"
            ) from error
        try:
            manifest = parse_visible_manifest(manifest_value)
        except ValueError as error:
            raise EvalError(f"invalid visible evaluator manifest: {error}") from error
        parts = PurePosixPath(reference).parts
        if (
            manifest["evaluator_id"] != parts[2]
            or manifest["evaluator_version"] != parts[3]
        ):
            raise EvalError("manifest path does not match evaluator identity")

        apparatus_commit = str(manifest["apparatus_commit"])
        resolved_apparatus = _worktrees._git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{apparatus_commit}^{{commit}}",
        )
        if resolved_apparatus != apparatus_commit:
            raise EvalError("apparatus_commit is not an exact Git commit")
        apparatus_tree = _worktrees._git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{apparatus_commit}^{{tree}}",
        )
        for apparatus_path in manifest["apparatus_paths"]:  # type: ignore[union-attr]
            path = str(apparatus_path["path"])
            blob = _read_regular_git_blob(
                self.repository,
                apparatus_commit,
                path,
                "apparatus",
            )
            if hashlib.sha256(blob).hexdigest() != apparatus_path["blob_sha256"]:
                raise EvalError(f"apparatus blob hash mismatch: {path}")
        _require_clean_registration(self.repository)

        descriptor: dict[str, object] = {
            **manifest,
            "manifest_ref": reference,
            "manifest_commit": manifest_commit,
            "manifest_blob_sha256": hashlib.sha256(manifest_blob).hexdigest(),
            "apparatus_tree": apparatus_tree,
            "registration_actor": registration_actor,
            "registered_at": utc_now(),
        }
        descriptor["descriptor_sha256"] = record_sha256(
            descriptor,
            "descriptor_sha256",
        )
        path = (
            self.root
            / ".aros"
            / "evaluators"
            / str(manifest["evaluator_id"])
            / str(manifest["evaluator_version"])
            / "descriptor.json"
        )
        if not create_json(path, descriptor):
            raise EvalError(f"evaluator descriptor already exists: {path}")
        return descriptor

    def run(
        self,
        evaluator_id: str,
        version: str,
        candidate_commit: str,
        *,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, object] | ExistingEvaluation:
        """Execute one visible evaluation through the durable Run service."""
        attempt = self._begin_execution(
            evaluator_id,
            version,
            candidate_commit,
            actor,
            idempotency_key,
        )
        if isinstance(attempt, ExistingEvaluation):
            return attempt
        with attempt as lease:
            request = lease.request
            execution = lease.execution
            descriptor = self._load_descriptor(evaluator_id, version)
            if (
                descriptor["descriptor_sha256"] != request["descriptor_sha256"]
                or descriptor["apparatus_commit"] != request["apparatus_commit"]
            ):
                raise EvalError("evaluator descriptor differs from the frozen request")
            bundle = _worktrees.create_execution_bundle(
                self.repository,
                self.root / ".worktree" / "eval" / str(request["eval_id"]),
                str(request["candidate_commit"]),
                str(request["apparatus_commit"]),
            )
            _worktrees.validate_execution_bundle(self.repository, bundle)
            runs = RunService(self.root)
            resource_limits = descriptor["resource_limits"]
            assert isinstance(resource_limits, dict)
            manifest = runs.prepare_bundle(
                bundle,
                list(descriptor["scorer_argv"]),  # type: ignore[arg-type]
                cwd=str(descriptor["scorer_cwd"]),
                timeout_seconds=resource_limits["timeout_seconds"],  # type: ignore[arg-type]
                success_exit_codes=list(  # type: ignore[arg-type]
                    descriptor["success_exit_codes"]
                ),
                idempotency_key=str(request["eval_id"]),
                actor=str(request["actor"]),
            )
            run_link = self._publish_run_link(request, execution, bundle, manifest)
            try:
                status = runs.start(
                    str(run_link["run_id"]),
                    actor=str(request["actor"]),
                )
            except ValueError:
                status = runs.status(str(run_link["run_id"]))
                if status.get("state") not in {
                    "completed",
                    "failed_process",
                    "timed_out",
                    "cancelled",
                    "lost",
                }:
                    raise
            while status.get("state") in {"launched", "running"}:
                time.sleep(0.02)
                status = runs.status(str(run_link["run_id"]))
            if status.get("state") == "lost":
                return self._linked_lost_evaluation(request, run_link, status)
            return self._publish_visible_receipt(
                request,
                execution,
                descriptor,
                bundle,
                run_link,
                status,
                runs,
            )

    def status(self, eval_id: str) -> dict[str, object]:
        """Return the factual public projection for one visible evaluation."""
        evaluation_id = _evaluation_id(eval_id)
        reader = read_json_strict_no_repair
        request = self._load_request(evaluation_id, reader=reader)
        idempotency_lock = (
            self.root
            / ".aros"
            / "locks"
            / f"eval-idempotency-{request['idempotency_key_sha256']}.lock"
        )
        lock_fd = _acquire_existing_idempotency_lock(idempotency_lock)
        try:
            state = self._existing_evaluation(
                request,
                reconcile_run=False,
                reader=reader,
            ).status
        finally:
            os.close(lock_fd)
        if state.get("evaluation_state") == "completed":
            return {
                "eval_id": evaluation_id,
                "evaluation_state": state["evaluation_state"],
                "referenced_process_state": state["referenced_process_state"],
                "measurement_state": state["measurement_state"],
                "run_id": state["run_id"],
                "receipt_ref": f"eval/evaluations/{evaluation_id}/receipt.json",
                "reason": None,
                "updated_at": state["finished_at"],
            }
        return {
            field: state[field]
            for field in (
                "eval_id",
                "evaluation_state",
                "referenced_process_state",
                "measurement_state",
                "run_id",
                "receipt_ref",
                "reason",
                "updated_at",
            )
        }

    def observe(
        self,
        eval_id: str,
        *,
        stream: str,
        max_bytes: int = _MAX_OBSERVE_BYTES,
    ) -> str:
        """Return one bounded strict-UTF-8 tail from a linked visible Run."""
        evaluation_id = _evaluation_id(eval_id)
        reader = read_json_strict_no_repair
        if stream not in {"stdout", "stderr"}:
            raise EvalError("stream must be 'stdout' or 'stderr'")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= _MAX_OBSERVE_BYTES
        ):
            raise EvalError("max_bytes must be a plain integer from 1 through 65536")
        request = self._load_request(evaluation_id, reader=reader)
        descriptor = self._load_descriptor(
            str(request["evaluator_id"]),
            str(request["evaluator_version"]),
            reader=reader,
        )
        if (
            descriptor.get("visibility") != "visible"
            or descriptor.get("descriptor_sha256")
            != request["descriptor_sha256"]
        ):
            raise EvalError("evaluation is not bound to a visible evaluator")
        execution = self._load_execution(request, reader=reader)
        run_link = self._load_run_link(request, execution, reader=reader)
        if run_link is None:
            raise EvalError("evaluation has no linked visible Run")
        self._linked_run_status(
            request,
            run_link,
            reconcile=False,
            reader=reader,
        )
        try:
            raw = RunService(self.root)._tail_bytes(
                str(run_link["run_id"]),
                stream=stream,
                max_bytes=max_bytes,
            )
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EvalError(f"visible {stream} is not strict UTF-8") from error
        except (OSError, ValueError) as error:
            raise EvalError(f"unable to observe visible {stream}") from error

    def audit(self, eval_id: str) -> dict[str, object]:
        """Validate one visible evaluation lineage without changing it."""
        evaluation_id = _evaluation_id(eval_id)
        reader = read_json_strict_no_repair
        checked_refs: list[str] = []
        issues: list[str] = []

        request_ref = f".aros/evaluations/{evaluation_id}/request.json"
        checked_refs.append(request_ref)
        try:
            request = self._load_request(evaluation_id, reader=reader)
        except EvalError as error:
            issues.append(f"{request_ref}: {error}")
            return _audit_projection(evaluation_id, checked_refs, issues)

        descriptor_ref = (
            ".aros/evaluators/"
            f"{request['evaluator_id']}/{request['evaluator_version']}/descriptor.json"
        )
        checked_refs.append(descriptor_ref)
        try:
            descriptor = self._load_descriptor(
                str(request["evaluator_id"]),
                str(request["evaluator_version"]),
                reader=reader,
            )
            self._validate_descriptor_lineage(descriptor, request)
        except (OSError, ValueError) as error:
            issues.append(f"{descriptor_ref}: {error}")

        execution_ref = f".aros/evaluations/{evaluation_id}/execution.json"
        checked_refs.append(execution_ref)
        try:
            execution = self._load_execution(request, reader=reader)
        except EvalError as error:
            issues.append(f"{execution_ref}: {error}")
            return _audit_projection(evaluation_id, checked_refs, issues)

        run_link_ref = f".aros/evaluations/{evaluation_id}/run.json"
        checked_refs.append(run_link_ref)
        try:
            run_link = self._load_run_link(request, execution, reader=reader)
        except EvalError as error:
            issues.append(f"{run_link_ref}: {error}")
            return _audit_projection(evaluation_id, checked_refs, issues)
        if run_link is None:
            issues.append(f"{run_link_ref}: evaluation Run link is missing")
            return _audit_projection(evaluation_id, checked_refs, issues)

        run_id = str(run_link["run_id"])
        manifest_ref = f"runs/{run_id}/manifest.json"
        status_ref = f".aros/runs/{run_id}/status.json"
        checked_refs.extend((manifest_ref, status_ref))
        try:
            manifest_value = reader(self.root / manifest_ref)
            if not isinstance(manifest_value, dict):
                raise ValueError("Run manifest is not an object")
            bundle = manifest_value.get("execution_bundle")
            if not isinstance(bundle, dict):
                raise ValueError("Run execution bundle is missing")
            portable = {
                "candidate": bundle.get("candidate"),
                "apparatus": bundle.get("apparatus"),
                "temp": bundle.get("temp"),
            }
            if (
                bundle.get("bundle_sha256") != run_link["bundle_sha256"]
                or json_sha256(portable) != run_link["bundle_sha256"]
            ):
                raise ValueError("Run execution bundle hash mismatch")
        except (OSError, ValueError) as error:
            issues.append(f"{manifest_ref}: {error}")
        runs = RunService(self.root)
        try:
            observed_status = runs.status(
                run_id,
                reconcile=False,
                reader=reader,
            )
        except (OSError, ValueError) as error:
            issues.append(f"{status_ref}: {error}")
            observed_status = None
        try:
            self._linked_run_status(
                request,
                run_link,
                reconcile=False,
                reader=reader,
            )
        except EvalError as error:
            issues.append(f"{manifest_ref} or {status_ref}: {error}")

        terminal_state = observed_status is not None and observed_status.get("state") in {
            "completed",
            "failed_process",
            "timed_out",
            "cancelled",
        }
        final_ref = f"runs/{run_id}/final.json"
        try:
            reader(self.root / final_ref)
        except FileNotFoundError:
            final_exists = False
        except (OSError, ValueError):
            final_exists = True
        else:
            final_exists = True
        if final_exists:
            prelaunch_ref = f".aros/receipts/{run_id}-prelaunch.json"
            checked_refs.extend((prelaunch_ref, final_ref))
            try:
                final_value = runs.read_validated_final(
                    run_id,
                    reader=reader,
                )
            except (OSError, ValueError) as error:
                issues.append(f"{final_ref}: {error}")
                final_value = None
            if final_value is not None and observed_status is not None:
                if observed_status.get("state") != final_value.get("state"):
                    issues.append(
                        f"{status_ref} and {final_ref}: state mismatch "
                        f"({observed_status.get('state')} != {final_value.get('state')})"
                    )
                if observed_status.get("final_ref") != final_ref:
                    issues.append(
                        f"{status_ref} and {final_ref}: final reference mismatch"
                    )
            for stream in ("stdout", "stderr"):
                log_ref = f".aros/runs/{run_id}/{stream}.log"
                checked_refs.append(log_ref)
                try:
                    runs.verify_output(
                        run_id,
                        stream,
                        reader=reader,
                    )
                except (OSError, ValueError) as error:
                    issues.append(f"{log_ref}: {error}")
        elif terminal_state:
            checked_refs.append(final_ref)
            issues.append(f"{final_ref}: terminal Run final is missing")

        receipt_ref = f"eval/evaluations/{evaluation_id}/receipt.json"
        checked_refs.append(receipt_ref)
        try:
            self._load_receipt(
                request,
                execution,
                reconcile_run=False,
                reader=reader,
            )
        except EvalError as error:
            issues.append(f"{receipt_ref}: {error}")
        return _audit_projection(evaluation_id, checked_refs, issues)

    def _linked_lost_evaluation(
        self,
        request: dict[str, object],
        run_link: dict[str, object],
        run_status: dict[str, object],
        reason: str | None = None,
    ) -> ExistingEvaluation:
        run_reason = run_status.get("reason")
        updated_at = run_status.get("updated_at")
        return ExistingEvaluation(
            {
                "eval_id": request["eval_id"],
                "evaluation_state": "lost",
                "referenced_process_state": run_status.get("state"),
                "measurement_state": "not_available",
                "run_id": run_link["run_id"],
                "receipt_ref": None,
                "reason": reason
                or (run_reason if isinstance(run_reason, str) else "execution was lost"),
                "updated_at": updated_at if isinstance(updated_at, str) else utc_now(),
            }
        )

    def _publish_run_link(
        self,
        request: dict[str, object],
        execution: dict[str, object],
        bundle: _worktrees.ExecutionBundle,
        manifest: dict[str, object],
    ) -> dict[str, object]:
        run_link: dict[str, object] = {
            "schema_version": 1,
            "eval_id": request["eval_id"],
            "request_sha256": request["request_sha256"],
            "execution_sha256": execution["execution_sha256"],
            "run_id": manifest["run_id"],
            "run_manifest_sha256": manifest["manifest_sha256"],
            "bundle_sha256": bundle.bundle_sha256,
            "candidate_commit": request["candidate_commit"],
            "apparatus_commit": request["apparatus_commit"],
            "linked_at": utc_now(),
        }
        run_link["run_link_sha256"] = record_sha256(
            run_link,
            "run_link_sha256",
        )
        path = (
            self.root
            / ".aros"
            / "evaluations"
            / str(request["eval_id"])
            / "run.json"
        )
        if not create_json(path, run_link):
            raise EvalError("evaluation Run link already exists")
        try:
            return dict(
                _eval_records._validate_run_link(run_link, request, execution)
            )
        except ValueError as error:
            raise EvalError("evaluation Run link is invalid") from error

    def _publish_visible_receipt(
        self,
        request: dict[str, object],
        execution: dict[str, object],
        descriptor: dict[str, object],
        bundle: _worktrees.ExecutionBundle,
        run_link: dict[str, object],
        status: dict[str, object],
        runs: RunService,
    ) -> dict[str, object]:
        run_id = str(run_link["run_id"])
        expected_final_ref = f"runs/{run_id}/final.json"
        process_state = status.get("state")
        if process_state not in {
            "completed",
            "failed_process",
            "timed_out",
            "cancelled",
        } or status.get("final_ref") != expected_final_ref:
            raise EvalError("visible evaluation Run is not terminal")
        try:
            final_value = runs.read_validated_final(run_id)
        except ValueError as error:
            raise EvalError("visible evaluation Run final is invalid") from error
        if final_value.get("state") != process_state:
            raise EvalError("visible evaluation Run final state differs from status")
        output_valid = True
        try:
            stdout = runs.read_verified_output(run_id, "stdout")
            stderr = runs.read_verified_output(run_id, "stderr")
        except ValueError:
            stdout = stderr = b""
            output_valid = False
        if output_valid:
            for stream, raw in (("stdout", stdout), ("stderr", stderr)):
                content = final_value.get(stream)
                if (
                    not isinstance(content, dict)
                    or set(content) != {"path", "bytes", "sha256"}
                    or content.get("path") != f".aros/runs/{run_id}/{stream}.log"
                    or content.get("bytes") != len(raw)
                    or content.get("sha256") != hashlib.sha256(raw).hexdigest()
                ):
                    output_valid = False
                    break
        metric_contract = descriptor["metric_output"]
        assert isinstance(metric_contract, dict)
        cleanup_state = "preserved"
        try:
            _worktrees.validate_execution_bundle(self.repository, bundle)
        except _worktrees.WorktreeError:
            pass
        else:
            try:
                removed = _worktrees.remove_clean_execution_bundle(
                    self.repository,
                    bundle,
                )
            except _worktrees.BundleRemovalError as error:
                if error.removed:
                    raise
            else:
                if removed:
                    cleanup_state = "removed"
        measurement = _derive_visible_measurement(
            str(process_state),
            metric_contract,
            stdout,
            output_valid=output_valid,
            cleanup_state=cleanup_state,
        )
        try:
            receipt = build_measurement_receipt(
                request,
                execution,
                run_link,
                final_value,
                str(measurement["measurement_state"]),
                measurement,
                cleanup_state,
            )
        except ValueError as error:
            raise EvalError("visible evaluation receipt is invalid") from error
        path = self._receipt_path(str(request["eval_id"]))
        if not create_json(path, receipt):
            raise EvalError("visible evaluation receipt already exists")
        return receipt

    def _publish_request(
        self,
        evaluator_id: str,
        version: str,
        candidate_commit: str,
        actor: str,
        idempotency_key: str,
    ) -> tuple[dict[str, object], bool]:
        """Create one immutable evaluation request without claiming execution."""
        evaluator = _identifier(evaluator_id, "evaluator_id")
        evaluator_version = _identifier(version, "evaluator_version")
        request_actor = _required_text(actor, "actor")
        key = _required_text(idempotency_key, "idempotency_key")
        candidate_literal = _commit_literal(candidate_commit, "candidate")
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        eval_id = f"EVAL-{key_digest}"
        path = self.root / ".aros" / "evaluations" / eval_id / "request.json"
        identity = {
            "eval_id": eval_id,
            "evaluator_id": evaluator,
            "evaluator_version": evaluator_version,
            "candidate_commit": candidate_literal,
            "actor": request_actor,
            "idempotency_key_sha256": key_digest,
        }
        existing = self._load_existing_request(path, identity)
        if existing is not None:
            return existing, False

        candidate = _resolve_exact_commit(
            self.repository,
            candidate_literal,
            "candidate",
        )
        descriptor = self._load_descriptor(evaluator, evaluator_version)
        request: dict[str, object] = {
            "schema_version": 1,
            **identity,
            "descriptor_sha256": descriptor["descriptor_sha256"],
            "candidate_commit": candidate,
            "apparatus_commit": descriptor["apparatus_commit"],
            "created_at": utc_now(),
        }
        request["request_sha256"] = record_sha256(request, "request_sha256")
        if create_json(path, request):
            return request, True
        existing = self._load_existing_request(path, identity)
        if existing is None:
            raise EvalError(f"evaluation request disappeared after create conflict: {path}")
        return existing, False

    def _load_existing_request(
        self,
        path: Path,
        identity: dict[str, object],
    ) -> dict[str, object] | None:
        try:
            existing_value = read_json_strict(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise EvalError(f"invalid existing evaluation request: {path}") from error
        try:
            existing = _eval_records._validate_request(existing_value)
        except ValueError as error:
            raise EvalError(f"invalid existing evaluation request: {path}") from error
        if any(existing[field] != value for field, value in identity.items()):
            raise EvalError("idempotency key belongs to a different request")
        return dict(existing)

    def _load_request(
        self,
        eval_id: str,
        *,
        reader: _JsonReader | None = None,
    ) -> dict[str, object]:
        path = self.root / ".aros" / "evaluations" / eval_id / "request.json"
        try:
            value = _read_json_authority(path, reader)
            request = _eval_records._validate_request(value)
        except FileNotFoundError as error:
            raise EvalError(f"evaluation request does not exist: {eval_id}") from error
        except (OSError, ValueError) as error:
            raise EvalError(f"evaluation request is invalid: {eval_id}") from error
        if request["eval_id"] != eval_id:
            raise EvalError("evaluation request identity mismatch")
        return dict(request)

    def _load_execution(
        self,
        request: dict[str, object],
        *,
        reader: _JsonReader | None = None,
    ) -> dict[str, object]:
        eval_id = str(request["eval_id"])
        try:
            value = _read_json_authority(self._execution_path(eval_id), reader)
            execution = _eval_records._validate_execution(value, request)
        except FileNotFoundError as error:
            raise EvalError(f"evaluation execution does not exist: {eval_id}") from error
        except (OSError, ValueError) as error:
            raise EvalError(f"evaluation execution is invalid: {eval_id}") from error
        return dict(execution)

    def _begin_execution(
        self,
        evaluator_id: str,
        version: str,
        candidate_commit: str,
        actor: str,
        idempotency_key: str,
    ) -> ExecutionLease | ExistingEvaluation:
        """Publish and hold the sole local execution claim for one request."""
        _require_linux_claim_runtime()
        key = _required_text(idempotency_key, "idempotency_key")
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        idempotency_lock = (
            self.root
            / ".aros"
            / "locks"
            / f"eval-idempotency-{key_digest}.lock"
        )
        with file_lock(idempotency_lock):
            request, created = self._publish_request(
                evaluator_id,
                version,
                candidate_commit,
                actor,
                key,
            )
            if not created:
                return self._existing_evaluation(request)
            execution_lock = self._execution_lock_path(str(request["eval_id"]))
            lock_fd = _acquire_execution_lock(execution_lock)
            if lock_fd is None:
                raise EvalError("new evaluation request could not acquire execution lock")
            try:
                broker_pid = os.getpid()
                start_token = _linux_process_start_token(broker_pid)
                if start_token is None:
                    raise EvalError("unable to bind local evaluation broker identity")
                execution: dict[str, object] = {
                    "schema_version": 1,
                    "eval_id": request["eval_id"],
                    "request_sha256": request["request_sha256"],
                    "host": _required_text(socket.gethostname(), "host"),
                    "broker_pid": broker_pid,
                    "broker_start_token": start_token,
                    "claimed_at": utc_now(),
                }
                execution["execution_sha256"] = record_sha256(
                    execution,
                    "execution_sha256",
                )
                execution_path = self._execution_path(str(request["eval_id"]))
                if not create_json(execution_path, execution):
                    raise EvalError("evaluation execution claim already exists")
                _eval_records._validate_execution(execution, request)
                return ExecutionLease(dict(request), execution, lock_fd)
            except BaseException:
                _release_execution_lock(lock_fd)
                raise

    def _existing_evaluation(
        self,
        request: dict[str, object],
        *,
        reconcile_run: bool = True,
        reader: _JsonReader | None = None,
    ) -> ExistingEvaluation:
        receipt = self._load_receipt(
            request,
            None,
            reconcile_run=reconcile_run,
            reader=reader,
        )
        execution_path = self._execution_path(str(request["eval_id"]))
        try:
            execution_value = _read_json_authority(execution_path, reader)
            execution = _eval_records._validate_execution(execution_value, request)
        except FileNotFoundError:
            if receipt is not None:
                raise EvalError("existing receipt execution lineage is missing")
            return self._receipt_or_lost(
                request,
                None,
                "request has no execution claim",
                reconcile_run=reconcile_run,
                reader=reader,
            )
        except (OSError, ValueError) as error:
            raise EvalError("existing evaluation execution claim is invalid") from error
        if receipt is not None:
            if receipt["execution_sha256"] != execution["execution_sha256"]:
                raise EvalError("existing receipt execution lineage mismatch")
            self._validate_receipt_run_lineage(
                request,
                execution,
                receipt,
                reconcile_run=reconcile_run,
                reader=reader,
            )
            return ExistingEvaluation(receipt)
        if execution["host"] != socket.gethostname():
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim host is not local",
                reconcile_run=reconcile_run,
                reader=reader,
            )
        lock_state, lock_fd, lock_identity = _observe_execution_lock(
            self._execution_lock_path(str(request["eval_id"]))
        )
        if lock_state == "acquired":
            assert lock_fd is not None
            try:
                return self._receipt_or_lost(
                    request,
                    execution,
                    "execution claim lock was released",
                    reconcile_run=reconcile_run,
                    reader=reader,
                )
            finally:
                _release_execution_lock(lock_fd)
        if lock_state == "missing":
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim lock was released",
                reconcile_run=reconcile_run,
                reader=reader,
            )
        assert lock_identity is not None
        broker_pid = int(execution["broker_pid"])
        if _linux_process_start_token(broker_pid) != execution["broker_start_token"]:
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim broker is not live",
                reconcile_run=reconcile_run,
                reader=reader,
            )
        if not _linux_broker_owns_lock(execution, lock_identity):
            return self._receipt_or_lost(
                request,
                execution,
                "execution claim broker does not own the lock",
                reconcile_run=reconcile_run,
                reader=reader,
            )
        receipt = self._load_receipt(
            request,
            execution,
            reconcile_run=reconcile_run,
            reader=reader,
        )
        if receipt is not None:
            return ExistingEvaluation(receipt)
        run_link = self._load_run_link(request, execution, reader=reader)
        if run_link is not None:
            run_status = self._linked_run_status(
                request,
                run_link,
                reconcile=reconcile_run,
                reader=reader,
            )
            referenced_state = run_status.get("state")
            evaluation_state = (
                "finalizing"
                if referenced_state
                in {"completed", "failed_process", "timed_out", "cancelled"}
                else "running"
            )
            return ExistingEvaluation(
                {
                    "eval_id": request["eval_id"],
                    "evaluation_state": evaluation_state,
                    "referenced_process_state": referenced_state,
                    "measurement_state": "not_available",
                    "run_id": run_link["run_id"],
                    "receipt_ref": None,
                    "reason": "execution claim is live",
                    "updated_at": run_status.get("updated_at", execution["claimed_at"]),
                }
            )
        return ExistingEvaluation(
            {
                "eval_id": request["eval_id"],
                "evaluation_state": "running",
                "referenced_process_state": "prepared",
                "measurement_state": "not_available",
                "run_id": None,
                "receipt_ref": None,
                "reason": "execution claim is live",
                "updated_at": execution["claimed_at"],
            }
        )

    def _load_receipt(
        self,
        request: dict[str, object],
        execution: dict[str, object] | None,
        *,
        reconcile_run: bool = True,
        reader: _JsonReader | None = None,
    ) -> dict[str, object] | None:
        receipt = self._load_bound_receipt(
            request,
            execution,
            reader=reader,
        )
        if receipt is not None and execution is not None:
            self._validate_receipt_run_lineage(
                request,
                execution,
                receipt,
                reconcile_run=reconcile_run,
                reader=reader,
            )
        return receipt

    def _load_bound_receipt(
        self,
        request: dict[str, object],
        execution: dict[str, object] | None,
        *,
        reader: _JsonReader | None = None,
    ) -> dict[str, object] | None:
        receipt_path = self._receipt_path(str(request["eval_id"]))
        try:
            receipt_value = _read_json_authority(receipt_path, reader)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise EvalError("existing evaluation receipt is unreadable") from error
        except ValueError as error:
            raise EvalError("existing evaluation receipt is invalid") from error
        try:
            receipt = validate_measurement_receipt(receipt_value)
        except ValueError as error:
            raise EvalError("existing evaluation receipt is invalid") from error
        if any(
            receipt[field] != request[field]
            for field in (
                "eval_id",
                "descriptor_sha256",
                "request_sha256",
                "candidate_commit",
                "apparatus_commit",
            )
        ):
            raise EvalError("existing evaluation receipt lineage is invalid")
        if (
            execution is not None
            and receipt["execution_sha256"] != execution["execution_sha256"]
        ):
            raise EvalError("existing receipt execution lineage mismatch")
        return receipt

    def _receipt_or_lost(
        self,
        request: dict[str, object],
        execution: dict[str, object] | None,
        reason: str,
        *,
        reconcile_run: bool = True,
        reader: _JsonReader | None = None,
    ) -> ExistingEvaluation:
        receipt = self._load_receipt(
            request,
            execution,
            reconcile_run=reconcile_run,
            reader=reader,
        )
        if receipt is not None:
            if execution is None:
                raise EvalError("existing receipt execution lineage is missing")
            return ExistingEvaluation(receipt)
        if execution is not None:
            run_link = self._load_run_link(request, execution, reader=reader)
            if run_link is not None:
                run_status = self._linked_run_status(
                    request,
                    run_link,
                    reconcile=reconcile_run,
                    reader=reader,
                )
                return self._linked_lost_evaluation(
                    request,
                    run_link,
                    run_status,
                    reason,
                )
        return self._lost_evaluation(request, reason)

    def _load_run_link(
        self,
        request: dict[str, object],
        execution: dict[str, object],
        *,
        reader: _JsonReader | None = None,
    ) -> dict[str, object] | None:
        path = (
            self.root
            / ".aros"
            / "evaluations"
            / str(request["eval_id"])
            / "run.json"
        )
        try:
            value = _read_json_authority(path, reader)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise EvalError("existing evaluation Run link is invalid") from error
        try:
            return dict(_eval_records._validate_run_link(value, request, execution))
        except ValueError as error:
            raise EvalError("existing evaluation Run link is invalid") from error

    def _validate_receipt_run_lineage(
        self,
        request: dict[str, object],
        execution: dict[str, object],
        receipt: dict[str, object],
        *,
        reconcile_run: bool = True,
        reader: _JsonReader | None = None,
    ) -> None:
        run_link = self._load_run_link(request, execution, reader=reader)
        if run_link is None:
            raise EvalError("existing receipt Run link lineage is missing")
        self._linked_run_status(
            request,
            run_link,
            reconcile=reconcile_run,
            reader=reader,
        )
        self._validate_receipt_immutable_run_lineage(
            request,
            execution,
            receipt,
            run_link,
            reader=reader,
        )

    def _validate_receipt_immutable_run_lineage(
        self,
        request: dict[str, object],
        execution: dict[str, object],
        receipt: dict[str, object],
        run_link: dict[str, object],
        *,
        reader: _JsonReader | None = None,
    ) -> None:
        run_id = str(run_link["run_id"])
        try:
            runs = RunService(self.root)
            descriptor = self._load_descriptor(
                str(request["evaluator_id"]),
                str(request["evaluator_version"]),
                reader=reader,
            )
            self._validate_descriptor_lineage(descriptor, request)
            manifest_value = _read_json_authority(
                self.root / "runs" / run_id / "manifest.json",
                reader,
            )
            final_value = runs.read_validated_final(
                run_id,
                reader=reader,
            )
            output_valid = True
            try:
                stdout = runs.read_verified_output(run_id, "stdout", reader=reader)
                runs.read_verified_output(run_id, "stderr", reader=reader)
            except ValueError:
                runs.verify_output(run_id, "stdout", reader=reader)
                runs.verify_output(run_id, "stderr", reader=reader)
                stdout = b""
                output_valid = False
        except (OSError, ValueError) as error:
            raise EvalError("existing receipt Run final lineage is invalid") from error
        if not isinstance(manifest_value, dict):
            raise EvalError("linked Run manifest is invalid")
        self._validate_linked_run_manifest(request, run_link, manifest_value)
        metric_contract = descriptor["metric_output"]
        assert isinstance(metric_contract, dict)
        measurement = _derive_visible_measurement(
            str(final_value["state"]),
            metric_contract,
            stdout,
            output_valid=output_valid,
            cleanup_state=str(receipt["bundle_cleanup_state"]),
        )
        try:
            rebuilt = build_measurement_receipt(
                request,
                execution,
                run_link,
                final_value,
                str(measurement["measurement_state"]),
                measurement,
                str(receipt["bundle_cleanup_state"]),
            )
        except ValueError as error:
            raise EvalError("existing receipt Run lineage is invalid") from error
        if rebuilt != receipt:
            raise EvalError("existing receipt Run lineage is invalid")

    def _linked_run_status(
        self,
        request: dict[str, object],
        run_link: dict[str, object],
        *,
        reconcile: bool = True,
        reader: _JsonReader | None = None,
    ) -> dict[str, object]:
        run_id = str(run_link["run_id"])
        try:
            runs = RunService(self.root)
            status = runs.status(
                run_id,
                reconcile=reconcile,
                reader=reader,
            )
            manifest_value = _read_json_authority(
                self.root / "runs" / run_id / "manifest.json",
                reader,
            )
        except (OSError, ValueError) as error:
            raise EvalError("linked Run lineage is invalid") from error
        if not isinstance(manifest_value, dict):
            raise EvalError("linked Run manifest is invalid")
        self._validate_linked_run_manifest(request, run_link, manifest_value)
        if (
            status.get("run_id") != run_id
            or status.get("manifest_sha256") != run_link["run_manifest_sha256"]
        ):
            raise EvalError("linked Run lineage is invalid")
        state = status.get("state")
        allowed_states = {
            "prepared",
            "launched",
            "running",
            "completed",
            "failed_process",
            "timed_out",
            "cancelled",
            "lost",
        }
        if state not in allowed_states:
            raise EvalError("linked Run state is invalid")
        if state in {"completed", "failed_process", "timed_out", "cancelled"}:
            if status.get("final_ref") != f"runs/{run_id}/final.json":
                raise EvalError("linked terminal Run final reference is invalid")
            try:
                final = runs.read_validated_final(run_id, reader=reader)
            except ValueError as error:
                raise EvalError("linked terminal Run final is invalid") from error
            if final.get("state") != state:
                raise EvalError("linked Run status and final state differ")
        return status

    def _validate_linked_run_manifest(
        self,
        request: dict[str, object],
        run_link: dict[str, object],
        manifest_value: dict[str, object],
    ) -> None:
        bundle = manifest_value.get("execution_bundle")
        if not isinstance(bundle, dict):
            raise EvalError("linked Run bundle lineage is invalid")
        portable = {
            "candidate": bundle.get("candidate"),
            "apparatus": bundle.get("apparatus"),
            "temp": bundle.get("temp"),
        }
        candidate = bundle.get("candidate")
        apparatus = bundle.get("apparatus")
        if (
            manifest_value.get("manifest_sha256")
            != run_link["run_manifest_sha256"]
            or manifest_value.get("repository_ref")
            != f".worktree/eval/{request['eval_id']}"
            or manifest_value.get("candidate_commit")
            != run_link["candidate_commit"]
            or set(bundle) != {
                "candidate",
                "apparatus",
                "temp",
                "bundle_sha256",
            }
            or not isinstance(candidate, dict)
            or not isinstance(apparatus, dict)
            or set(candidate) != {"path", "commit", "tree"}
            or set(apparatus) != {"path", "commit", "tree"}
            or candidate.get("path") != "candidate"
            or apparatus.get("path") != "apparatus"
            or candidate.get("commit") != run_link["candidate_commit"]
            or apparatus.get("commit") != run_link["apparatus_commit"]
            or not _is_commit(candidate.get("tree"))
            or not _is_commit(apparatus.get("tree"))
            or bundle.get("temp") != "tmp"
            or bundle.get("bundle_sha256") != run_link["bundle_sha256"]
            or json_sha256(portable) != run_link["bundle_sha256"]
        ):
            raise EvalError("linked Run lineage is invalid")

    def _lost_evaluation(
        self,
        request: dict[str, object],
        reason: str,
    ) -> ExistingEvaluation:
        return ExistingEvaluation(
            {
                "eval_id": request["eval_id"],
                "evaluation_state": "lost",
                "referenced_process_state": "lost",
                "measurement_state": "not_available",
                "run_id": None,
                "receipt_ref": None,
                "reason": reason,
                "updated_at": utc_now(),
            }
        )

    def _execution_path(self, eval_id: str) -> Path:
        return self.root / ".aros" / "evaluations" / eval_id / "execution.json"

    def _receipt_path(self, eval_id: str) -> Path:
        return self.root / "eval" / "evaluations" / eval_id / "receipt.json"

    def _execution_lock_path(self, eval_id: str) -> Path:
        return self.root / ".aros" / "locks" / f"{eval_id}-execution.lock"

    def _load_descriptor(
        self,
        evaluator_id: str,
        version: str,
        *,
        reader: _JsonReader | None = None,
    ) -> dict[str, object]:
        path = (
            self.root
            / ".aros"
            / "evaluators"
            / evaluator_id
            / version
            / "descriptor.json"
        )
        try:
            value = _read_json_authority(path, reader)
        except (OSError, ValueError) as error:
            raise EvalError(f"unable to load evaluator descriptor: {path}") from error
        if type(value) is not dict or not _DESCRIPTOR_METADATA_FIELDS.issubset(value):
            raise EvalError("evaluator descriptor has invalid fields")
        manifest_value = {
            field: item
            for field, item in value.items()
            if field not in _DESCRIPTOR_METADATA_FIELDS
        }
        try:
            manifest = parse_visible_manifest(manifest_value)
        except ValueError as error:
            raise EvalError(f"invalid evaluator descriptor manifest: {error}") from error
        if (
            manifest["evaluator_id"] != evaluator_id
            or manifest["evaluator_version"] != version
            or value["manifest_ref"]
            != f"eval/suites/{evaluator_id}/{version}/manifest.json"
            or not _is_commit(value["manifest_commit"])
            or not _is_hash(value["manifest_blob_sha256"])
            or not _is_commit(value["apparatus_tree"])
            or not isinstance(value["registration_actor"], str)
            or not value["registration_actor"]
            or not isinstance(value["registered_at"], str)
            or not _is_hash(value["descriptor_sha256"])
            or value["descriptor_sha256"]
            != record_sha256(value, "descriptor_sha256")
        ):
            raise EvalError("evaluator descriptor binding is invalid")
        return dict(value)

    def _validate_descriptor_lineage(
        self,
        descriptor: dict[str, object],
        request: dict[str, object],
    ) -> None:
        if (
            descriptor["descriptor_sha256"] != request["descriptor_sha256"]
            or descriptor["apparatus_commit"] != request["apparatus_commit"]
        ):
            raise EvalError("evaluator descriptor differs from the frozen request")
        manifest_ref = str(descriptor["manifest_ref"])
        manifest_commit = str(descriptor["manifest_commit"])
        manifest_blob = _read_regular_git_blob(
            self.repository,
            manifest_commit,
            manifest_ref,
            "manifest",
        )
        if hashlib.sha256(manifest_blob).hexdigest() != descriptor["manifest_blob_sha256"]:
            raise EvalError("evaluator manifest blob hash mismatch")
        try:
            manifest_value = _store._strict_json_loads(manifest_blob)
            manifest = parse_visible_manifest(manifest_value)
        except (UnicodeError, ValueError) as error:
            raise EvalError("evaluator manifest blob is invalid") from error
        descriptor_manifest = {
            field: item
            for field, item in descriptor.items()
            if field not in _DESCRIPTOR_METADATA_FIELDS
        }
        if manifest != descriptor_manifest:
            raise EvalError("evaluator descriptor differs from its manifest blob")
        apparatus_commit = str(descriptor["apparatus_commit"])
        apparatus_tree = _worktrees._git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{apparatus_commit}^{{tree}}",
        )
        if apparatus_tree != descriptor["apparatus_tree"]:
            raise EvalError("evaluator apparatus tree mismatch")
        for entry in descriptor["apparatus_paths"]:  # type: ignore[union-attr]
            path = str(entry["path"])
            blob = _read_regular_git_blob(
                self.repository,
                apparatus_commit,
                path,
                "apparatus",
            )
            if hashlib.sha256(blob).hexdigest() != entry["blob_sha256"]:
                raise EvalError(f"evaluator apparatus blob hash mismatch: {path}")


def _derive_visible_measurement(
    process_state: str,
    metric_contract: dict[str, object],
    stdout: bytes,
    *,
    output_valid: bool,
    cleanup_state: str,
) -> dict[str, object]:
    unavailable_state = (
        "invalid_eval" if process_state == "completed" else "not_available"
    )
    if (
        process_state == "completed"
        and output_valid
        and cleanup_state == "removed"
    ):
        try:
            return parse_scalar_metric(stdout, metric_contract)
        except ValueError:
            pass
    return {
        "measurement_state": unavailable_state,
        "metric": None,
        "sample_count": None,
        "metric_name": metric_contract["metric_name"],
        "parser": metric_contract["parser"],
    }


def _manifest_reference(value: object) -> str:
    if not isinstance(value, str):
        raise EvalError("manifest_ref must be a string")
    path = PurePosixPath(value)
    if (
        "\x00" in value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) != 5
        or path.parts[:2] != ("eval", "suites")
        or path.parts[4] != "manifest.json"
    ):
        raise EvalError(
            "manifest_ref must be eval/suites/<evaluator-id>/<version>/manifest.json"
        )
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvalError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EvalError(f"{field} must be valid UTF-8") from error
    return value


def _identifier(value: object, field: str) -> str:
    text = _required_text(value, field)
    if _IDENTIFIER.fullmatch(text) is None:
        raise EvalError(f"{field} must be a safe path component")
    return text


def _evaluation_id(value: object) -> str:
    if not isinstance(value, str) or _EVAL_ID.fullmatch(value) is None:
        raise EvalError("eval_id must be EVAL- followed by 64 lowercase hex digits")
    return value


def _audit_projection(
    eval_id: str,
    checked_refs: list[str],
    issues: list[str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "eval_id": eval_id,
        "valid": not issues,
        "checked_refs": checked_refs,
        "issues": issues,
    }


def _read_json_authority(
    path: str | Path,
    reader: _JsonReader | None,
) -> object:
    return read_json_strict(path) if reader is None else reader(path)


def _resolve_exact_commit(
    repo: _worktrees.RepositoryBinding,
    value: object,
    description: str,
) -> str:
    commit = _commit_literal(value, description)
    try:
        resolved = _worktrees._git_text(
            repo,
            "rev-parse",
            "--verify",
            f"{commit}^{{commit}}",
        )
    except _worktrees.WorktreeError as error:
        raise EvalError(f"{description}_commit is unavailable") from error
    if resolved != commit:
        raise EvalError(f"{description}_commit is not exact")
    return commit


def _commit_literal(value: object, description: str) -> str:
    if not _is_commit(value):
        raise EvalError(f"{description}_commit must be a full lowercase Git commit")
    return value


def _read_regular_git_blob(
    repo: _worktrees.RepositoryBinding,
    commit: str,
    path: str,
    description: str,
) -> bytes:
    entry = _worktrees._git_bytes(
        repo,
        "--literal-pathspecs",
        "ls-tree",
        "-z",
        "--full-tree",
        commit,
        "--",
        path,
    )
    records = [record for record in entry.split(b"\0") if record]
    expected_path = path.encode("utf-8")
    if len(records) != 1:
        raise EvalError(f"{description} path must name one regular Git blob: {path}")
    header, separator, raw_path = records[0].partition(b"\t")
    fields = header.split(b" ")
    if (
        separator != b"\t"
        or raw_path != expected_path
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
        or re.fullmatch(rb"[0-9a-f]{40}", fields[2]) is None
    ):
        raise EvalError(f"{description} path must name one regular Git blob: {path}")
    return _worktrees._git_bytes(repo, "cat-file", "blob", fields[2].decode("ascii"))


def _is_commit(value: object) -> bool:
    return isinstance(value, str) and _COMMIT.fullmatch(value) is not None


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _acquire_execution_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise EvalError(f"unable to open execution lock: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvalError(f"execution lock must be a single-link regular file: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_existing_idempotency_lock(path: Path) -> int:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise EvalError(f"unable to open existing idempotency lock: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvalError(
                f"idempotency lock must be a single-link regular file: {path}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = os.fstat(descriptor)
        try:
            observed = path.lstat()
        except OSError as error:
            raise EvalError(f"idempotency lock path changed: {path}") from error
        if (
            not stat.S_ISREG(locked.st_mode)
            or locked.st_nlink != 1
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino) != (locked.st_dev, locked.st_ino)
        ):
            raise EvalError(f"idempotency lock identity changed: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _observe_execution_lock(
    path: Path,
) -> tuple[str, int | None, tuple[int, int] | None]:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "missing", None, None
    except OSError as error:
        raise EvalError(f"unable to inspect execution lock: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EvalError(f"execution lock must be a single-link regular file: {path}")
        identity = (metadata.st_dev, metadata.st_ino)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return "contended", None, identity
        return "acquired", descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _linux_broker_owns_lock(
    execution: dict[str, object],
    lock_identity: tuple[int, int],
) -> bool:
    broker_pid = int(execution["broker_pid"])
    recorded_token = str(execution["broker_start_token"])
    if _linux_process_start_token(broker_pid) != recorded_token:
        return False
    owns_lock_file = False
    try:
        with os.scandir(f"/proc/{broker_pid}/fd") as entries:
            for entry in entries:
                try:
                    metadata = entry.stat(follow_symlinks=True)
                except OSError:
                    continue
                if (metadata.st_dev, metadata.st_ino) == lock_identity:
                    owns_lock_file = True
                    break
    except OSError:
        return False
    return (
        _linux_process_start_token(broker_pid) == recorded_token
        and owns_lock_file
    )


def _linux_process_start_token(pid: int) -> str | None:
    if pid < 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_name = raw.rsplit(")", 1)[1].split()
        return f"linux-proc-start:{fields_after_name[19]}"
    except (OSError, IndexError, ValueError):
        return None


def _require_linux_claim_runtime() -> None:
    if sys.platform != "linux":
        raise EvalError("evaluation execution claim runtime requires Linux")


def _release_execution_lock(descriptor: int) -> None:
    os.close(descriptor)


def _require_clean_registration(repo: _worktrees.RepositoryBinding) -> None:
    try:
        _worktrees._reject_checkout_filters(repo)
    except _worktrees.WorktreeError as error:
        raise EvalError(f"registration filter configuration is unsafe: {error}") from error
    _reject_registration_hooks(repo)
    status_output = _worktrees._git_bytes(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status_output:
        raise EvalError("registration repository is dirty or contains untracked files")
    index_entries = _worktrees._git_bytes(repo, "ls-files", "-v", "-z")
    if any(
        entry[:1] == b"S" or entry[:1].islower()
        for entry in index_entries.split(b"\0")
        if entry
    ):
        raise EvalError("registration repository has ambiguous index flags")
    index = _worktrees._git_result(
        repo,
        "diff-index",
        "--cached",
        "--quiet",
        "HEAD",
        "--",
    )
    if index.returncode != 0:
        raise EvalError("registration repository is dirty")


def _reject_registration_hooks(repo: _worktrees.RepositoryBinding) -> None:
    configured = _worktrees._git_result(
        repo,
        "config",
        "--local",
        "--get",
        "core.hooksPath",
    )
    if configured.returncode == 0:
        raise EvalError("registration Git hook configuration is not allowed")
    if configured.returncode != 1:
        raise EvalError("unable to inspect registration Git hook configuration")
    hooks = repo.common_dir / "hooks"
    try:
        metadata = hooks.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EvalError("unable to inspect registration Git hooks") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EvalError("registration Git hooks path is ambiguous")
    try:
        entries = list(hooks.iterdir())
    except OSError as error:
        raise EvalError("unable to inspect registration Git hooks") from error
    if any(not entry.name.endswith(".sample") for entry in entries):
        raise EvalError("registration Git hooks are not allowed")
