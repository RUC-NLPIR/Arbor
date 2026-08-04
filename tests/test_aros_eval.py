"""Visible AROS evaluation registration and one-attempt request tests."""

from __future__ import annotations

import hashlib
import json
import os
import select
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
from arbor.aros.eval_records import build_measurement_receipt, validate_measurement_receipt
from arbor.aros.receipts import record_sha256
from arbor.aros.runs import RunError, RunService
from arbor.aros.store import (
    atomic_write_json,
    final_identity,
    json_sha256,
    manifest_sha256,
    process_start_token,
)


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
    root: Path,
    request: dict[str, object],
    execution: dict[str, object],
) -> dict[str, object]:
    run_id = f"RUN-visible-{str(request['eval_id'])[-12:]}"
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    portable = {
        "candidate": {
            "path": "candidate",
            "commit": request["candidate_commit"],
            "tree": _git(root, "rev-parse", f"{request['candidate_commit']}^{{tree}}"),
        },
        "apparatus": {
            "path": "apparatus",
            "commit": request["apparatus_commit"],
            "tree": _git(root, "rev-parse", f"{request['apparatus_commit']}^{{tree}}"),
        },
        "temp": "tmp",
    }
    bundle_sha256 = json_sha256(portable)
    execution_bundle = {**portable, "bundle_sha256": bundle_sha256}
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "repository_ref": f".worktree/eval/{request['eval_id']}",
        "base_commit": request["candidate_commit"],
        "candidate_commit": request["candidate_commit"],
        "argv": [sys.executable, "../apparatus/evaluation/score.py"],
        "cwd": "candidate",
        "timeout_seconds": 300,
        "idempotency_key": request["eval_id"],
        "security_profile": "isolated-linux",
        "writable_paths": ["tmp"],
        "network_policy": "none",
        "process_policy": "isolated",
        "environment_policy": {"kind": "empty"},
        "isolation_limits": {},
        "environment_ref": {"kind": "test"},
        "environment_sha256": "0" * 64,
        "actor": request["actor"],
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
        "created_at": execution["claimed_at"],
        "execution_bundle": execution_bundle,
    }
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    atomic_write_json(root / "runs" / run_id / "manifest.json", manifest)
    run_link: dict[str, object] = {
        "schema_version": 1,
        "eval_id": request["eval_id"],
        "request_sha256": request["request_sha256"],
        "execution_sha256": execution["execution_sha256"],
        "run_id": run_id,
        "run_manifest_sha256": manifest["manifest_sha256"],
        "bundle_sha256": bundle_sha256,
        "candidate_commit": request["candidate_commit"],
        "apparatus_commit": request["apparatus_commit"],
        "linked_at": execution["claimed_at"],
    }
    run_link["run_link_sha256"] = record_sha256(run_link, "run_link_sha256")
    atomic_write_json(
        root / ".aros" / "evaluations" / str(request["eval_id"]) / "run.json",
        run_link,
    )
    prelaunch: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": f"{run_id}-prelaunch",
        "kind": "run_prelaunch",
        "run_id": run_id,
        "actor": request["actor"],
        "created_at": execution["claimed_at"],
        "base_commit": manifest["base_commit"],
        "manifest_sha256": manifest["manifest_sha256"],
        "carrier": "tmux",
        "tmux_session": f"aros-{run_id.lower()}",
        "host": eval_module.socket.gethostname(),
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
    final = final_identity(manifest)
    final.update(
        {
            "schema_version": 1,
            "state": "completed",
            "exit_code": 0,
            "started_at": execution["claimed_at"],
            "finished_at": execution["claimed_at"],
            "finalized_at": execution["claimed_at"],
            "duration_seconds": 0.0,
            "resource_usage": {"wall_seconds": 0.0},
            "host": prelaunch["host"],
            "actual_environment_sha256": "0" * 64,
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
    atomic_write_json(
        root / ".aros" / "runs" / run_id / "status.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "state": "completed",
            "manifest_sha256": manifest["manifest_sha256"],
            "actor": prelaunch["actor"],
            "carrier": "tmux",
            "tmux_session": prelaunch["tmux_session"],
            "host": prelaunch["host"],
            "launch_receipt_sha256": prelaunch["receipt_sha256"],
            "launched_at": execution["claimed_at"],
            "finished_at": execution["claimed_at"],
            "final_ref": f"runs/{run_id}/final.json",
            "updated_at": execution["claimed_at"],
        },
    )
    return build_measurement_receipt(
        request,
        execution,
        run_link,
        final,
        "valid",
        {
            "measurement_state": "valid",
            "metric": 0.5,
            "sample_count": 1,
            "metric_name": "quality",
            "parser": "aros.scalar-metric-v1",
        },
        "removed",
    )


def _registered_visible_run_service(
    root: Path,
    *,
    minimum: int | float = 0,
    minimum_samples: int = 1,
) -> tuple[EvalService, dict[str, object], str]:
    manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    manifest["metric_output"]["minimum"] = minimum  # type: ignore[index]
    manifest["metric_output"]["minimum_samples"] = minimum_samples  # type: ignore[index]
    manifest_path = root / "eval" / "suites" / "quality" / "1" / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "eval/suites/quality/1/manifest.json")
    _git(root, "commit", "-qm", "configure visible evaluation run")
    candidate_commit = _git(root, "rev-parse", "HEAD")
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    return service, manifest, candidate_commit


def _install_terminal_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str,
    stdout: bytes,
    stderr: bytes = b"",
) -> list[str]:
    starts: list[str] = []

    def terminal_start(
        service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        del actor
        starts.append(run_id)
        manifest = json.loads(
            (root / "runs" / run_id / "manifest.json").read_text(encoding="utf-8")
        )
        runtime = root / ".aros" / "runs" / run_id
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "stdout.log").write_bytes(stdout)
        (runtime / "stderr.log").write_bytes(stderr)
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
            "host": eval_module.socket.gethostname(),
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
        prelaunch["receipt_sha256"] = record_sha256(
            prelaunch,
            "receipt_sha256",
        )
        atomic_write_json(
            root / ".aros" / "receipts" / f"{run_id}-prelaunch.json",
            prelaunch,
        )
        final = final_identity(manifest)
        final.update(
            {
                "schema_version": 1,
                "state": state,
                "exit_code": 0 if state == "completed" else 1,
                "started_at": launched_at,
                "finished_at": manifest["created_at"],
                "finalized_at": manifest["created_at"],
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
                    "bytes": len(stderr),
                    "sha256": hashlib.sha256(stderr).hexdigest(),
                },
            }
        )
        atomic_write_json(root / "runs" / run_id / "final.json", final)
        status_path = runtime / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.update(
            {
                "state": state,
                "exit_code": final["exit_code"],
                "actor": prelaunch["actor"],
                "carrier": "tmux",
                "tmux_session": prelaunch["tmux_session"],
                "host": prelaunch["host"],
                "finished_at": final["finished_at"],
                "final_ref": f"runs/{run_id}/final.json",
                "launch_receipt_sha256": prelaunch["receipt_sha256"],
                "launched_at": launched_at,
                "updated_at": final["finished_at"],
            }
        )
        atomic_write_json(status_path, status)
        return {
            "run_id": run_id,
            "state": state,
            "final_ref": f"runs/{run_id}/final.json",
        }

    monkeypatch.setattr(RunService, "start", terminal_start)
    return starts


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


def _install_json_crash_alias(path: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(path.name)).hexdigest()
    alias = path.parent / f".aros-json-{digest}.inspection-crash.tmp"
    os.link(path, alias, follow_symlinks=False)
    return alias


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
def test_execution_lease_lock_remains_contended_until_fd_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "close-linearization"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    lease_fd = lease.lock_fd
    lock_path = (
        root
        / ".aros"
        / "locks"
        / f"{lease.request['eval_id']}-execution.lock"
    )
    ready_reader, ready_writer = os.pipe()
    probe_reader, probe_writer = os.pipe()
    outcome_reader, outcome_writer = os.pipe()
    release_reader, release_writer = os.pipe()
    holder_pid = os.fork()
    if holder_pid == 0:
        try:
            os.close(ready_reader)
            os.close(probe_writer)
            os.close(outcome_reader)
            os.close(release_writer)
            os.close(lease_fd)
            lock_fd = os.open(lock_path, os.O_RDWR)
            os.write(ready_writer, b"1")
            os.read(probe_reader, 1)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.write(outcome_writer, b"B")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                os.write(outcome_writer, b"A")
            else:
                os.write(outcome_writer, b"A")
            os.read(release_reader, 1)
            os.close(lock_fd)
        except BaseException:
            os._exit(91)
        os._exit(0)
    os.close(ready_writer)
    os.close(probe_reader)
    os.close(outcome_writer)
    os.close(release_reader)
    assert os.read(ready_reader, 1) == b"1"
    real_close = eval_module.os.close
    close_entered = threading.Event()
    allow_close = threading.Event()

    def pause_lease_close(descriptor: int) -> None:
        if descriptor == lease_fd:
            close_entered.set()
            assert allow_close.wait(timeout=5)
        real_close(descriptor)

    monkeypatch.setattr(eval_module.os, "close", pause_lease_close)
    close_future = None
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        close_future = pool.submit(lease.close)
        assert close_entered.wait(timeout=5)
        os.write(probe_writer, b"1")
        assert os.read(outcome_reader, 1) == b"B"
        allow_close.set()
        close_future.result(timeout=5)
        assert os.read(outcome_reader, 1) == b"A"
        replay = service._begin_execution(
            "quality",
            "1",
            candidate_commit,
            "principal",
            key,
        )
        assert isinstance(replay, eval_module.ExistingEvaluation)
        assert replay.status["evaluation_state"] == "lost"
    finally:
        allow_close.set()
        if close_future is not None:
            close_future.result(timeout=5)
        pool.shutdown(wait=True)
        os.write(release_writer, b"1")
        os.close(release_writer)
        os.close(probe_writer)
        os.close(outcome_reader)
        os.close(ready_reader)
        _, holder_status = os.waitpid(holder_pid, 0)

    assert os.waitstatus_to_exitcode(holder_status) == 0


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
def test_cross_process_follower_waits_through_request_to_claim_window(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "cross-process-request-claim-window"
    request_ready_reader, request_ready_writer = os.pipe()
    request_release_reader, request_release_writer = os.pipe()
    claim_ready_reader, claim_ready_writer = os.pipe()
    winner_finish_reader, winner_finish_writer = os.pipe()
    follower_started_reader, follower_started_writer = os.pipe()
    follower_result_reader, follower_result_writer = os.pipe()
    winner_pid = os.fork()
    if winner_pid == 0:
        try:
            real_create_json = eval_module.create_json

            def pause_after_request(path: str | Path, value: object) -> bool:
                created = real_create_json(path, value)
                if Path(path).name == "request.json" and created:
                    os.write(request_ready_writer, b"1")
                    os.read(request_release_reader, 1)
                return created

            eval_module.create_json = pause_after_request
            winner = EvalService(root)._begin_execution(
                "quality",
                "1",
                candidate_commit,
                "principal",
                key,
            )
            if not isinstance(winner, eval_module.ExecutionLease):
                os._exit(92)
            os.write(claim_ready_writer, b"1")
            os.read(winner_finish_reader, 1)
            winner.close()
        except BaseException:
            os._exit(91)
        os._exit(0)
    assert os.read(request_ready_reader, 1) == b"1"
    follower_pid = os.fork()
    if follower_pid == 0:
        try:
            real_flock = eval_module.fcntl.flock
            signalled_flock = False

            def signal_idempotency_flock(descriptor: int, operation: int) -> object:
                nonlocal signalled_flock
                if not signalled_flock and operation == fcntl.LOCK_EX:
                    signalled_flock = True
                    os.write(follower_started_writer, b"F")
                return real_flock(descriptor, operation)

            eval_module.fcntl.flock = signal_idempotency_flock
            os.write(follower_started_writer, b"S")
            follower = EvalService(root)._begin_execution(
                "quality",
                "1",
                candidate_commit,
                "principal",
                key,
            )
            outcome = (
                b"R"
                if isinstance(follower, eval_module.ExistingEvaluation)
                and follower.status["evaluation_state"] == "running"
                else b"X"
            )
            os.write(follower_result_writer, outcome)
        except BaseException:
            os.write(follower_result_writer, b"E")
            os._exit(91)
        os._exit(0)
    assert os.read(follower_started_reader, 1) == b"S"
    try:
        flock_ready, _, _ = select.select([follower_started_reader], [], [], 5)
        assert flock_ready == [follower_started_reader]
        assert os.read(follower_started_reader, 1) == b"F"
        ready, _, _ = select.select([follower_result_reader], [], [], 0.25)
        assert ready == []
        os.write(request_release_writer, b"1")
        assert os.read(claim_ready_reader, 1) == b"1"
        assert os.read(follower_result_reader, 1) == b"R"
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        evaluation_root = root / ".aros" / "evaluations" / f"EVAL-{key_digest}"
        assert sorted(path.name for path in evaluation_root.iterdir()) == [
            "execution.json",
            "request.json",
        ]
    finally:
        for descriptor in (request_release_writer, winner_finish_writer):
            try:
                os.write(descriptor, b"1")
            except OSError:
                pass
        _, follower_status = os.waitpid(follower_pid, 0)
        _, winner_status = os.waitpid(winner_pid, 0)
        for descriptor in (
            request_ready_reader,
            request_ready_writer,
            request_release_reader,
            request_release_writer,
            claim_ready_reader,
            claim_ready_writer,
            winner_finish_reader,
            winner_finish_writer,
            follower_started_reader,
            follower_started_writer,
            follower_result_reader,
            follower_result_writer,
        ):
            os.close(descriptor)

    assert os.waitstatus_to_exitcode(follower_status) == 0
    assert os.waitstatus_to_exitcode(winner_status) == 0


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
    receipt = _terminal_receipt(root, lease.request, lease.execution)
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
    receipt = _terminal_receipt(root, lease.request, lease.execution)
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
    receipt = _terminal_receipt(root, lease.request, lease.execution)
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
            receipt = _terminal_receipt(root, lease.request, lease.execution)
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


@requires_linux_claims
def test_visible_eval_uses_one_run_and_parses_verified_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    manifest, _apparatus_tree, _manifest_commit = _init_evaluator_repository(root)
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "ignore evaluation worktrees")
    candidate_commit = _git(root, "rev-parse", "HEAD")
    service = EvalService(root)
    service.register(
        "eval/suites/quality/1/manifest.json",
        actor="registrar",
    )
    stdout = b'{"schema_version":1,"metric":0.25,"sample_count":4}\n'
    stderr = b"diagnostic only\n"
    calls: list[tuple[object, ...]] = []

    class FactualRunService:
        def __init__(self, run_root: str | Path):
            assert Path(run_root) == root
            calls.append(("construct",))
            self.manifest: dict[str, object] | None = None
            self.status_count = 0

        def prepare_bundle(
            self,
            bundle: worktrees_module.ExecutionBundle,
            argv: list[str],
            *,
            cwd: str,
            timeout_seconds: float,
            success_exit_codes: list[int],
            idempotency_key: str,
            actor: str,
            label: str | None = None,
        ) -> dict[str, object]:
            calls.append(
                (
                    "prepare_bundle",
                    bundle,
                    argv,
                    cwd,
                    timeout_seconds,
                    success_exit_codes,
                    idempotency_key,
                    actor,
                    label,
                )
            )
            self.manifest = {
                "run_id": "RUN-visible-one",
                "manifest_sha256": "a" * 64,
                "execution_bundle": {
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
                },
            }
            return self.manifest

        def start(self, run_id: str, *, actor: str | None = None) -> dict[str, object]:
            calls.append(("start", run_id, actor))
            return {"run_id": run_id, "state": "launched"}

        def status(self, run_id: str) -> dict[str, object]:
            calls.append(("status", run_id))
            self.status_count += 1
            if self.status_count == 1:
                return {"run_id": run_id, "state": "running"}
            assert self.manifest is not None
            final = {
                "schema_version": 1,
                "run_id": run_id,
                "manifest_sha256": self.manifest["manifest_sha256"],
                "state": "completed",
                "candidate_commit": candidate_commit,
                "execution_bundle": self.manifest["execution_bundle"],
                "stdout": {
                    "path": f".aros/runs/{run_id}/stdout.log",
                    "bytes": len(stdout),
                    "sha256": hashlib.sha256(stdout).hexdigest(),
                },
                "stderr": {
                    "path": f".aros/runs/{run_id}/stderr.log",
                    "bytes": len(stderr),
                    "sha256": hashlib.sha256(stderr).hexdigest(),
                },
                "finished_at": "2026-08-04T00:00:00.000Z",
            }
            final_path = root / "runs" / run_id / "final.json"
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_text(json.dumps(final), encoding="utf-8")
            return {
                "run_id": run_id,
                "state": "completed",
                "final_ref": f"runs/{run_id}/final.json",
            }

        def read_verified_output(
            self,
            run_id: str,
            stream: str,
            max_bytes: int = 65_536,
        ) -> bytes:
            calls.append(("read_verified_output", run_id, stream, max_bytes))
            assert (root / "runs" / run_id / "final.json").is_file()
            return stdout if stream == "stdout" else stderr

        def read_validated_final(self, run_id: str) -> dict[str, object]:
            return json.loads(
                (root / "runs" / run_id / "final.json").read_text(encoding="utf-8")
            )

    monkeypatch.setattr(eval_module, "RunService", FactualRunService, raising=False)

    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-one-run",
    )

    assert receipt["evaluation_state"] == "completed"
    assert receipt["referenced_process_state"] == "completed"
    assert receipt["measurement_state"] == "valid"
    assert receipt["metric"] == 0.25
    assert receipt["sample_count"] == 4
    assert receipt["bundle_cleanup_state"] == "removed"
    prepare = [call for call in calls if call[0] == "prepare_bundle"]
    assert len(prepare) == 1
    assert prepare[0][2:6] == (
        manifest["scorer_argv"],
        manifest["scorer_cwd"],
        manifest["resource_limits"]["timeout_seconds"],
        manifest["success_exit_codes"],
    )
    assert type(prepare[0][4]) is type(manifest["resource_limits"]["timeout_seconds"])
    assert len([call for call in calls if call[0] == "start"]) == 1
    assert [call[0] for call in calls].count("read_verified_output") == 2
    assert not (root / ".worktree" / "eval" / receipt["eval_id"]).exists()
    receipt_path = root / "eval" / "evaluations" / receipt["eval_id"] / "receipt.json"
    assert validate_measurement_receipt(
        json.loads(receipt_path.read_text(encoding="utf-8"))
    ) == receipt


@requires_linux_claims
@pytest.mark.parametrize(
    (
        "case",
        "run_state",
        "stdout",
        "minimum",
        "minimum_samples",
        "expected_state",
        "expected_metric",
        "expected_samples",
    ),
    (
        (
            "underpowered",
            "completed",
            b'{"schema_version":1,"metric":0.2,"sample_count":1}\n',
            0,
            5,
            "underpowered",
            0.2,
            1,
        ),
        (
            "invalid",
            "completed",
            b"worker prose is not a metric\n",
            0,
            1,
            "invalid_eval",
            None,
            None,
        ),
        (
            "valid-negative",
            "completed",
            b'{"schema_version":1,"metric":-0.25,"sample_count":3}\n',
            -1,
            1,
            "valid",
            -0.25,
            3,
        ),
        (
            "failed-process",
            "failed_process",
            b'{"schema_version":1,"metric":0.9,"sample_count":9}\n',
            0,
            1,
            "not_available",
            None,
            None,
        ),
        (
            "timed-out",
            "timed_out",
            b"not parsed after timeout\n",
            0,
            1,
            "not_available",
            None,
            None,
        ),
        (
            "cancelled",
            "cancelled",
            b"not parsed after cancellation\n",
            0,
            1,
            "not_available",
            None,
            None,
        ),
    ),
)
def test_visible_eval_separates_failed_invalid_underpowered_and_valid_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    run_state: str,
    stdout: bytes,
    minimum: int | float,
    minimum_samples: int,
    expected_state: str,
    expected_metric: int | float | None,
    expected_samples: int | None,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(
        root,
        minimum=minimum,
        minimum_samples=minimum_samples,
    )
    starts = _install_terminal_run(
        root,
        monkeypatch,
        state=run_state,
        stdout=stdout,
    )

    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key=f"visible-{case}",
    )

    assert receipt["referenced_process_state"] == run_state
    assert receipt["measurement_state"] == expected_state
    assert receipt["metric"] == expected_metric
    assert receipt["sample_count"] == expected_samples
    assert len(starts) == 1


@requires_linux_claims
def test_visible_eval_uses_terminal_final_from_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="failed_process",
        stdout=b"",
        stderr=b"carrier launch failed\n",
    )
    terminal_start = RunService.start
    starts = 0

    def fail_after_final(
        run_service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        nonlocal starts
        starts += 1
        terminal_start(run_service, run_id, actor=actor)
        raise RunError("tmux launch failed after writing final")

    monkeypatch.setattr(RunService, "start", fail_after_final)

    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-terminal-start-failure",
    )

    assert starts == 1
    assert receipt["referenced_process_state"] == "failed_process"
    assert receipt["measurement_state"] == "not_available"


@requires_linux_claims
def test_run_lost_makes_eval_lost_without_receipt_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    starts: list[str] = []

    def lost_start(
        _service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        del actor
        starts.append(run_id)
        status_path = root / ".aros" / "runs" / run_id / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.update(
            {
                "state": "lost",
                "reason": "process_absent_without_final_receipt",
                "updated_at": "2026-08-04T00:00:00.000Z",
            }
        )
        atomic_write_json(status_path, status)
        return {
            "run_id": run_id,
            "state": "lost",
            "reason": "process_absent_without_final_receipt",
            "updated_at": "2026-08-04T00:00:00.000Z",
        }

    monkeypatch.setattr(RunService, "start", lost_start)

    result = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-run-lost",
    )

    assert isinstance(result, eval_module.ExistingEvaluation)
    assert result.status == {
        "eval_id": result.status["eval_id"],
        "evaluation_state": "lost",
        "referenced_process_state": "lost",
        "measurement_state": "not_available",
        "run_id": starts[0],
        "receipt_ref": None,
        "reason": "process_absent_without_final_receipt",
        "updated_at": "2026-08-04T00:00:00.000Z",
    }
    assert len(starts) == 1
    assert not (
        root / "eval" / "evaluations" / str(result.status["eval_id"]) / "receipt.json"
    ).exists()
    assert (root / ".worktree" / "eval" / str(result.status["eval_id"])).is_dir()

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("lost evaluation replay must have no side effects")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    monkeypatch.setattr(service, "_publish_visible_receipt", forbidden_side_effect)

    replay = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-run-lost",
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert replay.status["referenced_process_state"] == "lost"
    assert replay.status["run_id"] == starts[0]
    assert len(starts) == 1


@requires_linux_claims
def test_same_lost_key_never_prepares_starts_attaches_or_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    prepared: list[str] = []
    real_prepare = RunService.prepare_bundle

    def counting_prepare(
        run_service: RunService,
        bundle: worktrees_module.ExecutionBundle,
        argv: list[str],
        **kwargs: object,
    ) -> dict[str, object]:
        manifest = real_prepare(run_service, bundle, argv, **kwargs)  # type: ignore[arg-type]
        prepared.append(str(manifest["run_id"]))
        return manifest

    def crash_before_start(
        _service: RunService,
        _run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        del actor
        raise RuntimeError("injected crash before Run start")

    monkeypatch.setattr(RunService, "prepare_bundle", counting_prepare)
    monkeypatch.setattr(RunService, "start", crash_before_start)
    with pytest.raises(RuntimeError, match="before Run start"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key="visible-linked-prepared-loss",
        )

    assert len(prepared) == 1
    run_id = prepared[0]
    eval_id = "EVAL-" + hashlib.sha256(
        b"visible-linked-prepared-loss"
    ).hexdigest()
    link_path = root / ".aros" / "evaluations" / eval_id / "run.json"
    assert link_path.is_file()
    assert RunService(root).status(run_id, reconcile=False)["state"] == "prepared"

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("lost evaluation must never resume its attempt")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(service, "_publish_visible_receipt", forbidden_side_effect)

    replay = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-linked-prepared-loss",
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert replay.status["referenced_process_state"] == "prepared"
    assert replay.status["run_id"] == run_id
    assert len(list((root / "runs").glob("RUN-*/manifest.json"))) == 1
    assert (root / ".worktree" / "eval" / eval_id).is_dir()
    assert not (root / "eval" / "evaluations" / eval_id / "receipt.json").exists()


@requires_linux_claims
def test_broker_loss_after_run_final_never_reconstructs_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    starts = _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.8,"sample_count":8}\n',
    )

    def crash_before_finalization(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected broker loss after Run final")

    monkeypatch.setattr(
        service,
        "_publish_visible_receipt",
        crash_before_finalization,
    )
    with pytest.raises(RuntimeError, match="after Run final"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key="visible-final-broker-loss",
        )

    assert len(starts) == 1
    run_id = starts[0]
    eval_id = "EVAL-" + hashlib.sha256(b"visible-final-broker-loss").hexdigest()
    assert (root / "runs" / run_id / "final.json").is_file()
    assert not (root / "eval" / "evaluations" / eval_id / "receipt.json").exists()

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("released broker must never reconstruct measurement")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    monkeypatch.setattr(RunService, "read_verified_output", forbidden_side_effect)
    monkeypatch.setattr(eval_module, "parse_scalar_metric", forbidden_side_effect)
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(service, "_publish_visible_receipt", forbidden_side_effect)

    replay = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-final-broker-loss",
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert replay.status["referenced_process_state"] == "completed"
    assert replay.status["measurement_state"] == "not_available"
    assert replay.status["run_id"] == run_id
    assert not (root / "eval" / "evaluations" / eval_id / "receipt.json").exists()


@requires_linux_claims
def test_receipt_replay_requires_full_run_link_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.6,"sample_count":6}\n',
    )
    key = "visible-receipt-run-link-lineage"
    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key=key,
    )
    assert receipt["measurement_state"] == "valid"
    eval_id = str(receipt["eval_id"])
    link_path = root / ".aros" / "evaluations" / eval_id / "run.json"
    run_link = json.loads(link_path.read_text(encoding="utf-8"))
    run_link["bundle_sha256"] = "0" * 64
    run_link["run_link_sha256"] = record_sha256(
        run_link,
        "run_link_sha256",
    )
    atomic_write_json(link_path, run_link)

    with pytest.raises(EvalError, match="Run link|lineage"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )


@requires_linux_claims
@pytest.mark.parametrize("tamper", ("bogus", "completed-no-final"))
def test_linked_run_status_rejects_bogus_or_terminal_without_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    key = f"visible-linked-status-{tamper}"

    def crash_before_start(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected pre-start crash")

    monkeypatch.setattr(RunService, "start", crash_before_start)
    with pytest.raises(RuntimeError, match="pre-start"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )
    eval_id = "EVAL-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    evaluation_root = root / ".aros" / "evaluations" / eval_id
    request = json.loads((evaluation_root / "request.json").read_text(encoding="utf-8"))
    run_link = json.loads((evaluation_root / "run.json").read_text(encoding="utf-8"))
    run_id = str(run_link["run_id"])
    status_path = root / ".aros" / "runs" / run_id / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["state"] = "bogus" if tamper == "bogus" else "completed"
    if tamper == "completed-no-final":
        status["final_ref"] = f"runs/{run_id}/final.json"
        status["finished_at"] = status["updated_at"]
    atomic_write_json(status_path, status)

    with pytest.raises(EvalError, match="linked Run|lineage|state|final"):
        service._linked_run_status(request, run_link)


@requires_linux_claims
def test_linked_run_status_rejects_wrong_final_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.5,"sample_count":5}\n',
    )

    def crash_before_measurement(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected pre-measurement crash")

    monkeypatch.setattr(
        service,
        "_publish_visible_receipt",
        crash_before_measurement,
    )
    key = "visible-linked-wrong-final-state"
    with pytest.raises(RuntimeError, match="pre-measurement"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )
    eval_id = "EVAL-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    evaluation_root = root / ".aros" / "evaluations" / eval_id
    request = json.loads((evaluation_root / "request.json").read_text(encoding="utf-8"))
    run_link = json.loads((evaluation_root / "run.json").read_text(encoding="utf-8"))
    run_id = str(run_link["run_id"])
    status_path = root / ".aros" / "runs" / run_id / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["state"] = "failed_process"
    atomic_write_json(status_path, status)

    with pytest.raises(EvalError, match="linked Run|lineage|state|final"):
        service._linked_run_status(request, run_link)


@requires_linux_claims
@pytest.mark.parametrize(
    ("run_state", "measurement_state"),
    (("completed", "invalid_eval"), ("failed_process", "not_available")),
)
@pytest.mark.parametrize("bundle_drift", ("dirty", "ambiguous"))
def test_visible_eval_removes_exact_clean_bundle_and_preserves_dirty_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_state: str,
    measurement_state: str,
    bundle_drift: str,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state=run_state,
        stdout=b'{"schema_version":1,"metric":0.7,"sample_count":7}\n',
    )
    terminal_start = RunService.start

    def dirty_after_final(
        run_service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        status = terminal_start(run_service, run_id, actor=actor)
        run_manifest = json.loads(
            (root / "runs" / run_id / "manifest.json").read_text(encoding="utf-8")
        )
        bundle_root = root / str(run_manifest["repository_ref"])
        if bundle_drift == "dirty":
            (bundle_root / "candidate" / "untracked-result.txt").write_text(
                "preserve this dirty evaluation evidence\n",
                encoding="utf-8",
            )
        else:
            marker = bundle_root / "candidate" / ".git"
            saved = marker.with_name(".git.saved")
            marker.rename(saved)
            marker.symlink_to(saved.name)
        return status

    monkeypatch.setattr(RunService, "start", dirty_after_final)

    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key=f"visible-{bundle_drift}-bundle-{run_state}",
    )

    eval_id = str(receipt["eval_id"])
    bundle_root = root / ".worktree" / "eval" / eval_id
    assert receipt["referenced_process_state"] == run_state
    assert receipt["measurement_state"] == measurement_state
    assert receipt["metric"] is None
    assert receipt["sample_count"] is None
    assert receipt["bundle_cleanup_state"] == "preserved"
    if bundle_drift == "dirty":
        assert (bundle_root / "candidate" / "untracked-result.txt").is_file()
    else:
        assert (bundle_root / "candidate" / ".git").is_symlink()
        assert (bundle_root / "candidate" / ".git.saved").is_file()
    assert (bundle_root / "apparatus").is_dir()


@requires_linux_claims
def test_partial_bundle_removal_without_receipt_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    starts = _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.4,"sample_count":4}\n',
    )
    real_remove_checkout = worktrees_module.remove_clean_checkout
    removals = 0

    def remove_candidate_then_fail(
        repository: worktrees_module.RepositoryBinding,
        checkout: worktrees_module.CheckoutBinding,
    ) -> bool:
        nonlocal removals
        removals += 1
        if removals == 1:
            return real_remove_checkout(repository, checkout)
        raise worktrees_module.WorktreeError("injected apparatus removal failure")

    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_checkout",
        remove_candidate_then_fail,
    )
    key = "visible-partial-cleanup-loss"
    with pytest.raises(
        worktrees_module.BundleRemovalError,
        match="removal failed",
    ) as failure:
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )

    assert len(starts) == 1
    eval_id = "EVAL-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    bundle_root = root / ".worktree" / "eval" / eval_id
    assert not (bundle_root / "candidate").exists()
    assert (bundle_root / "apparatus").is_dir()
    assert failure.value.removed == (bundle_root / "candidate",)
    assert failure.value.remaining == (bundle_root / "apparatus",)
    registrations = _git(root, "worktree", "list", "--porcelain")
    assert str(bundle_root / "candidate") not in registrations
    assert str(bundle_root / "apparatus") in registrations
    assert not (root / "eval" / "evaluations" / eval_id / "receipt.json").exists()

    def forbidden_cleanup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("lost replay must never resume partial cleanup")

    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_cleanup,
    )
    replay = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key=key,
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    assert replay.status["evaluation_state"] == "lost"
    assert replay.status["referenced_process_state"] == "completed"
    assert replay.status["measurement_state"] == "not_available"
    assert not (root / "eval" / "evaluations" / eval_id / "receipt.json").exists()


@requires_linux_claims
def test_cleanup_ambiguity_before_first_removal_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.4,"sample_count":4}\n',
    )

    def ambiguous_before_removal(
        _repository: worktrees_module.RepositoryBinding,
        bundle: worktrees_module.ExecutionBundle,
    ) -> bool:
        raise worktrees_module.BundleRemovalError(
            (),
            (bundle.candidate.path, bundle.apparatus.path),
        )

    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        ambiguous_before_removal,
    )

    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-pre-removal-ambiguity",
    )

    bundle_root = root / ".worktree" / "eval" / str(receipt["eval_id"])
    assert receipt["measurement_state"] == "invalid_eval"
    assert receipt["bundle_cleanup_state"] == "preserved"
    assert (bundle_root / "candidate").is_dir()
    assert (bundle_root / "apparatus").is_dir()


@requires_linux_claims
def test_completed_output_integrity_failure_is_invalid_eval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.9,"sample_count":9}\n',
    )
    terminal_start = RunService.start

    def drift_output_after_final(
        run_service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        status = terminal_start(run_service, run_id, actor=actor)
        log = root / ".aros" / "runs" / run_id / "stdout.log"
        log.write_bytes(log.read_bytes() + b"post-final drift\n")
        return status

    monkeypatch.setattr(RunService, "start", drift_output_after_final)

    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="visible-output-integrity-failure",
    )

    assert receipt["referenced_process_state"] == "completed"
    assert receipt["measurement_state"] == "invalid_eval"
    assert receipt["metric"] is None
    assert receipt["sample_count"] is None
    assert receipt["bundle_cleanup_state"] == "removed"


@requires_linux_claims
@pytest.mark.parametrize("tamper", ("launch-lineage", "schema", "timestamp"))
def test_corrupt_run_final_never_reads_output_cleans_or_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.9,"sample_count":9}\n',
    )
    terminal_start = RunService.start

    def corrupt_after_final(
        run_service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        status = terminal_start(run_service, run_id, actor=actor)
        final_path = root / "runs" / run_id / "final.json"
        final = json.loads(final_path.read_text(encoding="utf-8"))
        if tamper == "launch-lineage":
            final["launch_receipt_sha256"] = "b" * 64
        elif tamper == "schema":
            final["schema_version"] = 2
        else:
            final["finalized_at"] = "2026-08-04T00:00:00.000Z"
            assert final["finalized_at"] != final["finished_at"]
        atomic_write_json(final_path, final)
        return status

    monkeypatch.setattr(RunService, "start", corrupt_after_final)

    def forbidden_after_invalid_final(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid Run final must stop before output or cleanup")

    monkeypatch.setattr(
        RunService,
        "read_verified_output",
        forbidden_after_invalid_final,
    )
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_after_invalid_final,
    )
    key = f"visible-corrupt-final-{tamper}"
    with pytest.raises(EvalError, match="Run final"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )

    eval_id = "EVAL-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    receipt_path = root / "eval" / "evaluations" / eval_id / "receipt.json"
    assert not receipt_path.exists()
    assert (root / ".worktree" / "eval" / eval_id).is_dir()

    with pytest.raises(EvalError, match="linked terminal Run final"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )
    assert not receipt_path.exists()


@requires_linux_claims
@pytest.mark.parametrize(
    "tamper",
    (
        "runner-invocation",
        "empty-actor",
        "empty-host",
        "missing-final-host",
        "empty-final-host",
    ),
)
def test_forged_prelaunch_provenance_never_publishes_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.9,"sample_count":9}\n',
    )
    terminal_start = RunService.start

    def forge_after_final(
        run_service: RunService,
        run_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, object]:
        status = terminal_start(run_service, run_id, actor=actor)
        prelaunch_path = root / ".aros" / "receipts" / f"{run_id}-prelaunch.json"
        status_path = root / ".aros" / "runs" / run_id / "status.json"
        final_path = root / "runs" / run_id / "final.json"
        prelaunch = json.loads(prelaunch_path.read_text(encoding="utf-8"))
        persisted_status = json.loads(status_path.read_text(encoding="utf-8"))
        final = json.loads(final_path.read_text(encoding="utf-8"))
        if tamper == "runner-invocation":
            prelaunch["runner_invocation"] = ["/forged/runner", run_id]
        elif tamper == "empty-actor":
            prelaunch["actor"] = ""
            persisted_status["actor"] = ""
        elif tamper == "empty-host":
            prelaunch["host"] = ""
            persisted_status["host"] = ""
            final.pop("host", None)
        elif tamper == "missing-final-host":
            final.pop("host", None)
        else:
            final["host"] = ""
        if tamper in {"runner-invocation", "empty-actor", "empty-host"}:
            prelaunch["receipt_sha256"] = record_sha256(
                prelaunch,
                "receipt_sha256",
            )
            persisted_status["launch_receipt_sha256"] = prelaunch["receipt_sha256"]
            final["launch_receipt_sha256"] = prelaunch["receipt_sha256"]
        atomic_write_json(prelaunch_path, prelaunch)
        atomic_write_json(status_path, persisted_status)
        atomic_write_json(final_path, final)
        return status

    monkeypatch.setattr(RunService, "start", forge_after_final)

    def forbidden_after_forgery(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("forged prelaunch must stop before output or cleanup")

    monkeypatch.setattr(RunService, "read_verified_output", forbidden_after_forgery)
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_after_forgery,
    )
    key = f"visible-forged-prelaunch-provenance-{tamper}"
    with pytest.raises(EvalError, match="Run final"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )

    eval_id = "EVAL-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    receipt_path = root / "eval" / "evaluations" / eval_id / "receipt.json"
    assert not receipt_path.exists()
    assert (root / ".worktree" / "eval" / eval_id).is_dir()

    with pytest.raises(EvalError, match="linked terminal Run final"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )
    assert not receipt_path.exists()


@requires_linux_claims
@pytest.mark.parametrize(
    "checkpoint",
    (
        "before_bundle",
        "after_bundle",
        "before_prepare",
        "after_prepare",
        "before_link",
        "after_link",
        "before_start",
        "after_start",
        "before_cleanup",
        "after_cleanup",
        "after_receipt",
    ),
)
def test_visible_eval_fault_boundaries_never_resume_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    key = f"visible-fault-{checkpoint}"
    eval_id = "EVAL-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    evaluation_root = root / ".aros" / "evaluations" / eval_id
    bundle_root = root / ".worktree" / "eval" / eval_id
    receipt_path = root / "eval" / "evaluations" / eval_id / "receipt.json"

    def injected() -> None:
        raise RuntimeError(f"injected {checkpoint} crash")

    real_create_bundle = worktrees_module.create_execution_bundle
    real_prepare = RunService.prepare_bundle
    real_publish_link = service._publish_run_link
    if checkpoint == "before_bundle":
        monkeypatch.setattr(
            worktrees_module,
            "create_execution_bundle",
            lambda *_args, **_kwargs: injected(),
        )
    elif checkpoint == "after_bundle":
        def crash_after_bundle(*args: object, **kwargs: object) -> object:
            real_create_bundle(*args, **kwargs)  # type: ignore[arg-type]
            injected()

        monkeypatch.setattr(
            worktrees_module,
            "create_execution_bundle",
            crash_after_bundle,
        )
    elif checkpoint == "before_prepare":
        monkeypatch.setattr(
            RunService,
            "prepare_bundle",
            lambda *_args, **_kwargs: injected(),
        )
    elif checkpoint == "after_prepare":
        def crash_after_prepare(*args: object, **kwargs: object) -> object:
            real_prepare(*args, **kwargs)  # type: ignore[arg-type]
            injected()

        monkeypatch.setattr(RunService, "prepare_bundle", crash_after_prepare)
    elif checkpoint == "before_link":
        monkeypatch.setattr(
            service,
            "_publish_run_link",
            lambda *_args, **_kwargs: injected(),
        )
    elif checkpoint == "after_link":
        def crash_after_link(*args: object, **kwargs: object) -> object:
            real_publish_link(*args, **kwargs)  # type: ignore[arg-type]
            injected()

        monkeypatch.setattr(service, "_publish_run_link", crash_after_link)
    elif checkpoint == "before_start":
        monkeypatch.setattr(
            RunService,
            "start",
            lambda *_args, **_kwargs: injected(),
        )
    elif checkpoint == "after_start":
        def crash_after_start(
            _run_service: RunService,
            run_id: str,
            *,
            actor: str | None = None,
        ) -> object:
            del actor
            status_path = root / ".aros" / "runs" / run_id / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status.update(
                {
                    "state": "running",
                    "updated_at": "2026-08-04T00:00:00.000Z",
                }
            )
            atomic_write_json(status_path, status)
            injected()

        monkeypatch.setattr(RunService, "start", crash_after_start)
    else:
        _install_terminal_run(
            root,
            monkeypatch,
            state="completed",
            stdout=b'{"schema_version":1,"metric":0.3,"sample_count":3}\n',
        )
        if checkpoint == "before_cleanup":
            monkeypatch.setattr(
                worktrees_module,
                "remove_clean_execution_bundle",
                lambda *_args, **_kwargs: injected(),
            )
        else:
            real_create_json = eval_module.create_json

            def crash_at_receipt(path: str | Path, value: object) -> bool:
                if Path(path) == receipt_path:
                    if checkpoint == "after_receipt":
                        assert real_create_json(path, value) is True
                    injected()
                return real_create_json(path, value)

            monkeypatch.setattr(eval_module, "create_json", crash_at_receipt)

    with pytest.raises(RuntimeError, match=f"injected {checkpoint}"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )

    before_replay = {
        "bundle": bundle_root.exists(),
        "runs": sorted(str(path) for path in (root / "runs").glob("RUN-*/manifest.json")),
        "link": (evaluation_root / "run.json").exists(),
        "receipt": receipt_path.exists(),
    }
    monkeypatch.undo()

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("lost replay must never resume a faulted attempt")

    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(service, "_publish_visible_receipt", forbidden_side_effect)

    replay = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key=key,
    )

    assert isinstance(replay, eval_module.ExistingEvaluation)
    if checkpoint == "after_receipt":
        assert replay.status["evaluation_state"] == "completed"
    else:
        assert replay.status["evaluation_state"] == "lost"
        assert replay.status["measurement_state"] == "not_available"
    assert {
        "bundle": bundle_root.exists(),
        "runs": sorted(str(path) for path in (root / "runs").glob("RUN-*/manifest.json")),
        "link": (evaluation_root / "run.json").exists(),
        "receipt": receipt_path.exists(),
    } == before_replay


@requires_linux_claims
def test_status_keeps_eval_and_referenced_run_states_separate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")

    running_lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        "public-status-running",
    )
    assert isinstance(running_lease, eval_module.ExecutionLease)
    running_eval_id = str(running_lease.request["eval_id"])
    try:
        assert service.status(running_eval_id) == {
            "eval_id": running_eval_id,
            "evaluation_state": "running",
            "referenced_process_state": "prepared",
            "measurement_state": "not_available",
            "run_id": None,
            "receipt_ref": None,
            "reason": "execution claim is live",
            "updated_at": running_lease.execution["claimed_at"],
        }

        receipt = _terminal_receipt(
            root,
            running_lease.request,
            running_lease.execution,
        )
        assert service.status(running_eval_id) == {
            "eval_id": running_eval_id,
            "evaluation_state": "finalizing",
            "referenced_process_state": "completed",
            "measurement_state": "not_available",
            "run_id": receipt["run_id"],
            "receipt_ref": None,
            "reason": "execution claim is live",
            "updated_at": running_lease.execution["claimed_at"],
        }

        receipt_path = (
            root / "eval" / "evaluations" / running_eval_id / "receipt.json"
        )
        atomic_write_json(receipt_path, receipt)
        assert service.status(running_eval_id) == {
            "eval_id": running_eval_id,
            "evaluation_state": "completed",
            "referenced_process_state": "completed",
            "measurement_state": "valid",
            "run_id": receipt["run_id"],
            "receipt_ref": f"eval/evaluations/{running_eval_id}/receipt.json",
            "reason": None,
            "updated_at": receipt["finished_at"],
        }
    finally:
        running_lease.close()


@requires_linux_claims
def test_released_lock_without_receipt_is_immediately_lost(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    lost_lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        "public-status-lost",
    )
    assert isinstance(lost_lease, eval_module.ExecutionLease)
    lost_eval_id = str(lost_lease.request["eval_id"])
    lost_receipt = _terminal_receipt(
        root,
        lost_lease.request,
        lost_lease.execution,
    )
    lost_lease.close()

    assert service.status(lost_eval_id) == {
        "eval_id": lost_eval_id,
        "evaluation_state": "lost",
        "referenced_process_state": "completed",
        "measurement_state": "not_available",
        "run_id": lost_receipt["run_id"],
        "receipt_ref": None,
        "reason": "execution claim lock was released",
        "updated_at": lost_lease.execution["claimed_at"],
    }


@requires_linux_claims
def test_status_is_side_effect_free_and_never_reconciles_run(
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
        "side-effect-free-public-status",
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    receipt = _terminal_receipt(root, lease.request, lease.execution)
    eval_id = str(lease.request["eval_id"])
    lease.close()
    before = {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    real_status = RunService.status
    reconcile_arguments: list[bool] = []

    def recording_status(
        run_service: RunService,
        run_id: str,
        *,
        reconcile: bool = True,
        reader: eval_module._JsonReader | None = None,
    ) -> dict[str, object]:
        reconcile_arguments.append(reconcile)
        return real_status(
            run_service,
            run_id,
            reconcile=False,
            reader=reader,
        )

    monkeypatch.setattr(RunService, "status", recording_status)

    status = service.status(eval_id)

    assert status["evaluation_state"] == "lost"
    assert status["referenced_process_state"] == "completed"
    assert status["run_id"] == receipt["run_id"]
    assert reconcile_arguments == [False]
    assert {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    } == before


@requires_linux_claims
def test_public_status_serializes_execution_lock_probes_across_processes(
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
        "serialize-public-status-probes",
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    eval_id = str(lease.request["eval_id"])
    lease.close()

    start_reader, start_writer = os.pipe()
    outcome_reader, outcome_writer = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(start_writer)
            os.close(outcome_reader)
            assert os.read(start_reader, 1) == b"1"
            child_status = service.status(eval_id)
            os.write(
                outcome_writer,
                str(child_status["evaluation_state"]).encode("ascii"),
            )
        except BaseException:
            os._exit(91)
        os._exit(0)
    os.close(start_reader)
    os.close(outcome_writer)

    probe_held = threading.Event()
    release_probe = threading.Event()
    parent_pid = os.getpid()
    real_receipt_or_lost = service._receipt_or_lost

    def pause_parent_probe(*args: object, **kwargs: object) -> object:
        if os.getpid() == parent_pid:
            probe_held.set()
            assert release_probe.wait(5)
        return real_receipt_or_lost(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_receipt_or_lost", pause_parent_probe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            parent_status = pool.submit(service.status, eval_id)
            assert probe_held.wait(5)
            os.write(start_writer, b"1")
            readable, _, _ = select.select([outcome_reader], [], [], 0.3)
            release_probe.set()
            assert parent_status.result(timeout=5)["evaluation_state"] == "lost"
        child_outcome = os.read(outcome_reader, 16)
    finally:
        release_probe.set()
        os.close(start_writer)
        os.close(outcome_reader)
        _, child_status = os.waitpid(child_pid, 0)

    assert os.waitstatus_to_exitcode(child_status) == 0
    assert readable == []
    assert child_outcome == b"lost"


@requires_linux_claims
def test_public_status_rejects_replaced_idempotency_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    _manifest, _apparatus_tree, candidate_commit = _init_evaluator_repository(root)
    service = EvalService(root)
    service.register("eval/suites/quality/1/manifest.json", actor="registrar")
    key = "replaced-public-status-lock"
    lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        key,
    )
    assert isinstance(lease, eval_module.ExecutionLease)
    eval_id = str(lease.request["eval_id"])
    lease.close()
    key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    lock_path = root / ".aros" / "locks" / f"eval-idempotency-{key_digest}.lock"
    replacement = lock_path.with_name("replacement.lock")
    replacement.write_bytes(lock_path.read_bytes())
    replacement_inode = replacement.stat().st_ino
    real_flock = eval_module.fcntl.flock

    def replace_before_flock(descriptor: int, operation: int) -> None:
        if operation == eval_module.fcntl.LOCK_EX and replacement.exists():
            replacement.replace(lock_path)
        real_flock(descriptor, operation)

    monkeypatch.setattr(eval_module.fcntl, "flock", replace_before_flock)

    with pytest.raises(EvalError, match="idempotency lock.*changed"):
        service.status(eval_id)

    assert lock_path.stat().st_ino == replacement_inode
    assert not replacement.exists()


@requires_linux_claims
def test_observe_returns_only_requested_bounded_visible_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b"private-prefix:stdout",
        stderr=b"private-prefix:stderr",
    )
    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="public-observe-bounded-stream",
    )
    eval_id = str(receipt["eval_id"])
    run_id = str(receipt["run_id"])
    real_tail_bytes = RunService._tail_bytes
    tail_calls: list[tuple[str, str, int]] = []

    def recording_tail_bytes(
        run_service: RunService,
        linked_run_id: str,
        *,
        stream: str,
        max_bytes: int,
    ) -> bytes:
        tail_calls.append((linked_run_id, stream, max_bytes))
        return real_tail_bytes(
            run_service,
            linked_run_id,
            stream=stream,
            max_bytes=max_bytes,
        )

    monkeypatch.setattr(RunService, "_tail_bytes", recording_tail_bytes)

    assert service.observe(eval_id, stream="stdout", max_bytes=6) == "stdout"
    assert service.observe(eval_id, stream="stderr", max_bytes=6) == "stderr"
    assert tail_calls == [(run_id, "stdout", 6), (run_id, "stderr", 6)]

    for stream, max_bytes in (
        ("protected", 1),
        ("stdout", 0),
        ("stdout", -1),
        ("stdout", True),
        ("stdout", 65_537),
    ):
        with pytest.raises(EvalError):
            service.observe(eval_id, stream=stream, max_bytes=max_bytes)  # type: ignore[arg-type]
    assert tail_calls == [(run_id, "stdout", 6), (run_id, "stderr", 6)]

    stdout_path = root / ".aros" / "runs" / run_id / "stdout.log"
    stdout_path.unlink()
    assert service.observe(eval_id, stream="stdout", max_bytes=65_536) == ""
    stdout_path.write_bytes(b"invalid:\xff")
    with pytest.raises(EvalError, match="UTF-8"):
        service.observe(eval_id, stream="stdout", max_bytes=65_536)


@requires_linux_claims
def test_audit_is_exact_nonpersisted_and_side_effect_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b"x" * 65_537,
        stderr=b"visible diagnostics\n",
    )
    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key="side-effect-free-public-audit",
    )
    assert receipt["measurement_state"] == "invalid_eval"
    eval_id = str(receipt["eval_id"])
    run_id = str(receipt["run_id"])
    before = {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("audit must only validate existing authorities")

    monkeypatch.setattr(eval_module, "parse_scalar_metric", forbidden_side_effect)
    monkeypatch.setattr(eval_module, "create_json", forbidden_side_effect)
    monkeypatch.setattr(RunService, "read_verified_output", forbidden_side_effect)
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_side_effect,
    )

    audit = service.audit(eval_id)

    assert audit == {
        "schema_version": 1,
        "eval_id": eval_id,
        "valid": True,
        "checked_refs": [
            f".aros/evaluations/{eval_id}/request.json",
            ".aros/evaluators/quality/1/descriptor.json",
            f".aros/evaluations/{eval_id}/execution.json",
            f".aros/evaluations/{eval_id}/run.json",
            f"runs/{run_id}/manifest.json",
            f".aros/runs/{run_id}/status.json",
            f".aros/receipts/{run_id}-prelaunch.json",
            f"runs/{run_id}/final.json",
            f".aros/runs/{run_id}/stdout.log",
            f".aros/runs/{run_id}/stderr.log",
            f"eval/evaluations/{eval_id}/receipt.json",
        ],
        "issues": [],
    }
    assert {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    } == before


@requires_linux_claims
@pytest.mark.parametrize(
    ("authority", "issue_fragment"),
    (
        ("descriptor", "descriptor"),
        ("request", "request"),
        ("execution", "execution"),
        ("run-link", "run link"),
        ("bundle", "bundle"),
        ("run-manifest", "manifest"),
        ("run-status", "state"),
        ("run-final", "final"),
        ("stdout-log", "stdout"),
        ("receipt", "receipt"),
    ),
)
def test_audit_detects_request_run_bundle_log_or_receipt_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    issue_fragment: str,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.7,"sample_count":7}\n',
        stderr=b"visible diagnostics\n",
    )
    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key=f"audit-tamper-{authority}",
    )
    eval_id = str(receipt["eval_id"])
    run_id = str(receipt["run_id"])
    evaluation_root = root / ".aros" / "evaluations" / eval_id
    request_path = evaluation_root / "request.json"
    execution_path = evaluation_root / "execution.json"
    run_link_path = evaluation_root / "run.json"
    manifest_path = root / "runs" / run_id / "manifest.json"
    status_path = root / ".aros" / "runs" / run_id / "status.json"
    final_path = root / "runs" / run_id / "final.json"
    stdout_path = root / ".aros" / "runs" / run_id / "stdout.log"
    receipt_path = root / "eval" / "evaluations" / eval_id / "receipt.json"
    expected_ref = {
        "descriptor": ".aros/evaluators/quality/1/descriptor.json",
        "request": f".aros/evaluations/{eval_id}/request.json",
        "execution": f".aros/evaluations/{eval_id}/execution.json",
        "run-link": f".aros/evaluations/{eval_id}/run.json",
        "bundle": f"runs/{run_id}/manifest.json",
        "run-manifest": f"runs/{run_id}/manifest.json",
        "run-status": f".aros/runs/{run_id}/status.json",
        "run-final": f"runs/{run_id}/final.json",
        "stdout-log": f".aros/runs/{run_id}/stdout.log",
        "receipt": f"eval/evaluations/{eval_id}/receipt.json",
    }[authority]

    if authority == "descriptor":
        def unavailable_apparatus_tree(*_args: object, **_kwargs: object) -> str:
            raise worktrees_module.WorktreeError("apparatus commit is unavailable")

        monkeypatch.setattr(
            worktrees_module,
            "_git_text",
            unavailable_apparatus_tree,
        )
    elif authority == "stdout-log":
        original = stdout_path.read_bytes()
        stdout_path.write_bytes(b"X" + original[1:])
    else:
        target = {
            "request": request_path,
            "execution": execution_path,
            "run-link": run_link_path,
            "bundle": manifest_path,
            "run-manifest": manifest_path,
            "run-status": status_path,
            "run-final": final_path,
            "receipt": receipt_path,
        }[authority]
        value = json.loads(target.read_text(encoding="utf-8"))
        if authority == "request":
            value["actor"] = "tampered"
        elif authority == "execution":
            value["broker_pid"] = int(value["broker_pid"]) + 1
        elif authority == "run-link":
            value["linked_at"] = "2026-08-04T01:02:03.004Z"
        elif authority == "bundle":
            value["execution_bundle"]["bundle_sha256"] = "f" * 64
            value["manifest_sha256"] = manifest_sha256(value)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["manifest_sha256"] = value["manifest_sha256"]
            atomic_write_json(status_path, status)
            run_link = json.loads(run_link_path.read_text(encoding="utf-8"))
            run_link["run_manifest_sha256"] = value["manifest_sha256"]
            run_link["run_link_sha256"] = record_sha256(
                run_link,
                "run_link_sha256",
            )
            atomic_write_json(run_link_path, run_link)
        elif authority == "run-manifest":
            value["actor"] = "tampered"
        elif authority == "run-status":
            value["state"] = "tampered"
        elif authority == "run-final":
            value["host"] = ""
        else:
            value["metric"] = 0.8
        atomic_write_json(target, value)

    before = {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("audit must report tampering without side effects")

    monkeypatch.setattr(eval_module, "parse_scalar_metric", forbidden_side_effect)
    monkeypatch.setattr(eval_module, "create_json", forbidden_side_effect)
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    monkeypatch.setattr(RunService, "reconcile", forbidden_side_effect)
    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_side_effect,
    )

    audit = service.audit(eval_id)

    assert set(audit) == {
        "schema_version",
        "eval_id",
        "valid",
        "checked_refs",
        "issues",
    }
    assert audit["valid"] is False
    assert expected_ref in audit["checked_refs"]
    assert audit["issues"]
    assert all(isinstance(issue, str) for issue in audit["issues"])
    assert any(issue_fragment in issue.lower() for issue in audit["issues"])
    assert {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    } == before


@requires_linux_claims
def test_status_and_audit_never_parse_or_repair_missing_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.9,"sample_count":9}\n',
    )

    def lose_broker_before_receipt(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected broker loss before measurement receipt")

    monkeypatch.setattr(
        service,
        "_publish_visible_receipt",
        lose_broker_before_receipt,
    )
    key = "public-status-audit-missing-measurement"
    with pytest.raises(RuntimeError, match="broker loss"):
        service.run(
            "quality",
            "1",
            candidate_commit,
            actor="principal",
            idempotency_key=key,
        )
    eval_id = "EVAL-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    run_link = json.loads(
        (root / ".aros" / "evaluations" / eval_id / "run.json").read_text(
            encoding="utf-8"
        )
    )
    run_id = str(run_link["run_id"])
    receipt_path = root / "eval" / "evaluations" / eval_id / "receipt.json"
    assert not receipt_path.exists()
    live_lease = service._begin_execution(
        "quality",
        "1",
        candidate_commit,
        "principal",
        "public-audit-live-finalizing",
    )
    assert isinstance(live_lease, eval_module.ExecutionLease)
    live_receipt = _terminal_receipt(
        root,
        live_lease.request,
        live_lease.execution,
    )
    live_eval_id = str(live_lease.request["eval_id"])
    live_run_id = str(live_receipt["run_id"])
    live_runtime = root / ".aros" / "runs" / live_run_id
    (live_runtime / "stdout.log").write_bytes(b"")
    (live_runtime / "stderr.log").write_bytes(b"")
    before = {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }

    def forbidden_side_effect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("missing measurement must never be reconstructed")

    monkeypatch.setattr(eval_module, "parse_scalar_metric", forbidden_side_effect)
    monkeypatch.setattr(eval_module, "create_json", forbidden_side_effect)
    monkeypatch.setattr(RunService, "prepare_bundle", forbidden_side_effect)
    monkeypatch.setattr(RunService, "start", forbidden_side_effect)
    monkeypatch.setattr(RunService, "reconcile", forbidden_side_effect)
    monkeypatch.setattr(
        worktrees_module,
        "create_execution_bundle",
        forbidden_side_effect,
    )
    monkeypatch.setattr(
        worktrees_module,
        "remove_clean_execution_bundle",
        forbidden_side_effect,
    )

    status = service.status(eval_id)
    audit = service.audit(eval_id)
    live_status = service.status(live_eval_id)
    live_audit = service.audit(live_eval_id)

    assert status["evaluation_state"] == "lost"
    assert status["referenced_process_state"] == "completed"
    assert status["measurement_state"] == "not_available"
    assert status["run_id"] == run_id
    assert status["receipt_ref"] is None
    assert audit["valid"] is True
    assert audit["issues"] == []
    assert f"eval/evaluations/{eval_id}/receipt.json" in audit["checked_refs"]
    assert live_status["evaluation_state"] == "finalizing"
    assert live_status["referenced_process_state"] == "completed"
    assert live_audit["valid"] is True
    assert live_audit["issues"] == []
    assert not receipt_path.exists()
    assert {
        path.relative_to(root): (path.stat().st_ino, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    } == before
    live_lease.close()


@requires_linux_claims
@pytest.mark.parametrize(
    ("authority", "operations"),
    (
        ("descriptor", ("observe", "audit")),
        ("request", ("status", "observe", "audit")),
        ("execution", ("status", "observe", "audit")),
        ("run-link", ("status", "observe", "audit")),
        ("run-manifest", ("status", "observe", "audit")),
        ("run-status", ("status", "observe", "audit")),
        ("prelaunch", ("status", "observe", "audit")),
        ("run-final", ("status", "observe", "audit")),
        ("receipt", ("status", "audit")),
    ),
)
def test_public_inspection_preserves_crash_aliases_for_every_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    operations: tuple[str, ...],
) -> None:
    root = tmp_path / "repository"
    service, _manifest, candidate_commit = _registered_visible_run_service(root)
    _install_terminal_run(
        root,
        monkeypatch,
        state="completed",
        stdout=b'{"schema_version":1,"metric":0.7,"sample_count":7}\n',
    )
    receipt = service.run(
        "quality",
        "1",
        candidate_commit,
        actor="principal",
        idempotency_key=f"read-only-crash-alias-{authority}",
    )
    eval_id = str(receipt["eval_id"])
    run_id = str(receipt["run_id"])
    evaluation_root = root / ".aros" / "evaluations" / eval_id
    path = {
        "descriptor": root
        / ".aros"
        / "evaluators"
        / "quality"
        / "1"
        / "descriptor.json",
        "request": evaluation_root / "request.json",
        "execution": evaluation_root / "execution.json",
        "run-link": evaluation_root / "run.json",
        "run-manifest": root / "runs" / run_id / "manifest.json",
        "run-status": root / ".aros" / "runs" / run_id / "status.json",
        "prelaunch": root / ".aros" / "receipts" / f"{run_id}-prelaunch.json",
        "run-final": root / "runs" / run_id / "final.json",
        "receipt": root / "eval" / "evaluations" / eval_id / "receipt.json",
    }[authority]

    for operation in operations:
        alias = _install_json_crash_alias(path)
        before = {
            item.relative_to(root): (
                item.lstat().st_ino,
                item.lstat().st_nlink,
                item.read_bytes(),
            )
            for item in root.rglob("*")
            if item.is_file()
        }

        if operation == "audit":
            assert service.audit(eval_id)["valid"] is False
        else:
            with pytest.raises(EvalError):
                if operation == "status":
                    service.status(eval_id)
                else:
                    service.observe(eval_id, stream="stdout", max_bytes=1)

        assert {
            item.relative_to(root): (
                item.lstat().st_ino,
                item.lstat().st_nlink,
                item.read_bytes(),
            )
            for item in root.rglob("*")
            if item.is_file()
        } == before
        alias.unlink()
