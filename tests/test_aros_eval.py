"""Visible AROS evaluation registration and one-attempt request tests."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import fcntl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import arbor.aros.eval as eval_module
import arbor.aros.worktrees as worktrees_module
from arbor.aros.eval import EvalError, EvalService
from arbor.aros.eval_records import validate_measurement_receipt
from arbor.aros.receipts import record_sha256
from arbor.aros.runs import RunService
from arbor.aros.store import process_start_token


requires_linux_claims = pytest.mark.skipif(
    sys.platform != "linux",
    reason="evaluation execution claims require Linux procfs and flock",
)


def _git(root: Path, *args: str) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "LD_", "DYLD_"))
    }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


def _visible_manifest(apparatus_commit: str, blob_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evaluator_id": "quality",
        "evaluator_version": "1",
        "visibility": "visible",
        "apparatus_commit": apparatus_commit,
        "apparatus_paths": [
            {"path": "evaluation/score.py", "blob_sha256": blob_sha256}
        ],
        "scorer_argv": ["python", "../apparatus/evaluation/score.py"],
        "scorer_cwd": ".",
        "inputs": [],
        "environment_ref": "isolated-evaluator-v1",
        "seed_policy": {"kind": "fixed", "seed": 7},
        "resource_limits": {"timeout_seconds": 300},
        "success_exit_codes": [0],
        "raw_outputs": ["stdout", "stderr"],
        "metric_output": {
            "source": "scorer_stdout",
            "parser": "aros.scalar-metric-v1",
            "metric_name": "quality",
            "minimum": 0,
            "maximum": 1,
            "minimum_samples": 1,
        },
        "known_limitations": [],
        "calibration_refs": [],
    }


def _init_evaluator_repository(root: Path) -> tuple[dict[str, object], str, str]:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / ".gitignore").write_text("/.aros/\n", encoding="utf-8")
    scorer = root / "evaluation" / "score.py"
    scorer.parent.mkdir()
    scorer.write_bytes(b"print('{\"schema_version\":1,\"metric\":0.5,\"sample_count\":1}')\n")
    _git(root, "add", ".gitignore", "evaluation/score.py")
    _git(root, "commit", "-qm", "add exact apparatus")
    apparatus_commit = _git(root, "rev-parse", "HEAD")
    apparatus_tree = _git(root, "rev-parse", f"{apparatus_commit}^{{tree}}")
    manifest = _visible_manifest(
        apparatus_commit,
        hashlib.sha256(scorer.read_bytes()).hexdigest(),
    )
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", "eval/suites/quality/1/manifest.json")
    _git(root, "commit", "-qm", "add visible evaluator manifest")
    return manifest, apparatus_tree, _git(root, "rev-parse", "HEAD")


def _terminal_receipt(
    request: dict[str, object],
    execution: dict[str, object],
) -> dict[str, object]:
    run_id = "RUN-visible-receipt"
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    receipt: dict[str, object] = {
        "schema_version": 1,
        "eval_id": request["eval_id"],
        "evaluation_state": "completed",
        "referenced_process_state": "completed",
        "measurement_state": "valid",
        "descriptor_sha256": request["descriptor_sha256"],
        "request_sha256": request["request_sha256"],
        "execution_sha256": execution["execution_sha256"],
        "run_id": run_id,
        "run_manifest_sha256": "1" * 64,
        "run_final_sha256": "2" * 64,
        "bundle_sha256": "3" * 64,
        "candidate_commit": request["candidate_commit"],
        "apparatus_commit": request["apparatus_commit"],
        "metric": 0.5,
        "sample_count": 1,
        "metric_name": "quality",
        "parser": "aros.scalar-metric-v1",
        "bundle_cleanup_state": "removed",
        "stdout": {
            "path": f".aros/runs/{run_id}/stdout.log",
            "bytes": 0,
            "sha256": empty_sha256,
        },
        "stderr": {
            "path": f".aros/runs/{run_id}/stderr.log",
            "bytes": 0,
            "sha256": empty_sha256,
        },
        "finished_at": execution["claimed_at"],
    }
    receipt["receipt_sha256"] = record_sha256(receipt, "receipt_sha256")
    return validate_measurement_receipt(receipt)


def _prepend_duplicate_json_field(path: Path, field: str, value: object) -> None:
    text = path.read_text(encoding="utf-8")
    marker = f'  "{field}":'
    assert marker in text
    path.write_text(
        text.replace(
            marker,
            f'  "{field}": {json.dumps(value)},\n{marker}',
            1,
        ),
        encoding="utf-8",
    )


def test_register_freezes_manifest_blob_and_exact_apparatus_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    manifest, apparatus_tree, manifest_commit = _init_evaluator_repository(root)
    manifest_ref = "eval/suites/quality/1/manifest.json"
    manifest_blob = subprocess.run(
        ["git", "-C", str(root), "show", f"{manifest_commit}:{manifest_ref}"],
        check=True,
        capture_output=True,
    ).stdout

    descriptor = EvalService(root).register(manifest_ref, actor="principal")

    expected = {
        **manifest,
        "manifest_ref": manifest_ref,
        "manifest_commit": manifest_commit,
        "manifest_blob_sha256": hashlib.sha256(manifest_blob).hexdigest(),
        "apparatus_tree": apparatus_tree,
        "registration_actor": "principal",
        "registered_at": descriptor["registered_at"],
    }
    expected["descriptor_sha256"] = record_sha256(expected, "descriptor_sha256")
    assert descriptor == expected
    descriptor_path = root / ".aros" / "evaluators" / "quality" / "1" / "descriptor.json"
    assert json.loads(descriptor_path.read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize(
    "drift",
    ("dirty", "untracked", "filter", "hook", "blob"),
)
def test_register_rejects_dirty_untracked_filter_hook_or_blob_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    root = tmp_path / "repository"
    manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    filter_marker: Path | None = None
    if drift == "dirty":
        (root / "evaluation" / "score.py").write_text(
            "print('dirty working bytes')\n",
            encoding="utf-8",
        )
    elif drift == "untracked":
        (root / "untracked.txt").write_text("untracked drift\n", encoding="utf-8")
    elif drift == "filter":
        (root / ".gitattributes").write_text(
            "evaluation/score.py filter=malicious\n",
            encoding="utf-8",
        )
        _git(root, "add", ".gitattributes")
        _git(root, "commit", "-qm", "declare apparatus clean filter")
        filter_marker = tmp_path / "clean-filter-ran"
        _git(
            root,
            "config",
            "filter.malicious.clean",
            f"sh -c 'touch {shlex.quote(str(filter_marker))}; cat'",
        )
    elif drift == "hook":
        hook = root / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
    else:
        manifest["apparatus_paths"] = [
            {"path": "evaluation/score.py", "blob_sha256": "0" * 64}
        ]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _git(root, "add", "eval/suites/quality/1/manifest.json")
        _git(root, "commit", "-qm", "drift declared apparatus blob")

    with pytest.raises(EvalError, match="dirty|untracked|filter|hook|blob"):
        EvalService(root).register(
            "eval/suites/quality/1/manifest.json",
            actor="principal",
        )

    assert not (root / ".aros" / "evaluators").exists()
    if filter_marker is not None:
        assert not filter_marker.exists()


@pytest.mark.parametrize("object_kind", ("tree", "symlink", "submodule"))
def test_register_requires_regular_apparatus_blobs(
    tmp_path: Path,
    object_kind: str,
) -> None:
    root = tmp_path / "repository"
    manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    if object_kind == "symlink":
        apparatus_path = "evaluation/link.py"
        link = root / apparatus_path
        link.symlink_to("../outside.py")
        _git(root, "add", apparatus_path)
    elif object_kind == "submodule":
        apparatus_path = "evaluation/submodule"
        submodule = tmp_path / "submodule"
        subprocess.run(["git", "init", "-q", str(submodule)], check=True)
        _git(submodule, "config", "user.email", "aros@example.invalid")
        _git(submodule, "config", "user.name", "AROS test")
        (submodule / "score.py").write_text("submodule scorer\n", encoding="utf-8")
        _git(submodule, "add", "score.py")
        _git(submodule, "commit", "-qm", "submodule apparatus")
        _git(
            root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(submodule),
            apparatus_path,
        )
    else:
        apparatus_path = "evaluation"
        (root / "evaluation" / "extra.py").write_text("tree member\n", encoding="utf-8")
        _git(root, "add", "evaluation/extra.py")
    _git(root, "commit", "-qm", f"add non-regular {object_kind} apparatus")
    apparatus_commit = _git(root, "rev-parse", "HEAD")
    raw_object = (
        b""
        if object_kind == "submodule"
        else subprocess.run(
            ["git", "-C", str(root), "show", f"{apparatus_commit}:{apparatus_path}"],
            check=True,
            capture_output=True,
        ).stdout
    )
    manifest["apparatus_commit"] = apparatus_commit
    manifest["apparatus_paths"] = [
        {
            "path": apparatus_path,
            "blob_sha256": hashlib.sha256(raw_object).hexdigest(),
        }
    ]
    manifest["scorer_argv"] = ["python", f"../apparatus/{apparatus_path}"]
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", "eval/suites/quality/1/manifest.json")
    _git(root, "commit", "-qm", "declare non-regular apparatus")

    with pytest.raises(EvalError, match="regular Git blob"):
        EvalService(root).register(
            "eval/suites/quality/1/manifest.json",
            actor="principal",
        )

    assert not (root / ".aros" / "evaluators").exists()


def test_register_requires_a_regular_manifest_blob(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    manifest_directory = root / "eval" / "suites" / "quality" / "1"
    manifest_path = manifest_directory / "manifest.json"
    manifest_path.unlink()
    (manifest_directory / "actual.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path.symlink_to("actual.json")
    _git(root, "add", "eval/suites/quality/1")
    _git(root, "commit", "-qm", "replace manifest with symlink blob")

    with pytest.raises(EvalError, match="regular Git blob"):
        EvalService(root).register(
            "eval/suites/quality/1/manifest.json",
            actor="principal",
        )

    assert not (root / ".aros" / "evaluators").exists()


def test_register_rejects_duplicate_manifest_json_keys(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    _prepend_duplicate_json_field(manifest_path, "evaluator_id", "shadow")
    _git(root, "add", "eval/suites/quality/1/manifest.json")
    _git(root, "commit", "-qm", "add ambiguous manifest key")

    with pytest.raises(EvalError, match="duplicate"):
        EvalService(root).register(
            "eval/suites/quality/1/manifest.json",
            actor="principal",
        )

    assert not (root / ".aros" / "evaluators").exists()


def test_register_strict_json_rejects_overflowing_float_before_manifest_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    assert '"timeout_seconds": 300' in raw
    manifest_path.write_text(
        raw.replace('"timeout_seconds": 300', '"timeout_seconds": 1e400'),
        encoding="utf-8",
    )
    _git(root, "add", "eval/suites/quality/1/manifest.json")
    _git(root, "commit", "-qm", "add overflowing JSON float")

    def forbidden_manifest_parse(_value: object) -> object:
        raise AssertionError("strict JSON must reject before manifest parsing")

    monkeypatch.setattr(eval_module, "parse_visible_manifest", forbidden_manifest_parse)
    with pytest.raises(EvalError, match="non-finite"):
        EvalService(root).register(
            "eval/suites/quality/1/manifest.json",
            actor="principal",
        )

    assert not (root / ".aros" / "evaluators").exists()


@pytest.mark.parametrize("encoding", ("utf-16", "utf-32"))
def test_register_rejects_non_utf8_manifest_bytes(
    tmp_path: Path,
    encoding: str,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_text(encoding="utf-8").encode(encoding))
    _git(root, "add", "eval/suites/quality/1/manifest.json")
    _git(root, "commit", "-qm", f"encode manifest as {encoding}")

    with pytest.raises(EvalError, match="UTF-8"):
        EvalService(root).register(
            "eval/suites/quality/1/manifest.json",
            actor="principal",
        )

    assert not (root / ".aros" / "evaluators").exists()


def test_eval_id_is_full_idempotency_digest_and_request_is_create_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    descriptor = service.register(
        "eval/suites/quality/1/manifest.json",
        actor="registrar",
    )
    key = "one-visible-evaluation"
    key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()

    request, created = service._publish_request(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )

    expected = {
        "schema_version": 1,
        "eval_id": f"EVAL-{key_digest}",
        "evaluator_id": "quality",
        "evaluator_version": "1",
        "descriptor_sha256": descriptor["descriptor_sha256"],
        "candidate_commit": candidate_commit,
        "apparatus_commit": descriptor["apparatus_commit"],
        "actor": "principal",
        "idempotency_key_sha256": key_digest,
        "created_at": request["created_at"],
    }
    expected["request_sha256"] = record_sha256(expected, "request_sha256")
    assert created is True
    assert request == expected
    same_request, same_created = service._publish_request(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert same_created is False
    assert same_request == request
    evaluation_root = root / ".aros" / "evaluations" / f"EVAL-{key_digest}"
    assert json.loads(
        (evaluation_root / "request.json").read_text(encoding="utf-8")
    ) == expected
    assert sorted(path.name for path in evaluation_root.iterdir()) == ["request.json"]
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()


def test_same_key_different_request_rejects_without_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "same-key-different-request"
    original, created = service._publish_request(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert created is True

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("request replay must not materialize or invoke Run")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)

    with pytest.raises(EvalError, match="idempotency key.*different request"):
        service._publish_request(
            "quality",
            "1",
            candidate_commit,
            "different-actor",
            key,
        )

    eval_id = str(original["eval_id"])
    request_path = root / ".aros" / "evaluations" / eval_id / "request.json"
    assert json.loads(request_path.read_text(encoding="utf-8")) == original
    assert sorted(path.name for path in request_path.parent.iterdir()) == ["request.json"]
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()


@requires_linux_claims
def test_existing_request_replay_does_not_reresolve_git_or_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "existing-request-is-authority"
    request, created = service._publish_request(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert created is True

    def forbidden_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("existing request replay must not re-resolve inputs")

    monkeypatch.setattr(eval_module, "_resolve_exact_commit", forbidden_resolution)
    monkeypatch.setattr(service, "_load_descriptor", forbidden_resolution)

    replay = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert replay.status["eval_id"] == request["eval_id"]
    with pytest.raises(EvalError, match="different request"):
        service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "different-actor",
            key,
        )


def test_execution_claim_runtime_is_linux_only_but_requests_are_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "portable-request-linux-claim"
    monkeypatch.setattr(sys, "platform", "darwin")

    request, created = service._publish_request(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )

    assert created is True
    with pytest.raises(EvalError, match="requires Linux"):
        service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
    evaluation_root = root / ".aros" / "evaluations" / str(request["eval_id"])
    assert sorted(path.name for path in evaluation_root.iterdir()) == ["request.json"]


@requires_linux_claims
def test_linux_start_token_and_execution_claim_allow_pid_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    real_read_text = Path.read_text
    proc_stat = "1 (init) " + " ".join(["S", *map(str, range(4, 23))])

    def read_proc_stat(path: Path, *args: object, **kwargs: object) -> str:
        if path == Path("/proc/1/stat"):
            return proc_stat
        return real_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_proc_stat)
    monkeypatch.setattr(eval_module.os, "getpid", lambda: 1)
    assert eval_module._linux_process_start_token(1) == "linux-proc-start:22"

    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        "pid-one-broker",
    )

    assert isinstance(lease, eval_module.ExecutionLease)
    assert lease.execution["broker_pid"] == 1
    assert lease.execution["broker_start_token"] == "linux-proc-start:22"
    lease.close()


@requires_linux_claims
def test_execution_claim_is_local_one_attempt_and_never_transfers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("claim admission must not materialize or invoke Run")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    key = "one-local-execution-claim"

    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )

    assert isinstance(lease, eval_module.ExecutionLease)
    expected_execution = {
        "schema_version": 1,
        "eval_id": lease.request["eval_id"],
        "request_sha256": lease.request["request_sha256"],
        "host": lease.execution["host"],
        "broker_pid": os.getpid(),
        "broker_start_token": process_start_token(os.getpid()),
        "claimed_at": lease.execution["claimed_at"],
    }
    expected_execution["execution_sha256"] = record_sha256(
        expected_execution,
        "execution_sha256",
    )
    assert lease.execution == expected_execution
    os.fstat(lease.lock_fd)
    replay = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "running"
    assert replay.status["eval_id"] == lease.request["eval_id"]
    old_fd = lease.lock_fd
    lease.close()
    lease.close()
    with pytest.raises(OSError):
        os.fstat(old_fd)
    execution_path = (
        root
        / ".aros"
        / "evaluations"
        / str(lease.request["eval_id"])
        / "execution.json"
    )
    assert json.loads(execution_path.read_text(encoding="utf-8")) == expected_execution
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()


@requires_linux_claims
def test_execution_lease_close_is_an_atomic_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        "concurrent-lease-close",
    )
    assert isinstance(lease, eval_module.ExecutionLease)

    class InterleavingDescriptor:
        def __init__(self) -> None:
            self.first_reads = threading.Barrier(2)
            self.second_reads = threading.Barrier(2)
            self.read_counts: dict[int, int] = {}
            self.guard = threading.Lock()

        def __get__(self, instance: object, _owner: object) -> int | object:
            if instance is None:
                return self
            values = vars(instance)
            value = int(values["lock_fd"])
            if value < 0:
                return value
            thread_id = threading.get_ident()
            with self.guard:
                count = self.read_counts.get(thread_id, 0) + 1
                self.read_counts[thread_id] = count
            barrier = self.first_reads if count == 1 else self.second_reads
            try:
                barrier.wait(timeout=0.25)
            except threading.BrokenBarrierError:
                pass
            return value

        def __set__(self, instance: object, value: int) -> None:
            vars(instance)["lock_fd"] = value

    monkeypatch.setattr(
        eval_module.ExecutionLease,
        "lock_fd",
        InterleavingDescriptor(),
        raising=False,
    )
    start = threading.Barrier(2)

    def close() -> None:
        start.wait(timeout=5)
        lease.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(close) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)

    assert vars(lease)["lock_fd"] == -1


@requires_linux_claims
def test_existing_released_claim_returns_lost_before_bundle_or_run_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "released-claim-is-lost"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    execution_path = (
        root
        / ".aros"
        / "evaluations"
        / str(lease.request["eval_id"])
        / "execution.json"
    )
    original_claim = execution_path.read_bytes()
    original_inode = execution_path.stat().st_ino
    lease.close()

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("released claim replay must not materialize or invoke Run")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)

    replay = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert set(replay.status) == {
        "eval_id",
        "evaluation_state",
        "referenced_process_state",
        "measurement_state",
        "run_id",
        "receipt_ref",
        "reason",
        "updated_at",
    }
    assert replay.status["eval_id"] == lease.request["eval_id"]
    assert replay.status["evaluation_state"] == "lost"
    assert replay.status["referenced_process_state"] == "lost"
    assert replay.status["measurement_state"] == "not_available"
    assert replay.status["run_id"] is None
    assert replay.status["receipt_ref"] is None
    assert "released" in str(replay.status["reason"])
    assert execution_path.read_bytes() == original_claim
    assert execution_path.stat().st_ino == original_inode
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()


@requires_linux_claims
def test_missing_execution_lock_is_lost(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "missing-execution-lock"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    lease.close()
    lock_path = (
        root
        / ".aros"
        / "locks"
        / f"{lease.request['eval_id']}-execution.lock"
    )
    lock_path.unlink()

    replay = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert "released" in str(replay.status["reason"])


@requires_linux_claims
def test_unrelated_process_flock_does_not_keep_released_claim_running(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "unrelated-flock"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    lock_path = (
        root
        / ".aros"
        / "locks"
        / f"{lease.request['eval_id']}-execution.lock"
    )
    lease.close()
    ready_reader, ready_writer = os.pipe()
    release_reader, release_writer = os.pipe()
    holder_pid = os.fork()
    if holder_pid == 0:
        try:
            os.close(ready_reader)
            os.close(release_writer)
            lock_fd = os.open(lock_path, os.O_RDWR)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            os.write(ready_writer, b"1")
            os.read(release_reader, 1)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except BaseException:
            os._exit(91)
        os._exit(0)
    os.close(ready_writer)
    os.close(release_reader)
    assert os.read(ready_reader, 1) == b"1"
    try:
        replay = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
    finally:
        os.write(release_writer, b"1")
        os.close(release_writer)
        os.close(ready_reader)
        _, holder_status = os.waitpid(holder_pid, 0)

    assert os.waitstatus_to_exitcode(holder_status) == 0
    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert "broker" in str(replay.status["reason"])


@requires_linux_claims
def test_cross_process_winner_and_follower_observe_one_live_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "cross-process-one-claim"
    ready_reader, ready_writer = os.pipe()
    release_reader, release_writer = os.pipe()
    winner_pid = os.fork()
    if winner_pid == 0:
        try:
            os.close(ready_reader)
            os.close(release_writer)
            winner = EvalService(root)._begin_execution(
                "quality",
                "1",
                candidate_commit,
                "principal",
                key,
            )
            if not isinstance(winner, eval_module.ExecutionLease):
                os._exit(92)
            os.write(ready_writer, b"1")
            os.read(release_reader, 1)
            winner.close()
        except BaseException:
            os._exit(91)
        os._exit(0)
    os.close(ready_writer)
    os.close(release_reader)
    assert os.read(ready_reader, 1) == b"1"
    try:
        follower = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
        execution_path = (
            root
            / ".aros"
            / "evaluations"
            / str(follower.status["eval_id"])
            / "execution.json"
        )
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        assert execution["broker_pid"] == winner_pid
        assert isinstance(follower, eval_module.ExistingEvaluation)
        assert follower.status["evaluation_state"] == "running"
    finally:
        os.write(release_writer, b"1")
        os.close(release_writer)
        os.close(ready_reader)
        _, winner_status = os.waitpid(winner_pid, 0)

    assert os.waitstatus_to_exitcode(winner_status) == 0
    replay = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"


@requires_linux_claims
def test_crash_after_request_before_claim_is_irrevocably_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "crash-between-request-and-claim"
    original_create_json = eval_module.create_json

    def crash_after_request(path: str | Path, value: object) -> bool:
        created = original_create_json(path, value)
        if Path(path).name == "request.json" and created:
            raise RuntimeError("injected crash after request publication")
        return created

    monkeypatch.setattr(eval_module, "create_json", crash_after_request)
    with pytest.raises(RuntimeError, match="injected crash"):
        service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
    monkeypatch.setattr(eval_module, "create_json", original_create_json)
    key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    evaluation_root = root / ".aros" / "evaluations" / f"EVAL-{key_digest}"
    request_path = evaluation_root / "request.json"
    original_request = request_path.read_bytes()
    original_inode = request_path.stat().st_ino
    assert sorted(path.name for path in evaluation_root.iterdir()) == ["request.json"]

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("request-only crash must never transfer the attempt")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    for _ in range(2):
        replay = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
        assert isinstance(replay, eval_module.ExistingEvaluation)
        assert replay.status["evaluation_state"] == "lost"
        assert "no execution claim" in str(replay.status["reason"])

    assert request_path.read_bytes() == original_request
    assert request_path.stat().st_ino == original_inode
    assert sorted(path.name for path in evaluation_root.iterdir()) == ["request.json"]
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()


@requires_linux_claims
def test_concurrent_same_key_publishes_one_request_and_one_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "concurrent-one-attempt"
    original_create_json = eval_module.create_json
    request_created = threading.Event()
    allow_winner_to_claim = threading.Event()
    second_finished = threading.Event()
    created_paths: list[str] = []
    created_paths_lock = threading.Lock()

    def pause_after_winning_request(path: str | Path, value: object) -> bool:
        created = original_create_json(path, value)
        if created:
            with created_paths_lock:
                created_paths.append(Path(path).name)
        if Path(path).name == "request.json" and created:
            request_created.set()
            assert allow_winner_to_claim.wait(timeout=5)
        return created

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("concurrent admission must not materialize or invoke Run")

    monkeypatch.setattr(eval_module, "create_json", pause_after_winning_request)
    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)

    def begin() -> object:
        return service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )

    def begin_second() -> object:
        try:
            return begin()
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(begin)
        assert request_created.wait(timeout=5)
        follower = pool.submit(begin_second)
        follower_finished_before_claim = second_finished.wait(timeout=1)
        allow_winner_to_claim.set()
        results = [winner.result(timeout=5), follower.result(timeout=5)]

    assert follower_finished_before_claim is False
    leases = [item for item in results if isinstance(item, eval_module.ExecutionLease)]
    existing = [
        item for item in results if isinstance(item, eval_module.ExistingEvaluation)
    ]
    assert len(leases) == 1
    assert len(existing) == 1
    assert existing[0].status["evaluation_state"] == "running"
    assert created_paths.count("request.json") == 1
    assert created_paths.count("execution.json") == 1
    eval_id = str(leases[0].request["eval_id"])
    evaluation_root = root / ".aros" / "evaluations" / eval_id
    assert sorted(path.name for path in evaluation_root.iterdir()) == [
        "execution.json",
        "request.json",
    ]
    leases[0].close()
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()


@requires_linux_claims
def test_existing_receipt_wins_over_a_live_execution_claim(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "terminal-receipt-wins"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    receipt = _terminal_receipt(lease.request, lease.execution)
    receipt_path = (
        root
        / "eval"
        / "evaluations"
        / str(lease.request["eval_id"])
        / "receipt.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    replay = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status == receipt
    lease.close()


@requires_linux_claims
def test_receipt_publication_linearizes_before_released_claim_becomes_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "receipt-linearization"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    receipt = _terminal_receipt(lease.request, lease.execution)
    receipt_path = (
        root
        / "eval"
        / "evaluations"
        / str(lease.request["eval_id"])
        / "receipt.json"
    )
    initial_receipt_miss = threading.Event()
    receipt_published = threading.Event()
    real_read_json_strict = eval_module.read_json_strict
    first_receipt_read = True

    def pause_initial_receipt_read(path: str | Path) -> object:
        nonlocal first_receipt_read
        if Path(path) == receipt_path and first_receipt_read:
            first_receipt_read = False
            initial_receipt_miss.set()
            assert receipt_published.wait(timeout=5)
            raise FileNotFoundError(receipt_path)
        return real_read_json_strict(path)

    monkeypatch.setattr(eval_module, "read_json_strict", pause_initial_receipt_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        replay_future = pool.submit(
            service._begin_execution,
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
        assert initial_receipt_miss.wait(timeout=5)
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        lease.close()
        receipt_published.set()
        replay = replay_future.result(timeout=5)

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status == receipt


@requires_linux_claims
def test_existing_receipt_must_match_the_local_execution_claim(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "receipt-execution-lineage"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    receipt = _terminal_receipt(lease.request, lease.execution)
    receipt["execution_sha256"] = "f" * 64
    receipt["receipt_sha256"] = record_sha256(receipt, "receipt_sha256")
    receipt = validate_measurement_receipt(receipt)
    receipt_path = (
        root
        / "eval"
        / "evaluations"
        / str(lease.request["eval_id"])
        / "receipt.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    try:
        with pytest.raises(EvalError, match="execution.*lineage"):
            service._begin_execution(
                "quality",
                "1",
                candidate_commit,
                "principal",
                key,
            )
    finally:
        lease.close()


@pytest.mark.parametrize("boundary", ("descriptor", "request", "execution", "receipt"))
def test_eval_rejects_duplicate_keys_at_persisted_json_boundaries(
    tmp_path: Path,
    boundary: str,
) -> None:
    if boundary in {"execution", "receipt"} and sys.platform != "linux":
        pytest.skip("evaluation execution claims require Linux")
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = f"duplicate-{boundary}-boundary"
    lease: object | None = None
    if boundary == "descriptor":
        target = root / ".aros" / "evaluators" / "quality" / "1" / "descriptor.json"
    elif boundary == "request":
        request, created = service._publish_request(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
        assert created is True
        target = (
            root
            / ".aros"
            / "evaluations"
            / str(request["eval_id"])
            / "request.json"
        )
    else:
        lease = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
        assert isinstance(lease, eval_module.ExecutionLease)
        if boundary == "execution":
            target = (
                root
                / ".aros"
                / "evaluations"
                / str(lease.request["eval_id"])
                / "execution.json"
            )
        else:
            receipt = _terminal_receipt(lease.request, lease.execution)
            target = (
                root
                / "eval"
                / "evaluations"
                / str(lease.request["eval_id"])
                / "receipt.json"
            )
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
    _prepend_duplicate_json_field(target, "schema_version", 999)

    try:
        with pytest.raises(EvalError, match="invalid|duplicate"):
            if boundary in {"descriptor", "request"}:
                service._publish_request(
                    "quality",
                    "1",
                    candidate_commit,
                    "principal",
                    key,
                )
            else:
                service._begin_execution(
                    "quality",
                    "1",
                    candidate_commit,
                    "principal",
                    key,
                )
    finally:
        if isinstance(lease, eval_module.ExecutionLease):
            lease.close()


@requires_linux_claims
def test_dead_claim_holder_is_lost_even_when_execution_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "dead-claim-holder"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    lease.close()
    execution_path = (
        root
        / ".aros"
        / "evaluations"
        / str(lease.request["eval_id"])
        / "execution.json"
    )
    dead_execution = dict(lease.execution)
    dead_execution["broker_pid"] = 2_147_483_647
    dead_execution["broker_start_token"] = "linux-proc-start:1"
    dead_execution["execution_sha256"] = record_sha256(
        dead_execution,
        "execution_sha256",
    )
    execution_path.write_text(
        json.dumps(dead_execution, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    lock_path = (
        root
        / ".aros"
        / "locks"
        / f"{lease.request['eval_id']}-execution.lock"
    )
    lock_fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dead claim replay must not materialize or invoke Run")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    try:
        replay = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert "broker is not live" in str(replay.status["reason"])
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()


@requires_linux_claims
def test_claim_from_another_host_is_lost_even_when_pid_and_lock_are_live(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "remote-host-claim"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    execution_path = (
        root
        / ".aros"
        / "evaluations"
        / str(lease.request["eval_id"])
        / "execution.json"
    )
    remote_execution = dict(lease.execution)
    remote_execution["host"] = f"remote-{lease.execution['host']}"
    remote_execution["execution_sha256"] = record_sha256(
        remote_execution,
        "execution_sha256",
    )
    execution_path.write_text(
        json.dumps(remote_execution, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    try:
        replay = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
    finally:
        lease.close()

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert "host" in str(replay.status["reason"])


@requires_linux_claims
def test_crash_after_claim_publication_never_transfers_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "crash-after-claim-publication"
    key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    eval_id = f"EVAL-{key_digest}"
    lock_path = root / ".aros" / "locks" / f"{eval_id}-execution.lock"
    original_create_json = eval_module.create_json

    def crash_after_claim(path: str | Path, value: object) -> bool:
        created = original_create_json(path, value)
        if Path(path).name == "execution.json" and created:
            raise RuntimeError("injected crash after claim publication")
        return created

    monkeypatch.setattr(eval_module, "create_json", crash_after_claim)
    with pytest.raises(RuntimeError, match="injected crash"):
        service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
    monkeypatch.setattr(eval_module, "create_json", original_create_json)

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("published claim must never transfer after crash")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    try:
        replay = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
        assert isinstance(replay, eval_module.ExistingEvaluation)
        assert replay.status["evaluation_state"] == "lost"
        assert "released" in str(replay.status["reason"])
    finally:
        leaked_descriptors: list[int] = []
        for descriptor_name in os.listdir("/proc/self/fd"):
            descriptor = int(descriptor_name)
            try:
                target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                continue
            if target == lock_path:
                leaked_descriptors.append(descriptor)
                os.close(descriptor)
        assert leaked_descriptors == []

    evaluation_root = root / ".aros" / "evaluations" / eval_id
    assert sorted(path.name for path in evaluation_root.iterdir()) == [
        "execution.json",
        "request.json",
    ]
    assert not (root / ".worktree").exists()
    assert not (root / ".aros" / "runs").exists()
