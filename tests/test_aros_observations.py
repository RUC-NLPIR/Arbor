"""Pure, service-validated observation lineage tests."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

import arbor.aros.observations as observations_module
import arbor.aros.runs as runs_module
import arbor.aros.store as store_module
import arbor.aros.tasks as tasks_module
from arbor.aros.eval import EvalService, read_validated_eval_receipt
from arbor.aros.eval_records import build_measurement_receipt
from arbor.aros.observations import (
    ObservationCatalog,
    ObservationError,
    ObservationRecord,
    validate_task_measurement_lineage,
)
from arbor.aros.receipts import record_sha256
from arbor.aros.runs import (
    RunError,
    RunService,
    read_validated_run_final,
    read_validated_run_manifest,
)
from arbor.aros.store import (
    atomic_write_json,
    final_identity,
    json_sha256,
    manifest_sha256,
    read_json_strict_no_repair,
)
from arbor.aros.tasks import TaskError, TaskService, read_validated_task_collection
from arbor.aros.worktrees import create_execution_bundle
from tests import test_aros_eval as eval_test_support
from tests import test_aros_tasks as task_test_support


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(root: Path) -> tuple[str, str]:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "aros@example.invalid")
    _git(root, "config", "user.name", "AROS test")
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "candidate.txt")
    _git(root, "commit", "-qm", "candidate")
    candidate_commit = _git(root, "rev-parse", "HEAD")
    scorer = root / "evaluation" / "score.py"
    scorer.parent.mkdir()
    scorer.write_text("print('score')\n", encoding="utf-8")
    _git(root, "add", "evaluation/score.py")
    _git(root, "commit", "-qm", "apparatus")
    return candidate_commit, _git(root, "rev-parse", "HEAD")


def _install_run_final(
    root: Path,
) -> tuple[RunService, dict[str, object], dict[str, object]]:
    _init_repo(root)
    service = RunService(root)
    manifest = service.prepare(
        [sys.executable, "-c", "pass"],
        idempotency_key="standalone-observation",
        actor="principal",
        security_profile="trusted-local",
    )
    run_id = str(manifest["run_id"])
    timestamp = str(manifest["created_at"])
    prelaunch: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": f"{run_id}-prelaunch",
        "kind": "run_prelaunch",
        "run_id": run_id,
        "actor": manifest["actor"],
        "created_at": timestamp,
        "base_commit": manifest["base_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "carrier": "tmux",
        "tmux_session": f"aros-{run_id.lower()}",
        "host": socket.gethostname(),
        "runner_version": 1,
        "runner_invocation": [
            sys.executable,
            "-m",
            "arbor.aros.runner",
            "--workspace",
            str(root),
            "--run-id",
            run_id,
        ],
    }
    prelaunch["receipt_sha256"] = record_sha256(prelaunch, "receipt_sha256")
    atomic_write_json(
        root / ".aros" / "receipts" / f"{run_id}-prelaunch.json",
        prelaunch,
    )
    runtime = root / ".aros" / "runs" / run_id
    (runtime / "stdout.log").write_bytes(b"")
    (runtime / "stderr.log").write_bytes(b"")
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    final = final_identity(manifest)
    final.update(
        {
            "schema_version": 1,
            "state": "completed",
            "exit_code": 0,
            "started_at": timestamp,
            "finished_at": timestamp,
            "finalized_at": timestamp,
            "duration_seconds": 0.0,
            "resource_usage": {"wall_seconds": 0.0},
            "host": prelaunch["host"],
            "launch_receipt_sha256": prelaunch["receipt_sha256"],
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
        }
    )
    atomic_write_json(root / "runs" / run_id / "final.json", final)
    return service, manifest, final


def _install_eval_receipt(
    root: Path,
    *,
    measurement_state: str = "valid",
    stdout: bytes = b'{"schema_version":1,"metric":0.5,"sample_count":1}\n',
) -> dict[str, object]:
    service, descriptor_manifest, candidate_commit = (
        eval_test_support._registered_visible_run_service(root)
    )
    apparatus_commit = str(descriptor_manifest["apparatus_commit"])
    key_hash = hashlib.sha256(b"evaluation-observation").hexdigest()
    eval_id = f"EVAL-{key_hash}"
    repository = service.repository
    bundle = create_execution_bundle(
        repository,
        root / ".worktree" / "eval" / eval_id,
        candidate_commit,
        apparatus_commit,
    )
    descriptor = read_json_strict_no_repair(
        root / ".aros" / "evaluators" / "quality" / "1" / "descriptor.json"
    )
    assert isinstance(descriptor, dict)
    runs = RunService(root)
    manifest = runs.prepare_bundle(
        bundle,
        [sys.executable, "../apparatus/evaluation/score.py"],
        cwd=".",
        timeout_seconds=60,
        success_exit_codes=[0],
        idempotency_key=eval_id,
        actor="principal",
    )
    timestamp = str(manifest["created_at"])
    request: dict[str, object] = {
        "schema_version": 1,
        "eval_id": eval_id,
        "evaluator_id": "quality",
        "evaluator_version": "1",
        "descriptor_sha256": descriptor["descriptor_sha256"],
        "candidate_commit": candidate_commit,
        "apparatus_commit": apparatus_commit,
        "actor": "principal",
        "idempotency_key_sha256": key_hash,
        "created_at": timestamp,
    }
    request["request_sha256"] = record_sha256(request, "request_sha256")
    execution: dict[str, object] = {
        "schema_version": 1,
        "eval_id": eval_id,
        "request_sha256": request["request_sha256"],
        "host": socket.gethostname(),
        "broker_pid": os.getpid(),
        "broker_start_token": "linux-proc-start:1",
        "claimed_at": timestamp,
    }
    execution["execution_sha256"] = record_sha256(
        execution,
        "execution_sha256",
    )
    run_id = str(manifest["run_id"])
    execution_bundle = manifest["execution_bundle"]
    assert isinstance(execution_bundle, dict)
    run_link: dict[str, object] = {
        "schema_version": 1,
        "eval_id": eval_id,
        "request_sha256": request["request_sha256"],
        "execution_sha256": execution["execution_sha256"],
        "run_id": run_id,
        "run_manifest_sha256": manifest["manifest_sha256"],
        "bundle_sha256": execution_bundle["bundle_sha256"],
        "candidate_commit": candidate_commit,
        "apparatus_commit": apparatus_commit,
        "linked_at": timestamp,
    }
    run_link["run_link_sha256"] = record_sha256(run_link, "run_link_sha256")
    runtime = root / ".aros" / "evaluations" / eval_id
    atomic_write_json(runtime / "request.json", request)
    atomic_write_json(runtime / "execution.json", execution)
    atomic_write_json(runtime / "run.json", run_link)

    prelaunch: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": f"{run_id}-prelaunch",
        "kind": "run_prelaunch",
        "run_id": run_id,
        "actor": manifest["actor"],
        "created_at": timestamp,
        "base_commit": manifest["base_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "carrier": "tmux",
        "tmux_session": f"aros-{run_id.lower()}",
        "host": socket.gethostname(),
        "runner_version": 1,
        "runner_invocation": [
            sys.executable,
            "-m",
            "arbor.aros.runner",
            "--workspace",
            str(root),
            "--run-id",
            run_id,
        ],
    }
    prelaunch["receipt_sha256"] = record_sha256(prelaunch, "receipt_sha256")
    atomic_write_json(
        root / ".aros" / "receipts" / f"{run_id}-prelaunch.json",
        prelaunch,
    )
    run_runtime = root / ".aros" / "runs" / run_id
    (run_runtime / "stdout.log").write_bytes(stdout)
    (run_runtime / "stderr.log").write_bytes(b"")
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    final = final_identity(manifest)
    final.update(
        {
            "schema_version": 1,
            "state": "completed",
            "exit_code": 0,
            "started_at": timestamp,
            "finished_at": timestamp,
            "finalized_at": timestamp,
            "duration_seconds": 0.0,
            "resource_usage": {"wall_seconds": 0.0},
            "host": prelaunch["host"],
            "launch_receipt_sha256": prelaunch["receipt_sha256"],
            "stdout": {
                "path": f".aros/runs/{run_id}/stdout.log",
                "bytes": len(stdout),
                "sha256": hashlib.sha256(stdout).hexdigest(),
            },
            "stderr": {
                "path": f".aros/runs/{run_id}/stderr.log",
                "bytes": 0,
                "sha256": empty_sha256,
            },
        }
    )
    atomic_write_json(root / "runs" / run_id / "final.json", final)
    atomic_write_json(
        run_runtime / "status.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "final_ref": f"runs/{run_id}/final.json",
            "updated_at": timestamp,
        },
    )
    measurement = {
        "measurement_state": measurement_state,
        "metric": 0.5 if measurement_state in {"valid", "underpowered"} else None,
        "sample_count": 1 if measurement_state in {"valid", "underpowered"} else None,
        "metric_name": "quality",
        "parser": "aros.scalar-metric-v1",
    }
    receipt = build_measurement_receipt(
        request,
        execution,
        run_link,
        final,
        measurement_state,
        measurement,
        "removed",
    )
    receipt_ref = f"eval/evaluations/{eval_id}/receipt.json"
    atomic_write_json(root / receipt_ref, receipt)
    return {
        "service": service,
        "descriptor": descriptor,
        "request": request,
        "execution": execution,
        "receipt": receipt,
        "receipt_ref": receipt_ref,
        "run_id": run_id,
    }


def _snapshot_tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    snapshot: dict[str, tuple[int, bytes | None]] = {}
    paths = [root, *sorted(root.rglob("*"), key=lambda path: path.as_posix())]
    for path in paths:
        metadata = path.lstat()
        payload: bytes | None = None
        if stat.S_ISREG(metadata.st_mode):
            payload = path.read_bytes()
        elif stat.S_ISLNK(metadata.st_mode):
            payload = os.fsencode(os.readlink(path))
        relative = "." if path == root else path.relative_to(root).as_posix()
        snapshot[relative] = (metadata.st_mode, payload)
    return snapshot


def _collected_task(
    root: Path,
) -> tuple[TaskService, str, dict[str, object]]:
    service, brief, ownership, _final = task_test_support._create_terminal_task(root)
    task_id = str(brief["task_id"])
    task_test_support._commit_child_return(root, brief, ownership)
    return service, task_id, service.collect(task_id)


def test_task_collection_reader_is_side_effect_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, task_id, collected = _collected_task(tmp_path)
    before = _snapshot_tree(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pure task reader attempted a side effect")

    monkeypatch.setattr(TaskService, "__init__", forbidden)
    monkeypatch.setattr(tasks_module, "_probe_filesystem_permissions", forbidden)
    monkeypatch.setattr(tasks_module, "_ensure_durable_lock_file", forbidden)
    monkeypatch.setattr(tasks_module, "_ensure_plain_directory", forbidden)
    monkeypatch.setattr(tasks_module, "file_lock", forbidden)
    monkeypatch.setattr(tasks_module, "create_json", forbidden)
    monkeypatch.setattr(tasks_module, "atomic_write_json", forbidden)

    assert read_validated_task_collection(tmp_path, task_id) == collected
    assert _snapshot_tree(tmp_path) == before
    assert service.root == tmp_path


def test_task_collection_reader_is_independent_of_task_service_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, task_id, collected = _collected_task(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pure reader used a TaskService method")

    for method in (
        "_load_brief",
        "_load_reviewed_return",
        "_load_collected",
        "_safe_git_result",
    ):
        monkeypatch.setattr(TaskService, method, forbidden)

    assert read_validated_task_collection(tmp_path, task_id) == collected


def test_task_collection_reader_rejects_lineage_file_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, task_id, _collected = _collected_task(tmp_path)
    real_read = tasks_module._read_object
    replaced = False

    def read_then_replace(
        path: Path,
        description: str,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal replaced
        value = real_read(path, description, **kwargs)  # type: ignore[arg-type]
        if description == "task collection" and not replaced:
            replaced = True
            replacement = path.with_name("collected-replacement.json")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        return value

    monkeypatch.setattr(tasks_module, "_read_object", read_then_replace)

    with pytest.raises(TaskError, match="workspace|changed|identity"):
        read_validated_task_collection(tmp_path, task_id)


def test_run_final_reader_returns_canonical_record_hash(tmp_path: Path) -> None:
    service, manifest, final = _install_run_final(tmp_path)
    run_id = str(manifest["run_id"])
    ref = f"runs/{run_id}/final.json"
    before = _snapshot_tree(tmp_path)

    record = ObservationCatalog(tmp_path).resolve(ref)

    assert record.kind == "run_final"
    assert record.record_sha256 == json_sha256(final)
    assert record.versioned_paths == (
        f"runs/{run_id}/manifest.json",
        ref,
    )
    assert record.candidate_commit is None
    assert record.payload["run_id"] == final["run_id"]
    assert record.payload["state"] == final["state"]
    assert read_validated_run_manifest(tmp_path, run_id) == manifest
    assert read_validated_run_final(tmp_path, run_id) == service.read_validated_final(
        run_id,
        reader=read_json_strict_no_repair,
    )
    assert _snapshot_tree(tmp_path) == before


def test_malformed_run_manifest_is_reported_as_observation_error(
    tmp_path: Path,
) -> None:
    _service, manifest, _final = _install_run_final(tmp_path)
    run_id = str(manifest["run_id"])
    manifest_path = tmp_path / "runs" / run_id / "manifest.json"
    prelaunch_path = tmp_path / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
    malformed = dict(manifest)
    malformed.pop("actor")
    malformed["manifest_sha256"] = manifest_sha256(malformed)
    prelaunch = read_json_strict_no_repair(prelaunch_path)
    assert isinstance(prelaunch, dict)
    prelaunch["manifest_sha256"] = malformed["manifest_sha256"]
    prelaunch["receipt_sha256"] = record_sha256(prelaunch, "receipt_sha256")
    atomic_write_json(manifest_path, malformed)
    atomic_write_json(prelaunch_path, prelaunch)

    with pytest.raises(ObservationError, match="observation|manifest|lineage"):
        ObservationCatalog(tmp_path).resolve(f"runs/{run_id}/final.json")


@pytest.mark.parametrize("race", ("parent", "git", "lineage_file"))
def test_observation_read_rejects_workspace_identity_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    _service, manifest, _final = _install_run_final(tmp_path)
    run_id = str(manifest["run_id"])
    ref = f"runs/{run_id}/final.json"
    catalog = ObservationCatalog(tmp_path)
    real_read = observations_module.read_validated_run_manifest
    swapped = False

    def read_then_swap(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal swapped
        value = real_read(*args, **kwargs)  # type: ignore[arg-type]
        if swapped:
            return value
        swapped = True
        if race == "parent":
            directory = tmp_path / "runs" / run_id
            backing = directory.with_name(f"{run_id}-original")
            directory.rename(backing)
            shutil.copytree(backing, directory)
        elif race == "git":
            git_directory = tmp_path / ".git"
            backing = tmp_path / ".git-original"
            git_directory.rename(backing)
            shutil.copytree(backing, git_directory)
        else:
            path = tmp_path / "runs" / run_id / "manifest.json"
            replacement = path.with_name("manifest-replacement.json")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        return value

    monkeypatch.setattr(
        observations_module,
        "read_validated_run_manifest",
        read_then_swap,
    )

    with pytest.raises(ObservationError, match="binding|changed|identity"):
        catalog.resolve(ref)


@pytest.mark.parametrize("owner", ("task", "run", "eval"))
def test_enumeration_rejects_directory_replacement_during_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
) -> None:
    if owner == "task":
        _collected_task(tmp_path)
        directory = tmp_path / "tasks"
    elif owner == "run":
        _install_run_final(tmp_path)
        directory = tmp_path / "runs"
    else:
        _install_eval_receipt(tmp_path)
        directory = tmp_path / "eval" / "evaluations"
    catalog = ObservationCatalog(tmp_path)
    identity = (directory.stat().st_dev, directory.stat().st_ino)
    real_iterdir = Path.iterdir
    real_listdir = store_module.os.listdir
    replaced = False

    def replace_directory() -> None:
        nonlocal replaced
        if replaced:
            return
        replaced = True
        backing = directory.with_name(f"{directory.name}-original")
        directory.rename(backing)
        directory.mkdir()

    def swapping_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == directory:
            replace_directory()
        return real_iterdir(path)

    def swapping_listdir(path: object = ".") -> list[str]:
        if isinstance(path, int):
            metadata = os.fstat(path)
            if (metadata.st_dev, metadata.st_ino) == identity:
                replace_directory()
        return real_listdir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "iterdir", swapping_iterdir)
    monkeypatch.setattr(store_module.os, "listdir", swapping_listdir)

    with pytest.raises(ObservationError, match="changed|identity|listing"):
        catalog.enumerate_terminal()


def test_standalone_reader_anchors_root_before_repository_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, manifest, _final = _install_run_final(tmp_path)
    run_id = str(manifest["run_id"])
    real_bind = runs_module.bind_repository
    replaced = False

    def bind_then_replace(root: str | Path):  # type: ignore[no-untyped-def]
        nonlocal replaced
        binding = real_bind(root)
        if not replaced:
            replaced = True
            backing = tmp_path.with_name(f"{tmp_path.name}-original")
            tmp_path.rename(backing)
            shutil.copytree(backing, tmp_path)
        return binding

    monkeypatch.setattr(runs_module, "bind_repository", bind_then_replace)

    with pytest.raises(RunError, match="changed|identity|workspace"):
        read_validated_run_final(tmp_path, run_id)


@pytest.mark.parametrize("missing", ("run_link", "final", "output"))
def test_eval_receipt_requires_full_run_lineage(
    tmp_path: Path,
    missing: str,
) -> None:
    installed = _install_eval_receipt(tmp_path)
    eval_id = str(installed["receipt"] ["eval_id"])
    run_id = str(installed["run_id"])
    if missing == "run_link":
        (tmp_path / ".aros" / "evaluations" / eval_id / "run.json").unlink()
    elif missing == "final":
        (tmp_path / "runs" / run_id / "final.json").unlink()
    else:
        (tmp_path / ".aros" / "runs" / run_id / "stdout.log").write_bytes(
            b"tampered"
        )

    with pytest.raises(ObservationError, match="lineage|observation|Run|output"):
        ObservationCatalog(tmp_path).resolve(str(installed["receipt_ref"]))


def test_eval_receipt_measurement_is_derived_from_descriptor_and_stdout(
    tmp_path: Path,
) -> None:
    installed = _install_eval_receipt(
        tmp_path,
        measurement_state="valid",
        stdout=b"",
    )

    with pytest.raises(ObservationError, match="measurement|receipt|lineage"):
        ObservationCatalog(tmp_path).resolve(str(installed["receipt_ref"]))


def test_eval_receipt_accepts_real_descriptor_and_matching_stdout(
    tmp_path: Path,
) -> None:
    installed = _install_eval_receipt(tmp_path)

    record = ObservationCatalog(tmp_path).resolve(str(installed["receipt_ref"]))

    assert record.kind == "measurement"
    assert record.payload["metric"] == 0.5
    assert record.payload["sample_count"] == 1


def test_eval_linked_run_is_not_a_second_observation(tmp_path: Path) -> None:
    installed = _install_eval_receipt(tmp_path)

    records = ObservationCatalog(tmp_path).enumerate_terminal()

    assert tuple(record.ref for record in records) == (installed["receipt_ref"],)
    assert records[0].versioned_paths == (
        installed["receipt_ref"],
        f"runs/{installed['run_id']}/manifest.json",
        f"runs/{installed['run_id']}/final.json",
    )


@pytest.mark.parametrize(
    "relative",
    (
        "tasks/not-a-task/collected.json",
        "runs/not-a-run/final.json",
        "eval/evaluations/not-an-eval/receipt.json",
    ),
)
def test_enumeration_rejects_invalid_identity_terminal_records(
    tmp_path: Path,
    relative: str,
) -> None:
    _init_repo(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ObservationError, match="identity|terminal|observation"):
        ObservationCatalog(tmp_path).enumerate_terminal()


def test_eval_receipt_reader_ignores_missing_mutable_run_status(
    tmp_path: Path,
) -> None:
    installed = _install_eval_receipt(tmp_path)
    status_path = (
        tmp_path / ".aros" / "runs" / str(installed["run_id"]) / "status.json"
    )
    status_path.unlink()

    record = ObservationCatalog(tmp_path).resolve(str(installed["receipt_ref"]))

    assert record.payload["receipt_sha256"] == installed["receipt"]["receipt_sha256"]
    assert not status_path.exists()


def _observation_record(
    *,
    ref: str,
    kind: str,
    candidate_commit: str,
    payload: dict[str, object],
) -> ObservationRecord:
    return ObservationRecord(
        ref=ref,
        kind=kind,  # type: ignore[arg-type]
        record_sha256="f" * 64,
        versioned_paths=(ref,),
        candidate_commit=candidate_commit,
        measurement_state="valid" if kind == "measurement" else None,
        payload=MappingProxyType(payload),
    )


def test_task_and_measurement_joint_closure_requires_same_candidate_commit() -> None:
    candidate = "a" * 40
    task = _observation_record(
        ref="tasks/TASK-20260805-joint/collected.json",
        kind="task_return",
        candidate_commit=candidate,
        payload={"child_commit": candidate},
    )
    matching = _observation_record(
        ref="eval/evaluations/EVAL-" + "1" * 64 + "/receipt.json",
        kind="measurement",
        candidate_commit=candidate,
        payload={"candidate_commit": candidate},
    )
    unrelated = _observation_record(
        ref="eval/evaluations/EVAL-" + "2" * 64 + "/receipt.json",
        kind="measurement",
        candidate_commit="b" * 40,
        payload={"candidate_commit": "b" * 40},
    )

    assert validate_task_measurement_lineage(task, matching) is None
    with pytest.raises(ObservationError, match="candidate commit"):
        validate_task_measurement_lineage(task, unrelated)

    missing = ObservationRecord(
        ref="tasks/TASK-20260805-missing/collected.json",
        kind="task_return",
        record_sha256="f" * 64,
        versioned_paths=(),
        candidate_commit=None,
        measurement_state=None,
        payload={},
    )
    missing_measurement = ObservationRecord(
        ref="eval/evaluations/EVAL-" + "3" * 64 + "/receipt.json",
        kind="measurement",
        record_sha256="f" * 64,
        versioned_paths=(),
        candidate_commit=None,
        measurement_state="valid",
        payload={},
    )
    with pytest.raises(ObservationError, match="candidate commit"):
        validate_task_measurement_lineage(missing, missing_measurement)


def test_observation_record_payload_is_deeply_immutable() -> None:
    source = {"nested": {"values": [1, 2]}}
    record = ObservationRecord(
        ref="runs/RUN-immutable/final.json",
        kind="run_final",
        record_sha256="f" * 64,
        versioned_paths=("runs/RUN-immutable/final.json",),
        candidate_commit=None,
        measurement_state=None,
        payload=source,
    )

    source["nested"]["values"].append(3)  # type: ignore[index,union-attr]

    assert record.payload["nested"]["values"] == (1, 2)  # type: ignore[index]
    with pytest.raises(TypeError):
        record.payload["nested"]["extra"] = True  # type: ignore[index]
    with pytest.raises(AttributeError):
        record.payload["nested"]["values"].append(4)  # type: ignore[index,union-attr]
    assert record.record_sha256 == "f" * 64


def test_invalid_or_lost_eval_cannot_resolve_as_measurement(tmp_path: Path) -> None:
    installed = _install_eval_receipt(
        tmp_path,
        measurement_state="invalid_eval",
        stdout=b"",
    )
    catalog = ObservationCatalog(tmp_path)
    record = catalog.resolve(str(installed["receipt_ref"]))

    assert record.kind == "eval_outcome"
    assert record.measurement_state == "invalid_eval"

    (tmp_path / str(installed["receipt_ref"])).unlink()
    with pytest.raises(ObservationError, match="observation|receipt|exist"):
        catalog.resolve(str(installed["receipt_ref"]))


def test_observation_resolve_rejects_runtime_and_path_escape(tmp_path: Path) -> None:
    _service, manifest, _final = _install_run_final(tmp_path)
    run_id = str(manifest["run_id"])
    ref = f"runs/{run_id}/final.json"
    catalog = ObservationCatalog(tmp_path)

    for unsafe in (
        f".aros/runs/{run_id}/status.json",
        f"../{ref}",
        f"/{ref}",
        f"runs/{run_id}/../{run_id}/final.json",
        ref.replace("/", "\\"),
    ):
        with pytest.raises(ObservationError, match="reference|unsupported|path"):
            catalog.resolve(unsafe)

    run_directory = tmp_path / "runs" / run_id
    backing = run_directory.with_name("run-backing")
    run_directory.rename(backing)
    run_directory.symlink_to(backing.name, target_is_directory=True)
    with pytest.raises(ObservationError, match="symlink|identity|path"):
        catalog.resolve(ref)


def test_extracted_task_reader_matches_existing_service_collection(
    tmp_path: Path,
) -> None:
    service, task_id, collected = _collected_task(tmp_path)
    collection_path = tmp_path / "tasks" / task_id / "collected.json"
    before = collection_path.read_bytes()

    assert read_validated_task_collection(tmp_path, task_id) == collected
    assert service.collect(task_id) == collected
    assert collection_path.read_bytes() == before


def test_extracted_eval_reader_matches_existing_service_without_writes(
    tmp_path: Path,
) -> None:
    installed = _install_eval_receipt(tmp_path)
    service = installed["service"]
    assert isinstance(service, EvalService)
    request = installed["request"]
    execution = installed["execution"]
    assert isinstance(request, dict) and isinstance(execution, dict)
    before = _snapshot_tree(tmp_path)

    expected = service._load_receipt(
        request,
        execution,
        reconcile_run=False,
        reader=read_json_strict_no_repair,
    )

    assert read_validated_eval_receipt(
        tmp_path,
        str(installed["receipt"] ["eval_id"]),
    ) == expected
    assert _snapshot_tree(tmp_path) == before
