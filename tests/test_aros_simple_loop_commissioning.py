"""Replacement deterministic AROS scientific-loop commissioning."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
PROVIDER = ROOT / "commissioning/simple_loop/provider.py"
DRIVER = ROOT / "scripts/commission_aros_simple_loop.py"
VERIFIER = ROOT / "scripts/verify_aros_simple_loop.py"


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(
    messages: list[dict[str, object]],
    response: Any,
    value: object,
) -> list[dict[str, object]]:
    call = response.get_tool_calls()[0]
    content = value if isinstance(value, str) else json.dumps(value)
    return [
        *messages,
        {"role": "assistant", "content": response.raw_content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": content,
                }
            ],
        },
    ]


def test_provider_drives_plain_checkpoint_task_eval_and_final_prose() -> None:
    provider = _module(PROVIDER, "simple_provider").SimpleLoopProvider()
    messages: list[dict[str, object]] = [{"role": "user", "content": "go"}]
    response = asyncio.run(provider.create(system="boot", messages=messages))
    calls: list[tuple[str, dict[str, object]]] = []

    def advance(value: object) -> Any:
        nonlocal messages, response
        call = response.get_tool_calls()[0]
        calls.append((call.name, call.input))
        messages = _result(messages, response, value)
        response = asyncio.run(provider.create(system="boot", messages=messages))
        return response

    advance({"unread_returns": [], "snapshot": {"candidate": {"head": "0" * 40}}})
    advance("wrote model")
    advance("wrote idea")
    prereg = response.get_tool_calls()[0]
    assert prereg.name == "Checkpoint"
    assert prereg.input == {
        "message": "Preregister deterministic mechanism and test.",
        "paths": ["ideas/I-E2E.md", "model/CURRENT.md"],
    }
    advance({"commit": "1" * 40})
    advance({"task_id": "TASK-live", "checkpoint": {"commit": "2" * 40}})
    run_id = "RUN-task-live"
    run_manifest_ref = f"runs/{run_id}/manifest.json"
    run_final_ref = f"runs/{run_id}/final.json"
    advance({"task_id": "TASK-live", "run_id": run_id, "state": "running"})
    advance({"task_id": "TASK-live", "run_id": run_id, "state": "completed"})
    advance(
        {
            "task_id": "TASK-live",
            "child_commit": "3" * 40,
            "return_commit": "4" * 40,
            "run_id": run_id,
            "run_manifest_ref": run_manifest_ref,
            "run_manifest_sha256": "8" * 64,
            "run_final_ref": run_final_ref,
            "run_final_sha256": "9" * 64,
            "collected_sha256": "a" * 64,
            "checkpoint": {"commit": "5" * 40},
        }
    )
    eval_id = "EVAL-" + "b" * 64
    advance(
        {
            "eval_id": eval_id,
            "candidate_commit": "3" * 40,
            "measurement_state": "valid",
            "metric": 1.0,
            "receipt_sha256": "c" * 64,
            "checkpoint": {"commit": "6" * 40},
        }
    )
    collected_ref = "tasks/TASK-live/collected.json"
    eval_ref = f"eval/evaluations/{eval_id}/receipt.json"
    advance(
        {
            "unread_returns": [
                {"ref": collected_ref},
                {"ref": eval_ref},
            ],
            "snapshot": {"candidate": {"head": "6" * 40}},
        }
    )
    for _ in range(5):
        advance("wrote semantic file")
    final_call = response.get_tool_calls()[0]
    assert final_call.name == "Checkpoint"
    assert final_call.input == {
        "message": "Interpret deterministic Task return and measurement.",
        "paths": [
            "ideas/I-E2E.md",
            "knowledge/claims/C-0001.md",
            "memory/NOW.md",
            "model/CURRENT.md",
            "questions/Q-0001/question.md",
        ],
    }
    calls.append((final_call.name, final_call.input))
    messages = _result(messages, response, {"commit": "7" * 40})
    final = asyncio.run(provider.create(system="boot", messages=messages))
    assert final.get_tool_calls() == []
    assert final.get_text() == "Deterministic research loop checkpointed."
    assert provider.run_id == run_id
    assert provider.run_manifest_ref == run_manifest_ref
    assert provider.run_manifest_sha256 == "8" * 64
    assert provider.run_final_ref == run_final_ref
    assert provider.run_final_sha256 == "9" * 64
    normalized = [
        (name, value.get("action")) if name in {"Task", "Eval"} else (name, None)
        for name, value in calls
    ]
    assert normalized == [
        ("Attention", None),
        ("Write", None),
        ("Write", None),
        ("Checkpoint", None),
        ("Task", "create"),
        ("Task", "start"),
        ("Task", "status"),
        ("Task", "collect"),
        ("Eval", "run"),
        ("Attention", None),
        ("Write", None),
        ("Write", None),
        ("Write", None),
        ("Write", None),
        ("Write", None),
        ("Checkpoint", None),
    ]
    assert all(name != "Run" for name, _value in calls)


def test_restart_provider_requires_no_unread_returns_and_exact_recent_refs() -> None:
    module = _module(PROVIDER, "simple_restart_provider")
    provider = module.SimpleLoopProvider(restart=True)
    messages: list[dict[str, object]] = [{"role": "user", "content": "recover"}]
    response = asyncio.run(provider.create(system="boot", messages=messages))
    task_ref = "tasks/TASK-live/collected.json"
    run_ref = "runs/RUN-task-live/final.json"
    eval_ref = "eval/evaluations/EVAL-live/receipt.json"
    messages = _result(
        messages,
        response,
        {
            "unread_returns": [],
            "recent_evidence_delta": [
                {
                    "commit": "7" * 40,
                    "observed_refs": sorted([task_ref, run_ref, eval_ref]),
                }
            ],
        },
    )

    final = asyncio.run(provider.create(system="boot", messages=messages))

    assert final.get_tool_calls() == []
    assert "Recovered deterministic research state" in final.get_text()


def test_replacement_commissioning_has_no_removed_schema_surface() -> None:
    for path in (PROVIDER, DRIVER, VERIFIER):
        source = path.read_text(encoding="utf-8")
        for removed in (
            "transition_audit",
            "proposal.json",
            "admission.json",
            "EvidenceLink",
            "OperationalIntent",
            "unassimilated_returns",
            "HumanDirectGateway",
        ):
            assert removed not in source


def test_read_only_git_environment_forces_optional_locks_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbor.aros import worktrees

    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setenv("GIT_HOSTILE_AMBIENT", "injected")

    environment = worktrees._git_environment()

    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_HOSTILE_AMBIENT" not in environment


def test_controlled_git_environment_preserves_mandatory_mutation_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbor.aros import worktrees

    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    environment = worktrees._git_environment()
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(root: Path, *args: str) -> None:
        subprocess.run(
            ["git", "--no-replace-objects", "-C", str(root), *args],
            check=True,
            capture_output=True,
            env=environment,
        )

    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Mandatory Lock Test")
    git(repository, "config", "user.email", "locks@example.invalid")
    (repository / "README.md").write_text("base\n")
    git(repository, "add", "README.md")
    git(repository, "commit", "-qm", "base")
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "-qb", "child", str(worktree))
    (worktree / "child.txt").write_text("child\n")
    git(worktree, "add", "child.txt")
    git(worktree, "commit", "-qm", "child")
    git(repository, "worktree", "remove", str(worktree))

    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert not worktree.exists()


def test_task_adapter_runtime_uses_controlled_not_private_wording() -> None:
    source = (ROOT / "src/aros/task_run.py").read_text(encoding="utf-8")

    assert "private physical directories" not in source
    assert "_private_directory" not in source


@pytest.mark.parametrize("path", [DRIVER, VERIFIER])
def test_commissioning_subprocess_environment_is_closed(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module(path, f"controlled_environment_{path.stem}")
    for key in ("LC_ALL", "LC_CTYPE", "TZ", "VIRTUAL_ENV_PROMPT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", "/controlled/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("VIRTUAL_ENV", "/controlled/venv")
    monkeypatch.setenv("PYTHONPATH", "/hostile/source")
    monkeypatch.setenv("GIT_DIR", "/hostile/git")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")

    assert hasattr(module, "_controlled_environment")
    environment = module._controlled_environment()

    assert environment == {
        "PATH": "/controlled/bin",
        "LANG": "C.UTF-8",
        "VIRTUAL_ENV": "/controlled/venv",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_verifier_git_ignores_replacement_refs(tmp_path: Path) -> None:
    verifier = _module(VERIFIER, "replacement_ref_verifier")
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.name", "Replacement Test")
    _git(project, "config", "user.email", "replacement@example.invalid")
    target = project / "value.txt"
    target.write_text("original\n", encoding="utf-8")
    _git(project, "add", "value.txt")
    _git(project, "commit", "-qm", "original")
    original = _git(project, "rev-parse", "HEAD")
    target.write_text("forged\n", encoding="utf-8")
    _git(project, "commit", "-qam", "forged")
    forged = _git(project, "rev-parse", "HEAD")
    _git(project, "replace", original, forged)

    assert verifier._git(project, "show", f"{original}:value.txt") == b"original\n"


def test_driver_cli_requires_wheel_and_source_commit() -> None:
    result = subprocess.run(
        [sys.executable, str(DRIVER), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--wheel" in result.stdout
    assert "--source-commit" in result.stdout


def test_driver_git_uses_no_replace_objects(tmp_path: Path) -> None:
    module = _module(DRIVER, "driver_no_replace_objects")
    driver = module.Driver(Path(sys.prefix) / "bin/aros", tmp_path)
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="head\n", stderr="")

    driver.run = run

    assert driver.git("rev-parse", "HEAD") == "head"
    assert calls == [
        ["git", "--no-replace-objects", "-C", str(driver.project), "rev-parse", "HEAD"]
    ]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _hash(value: dict[str, object], field: str | None = None) -> str:
    payload = dict(value)
    if field is not None:
        payload.pop(field, None)
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _file_tree(root: Path) -> tuple[list[dict[str, object]], str]:
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return entries, hashlib.sha256(raw).hexdigest()


def _record_payload(payloads: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for name, payload in sorted(payloads.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow([name, "sha256=" + digest.decode(), len(payload)])
    writer.writerow([record_name, "", ""])
    return output.getvalue().encode()


def _inventory_hash(payloads: dict[str, bytes]) -> str:
    entries = [
        {
            "path": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(payloads.items())
    ]
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _refresh_installed_inventory_hash(evidence: dict[str, object]) -> None:
    product = evidence["product"]
    assert isinstance(product, dict)
    distribution = Path(str(product["distribution_root"]))
    dist_info = Path(str(product["dist_info_root"]))
    payloads = {
        **{
            "arbor/" + str(entry["path"]): distribution.joinpath(
                str(entry["path"])
            ).read_bytes()
            for entry in _file_tree(distribution)[0]
        },
        **{
            dist_info.name + "/" + str(entry["path"]): dist_info.joinpath(
                str(entry["path"])
            ).read_bytes()
            for entry in _file_tree(dist_info)[0]
        },
        "../../../bin/aros": Path(str(product["aros_executable"])).read_bytes(),
    }
    product["installed_inventory_sha256"] = _inventory_hash(payloads)


def _rewrite_fixture_installed_record(evidence: dict[str, object]) -> None:
    product = evidence["product"]
    assert isinstance(product, dict)
    distribution = Path(str(product["distribution_root"]))
    dist_info = Path(str(product["dist_info_root"]))
    record = dist_info / "RECORD"
    payloads = {
        **{
            "arbor/" + str(entry["path"]): distribution.joinpath(
                str(entry["path"])
            ).read_bytes()
            for entry in _file_tree(distribution)[0]
        },
        **{
            dist_info.name + "/" + str(entry["path"]): dist_info.joinpath(
                str(entry["path"])
            ).read_bytes()
            for entry in _file_tree(dist_info)[0]
            if str(entry["path"]) != "RECORD"
        },
        "../../../bin/aros": Path(str(product["aros_executable"])).read_bytes(),
    }
    record.write_bytes(_record_payload(payloads, f"{dist_info.name}/RECORD"))
    _refresh_installed_inventory_hash(evidence)


def _verifier_fixture(
    root: Path,
    *,
    observed_refs: list[str] | None = None,
    wrong_manifest_hash: bool = False,
    wrong_final_hash: bool = False,
    argv_task_id: str | None = None,
    security_profile: str = "trusted-local",
    final_state: str = "completed",
    broken_base_lineage: bool = False,
    installed_task_runner: bool = False,
    installed_task_adapter: bool = True,
    receipt_eval_id: str | None = None,
    prereg_model_blob: str | None = None,
    source_mismatch_file: str | None = None,
    console_target: str = "arbor.cli.aros_app:main",
    aros_script_variant: str = "canonical",
) -> tuple[ModuleType, Path, dict[str, object]]:
    verifier = _module(VERIFIER, f"simple_verifier_{id(root)}")
    project = root / "project"
    project.mkdir(parents=True)
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.name", "Verifier Test")
    _git(project, "config", "user.email", "verifier@example.invalid")
    (project / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-qm", "base")

    provider_type = _module(PROVIDER, f"simple_fixture_provider_{id(root)}").SimpleLoopProvider
    provider = provider_type()
    prereg_model = provider._preregistered_model()
    prereg_idea = provider._preregistered_idea()
    _write_json(project / "unused.json", {})
    (project / "unused.json").unlink()
    (project / "model").mkdir()
    (project / "ideas").mkdir()
    (project / "model/CURRENT.md").write_text(
        prereg_model_blob or prereg_model,
        encoding="utf-8",
    )
    (project / "ideas/I-E2E.md").write_text(prereg_idea, encoding="utf-8")
    _git(project, "add", "model/CURRENT.md", "ideas/I-E2E.md")
    _git(project, "commit", "-qm", "Preregister deterministic mechanism and test.")
    prereg_commit = _git(project, "rev-parse", "HEAD")

    task_id = "TASK-20260807-verifier"
    run_id = "RUN-task-verifier"
    eval_run_id = "RUN-eval-verifier"
    eval_id = "EVAL-" + "b" * 64
    _git(project, "checkout", "-qb", "task-child")
    (project / "candidate-mode.txt").write_text("success\n", encoding="utf-8")
    _git(project, "add", "candidate-mode.txt")
    _git(project, "commit", "-qm", "produce deterministic candidate")
    child_commit = _git(project, "rev-parse", "HEAD")
    return_path = project / f"tasks/{task_id}/return.json"
    _write_json(return_path, {"task_id": task_id, "child_commit": child_commit})
    _git(project, "add", return_path.relative_to(project).as_posix())
    _git(project, "commit", "-qm", "record deterministic task return")
    return_commit = _git(project, "rev-parse", "HEAD")
    _git(project, "checkout", "-q", "main")

    run_manifest_ref = f"runs/{run_id}/manifest.json"
    run_final_ref = f"runs/{run_id}/final.json"
    collected_ref = f"tasks/{task_id}/collected.json"
    receipt_ref = f"eval/evaluations/{eval_id}/receipt.json"
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "argv": [
            sys.executable,
            "-B",
            "-m",
            "arbor.aros.task_adapter",
            "--workspace",
            str(project),
            "--task-id",
            argv_task_id or task_id,
        ],
        "security_profile": security_profile,
    }
    manifest["manifest_sha256"] = _hash(manifest, "manifest_sha256")
    if wrong_manifest_hash:
        manifest["manifest_sha256"] = "f" * 64
    final: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "manifest_sha256": manifest["manifest_sha256"],
        "state": final_state,
        "exit_code": 0,
    }
    collected: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "base_commit": child_commit if broken_base_lineage else prereg_commit,
        "child_commit": child_commit,
        "return_commit": return_commit,
        "final_state": final_state,
        "run_id": run_id,
        "run_manifest_ref": run_manifest_ref,
        "run_manifest_sha256": manifest["manifest_sha256"],
        "run_final_ref": run_final_ref,
        "run_final_sha256": "e" * 64 if wrong_final_hash else _hash(final),
    }
    collected["collected_sha256"] = _hash(collected, "collected_sha256")
    receipt: dict[str, object] = {
        "schema_version": 1,
        "eval_id": receipt_eval_id or eval_id,
        "run_id": eval_run_id,
        "candidate_commit": child_commit,
        "measurement_state": "valid",
        "metric": 1.0,
    }
    receipt["receipt_sha256"] = _hash(receipt, "receipt_sha256")
    for ref, value in (
        (run_manifest_ref, manifest),
        (run_final_ref, final),
        (collected_ref, collected),
        (receipt_ref, receipt),
    ):
        _write_json(project / ref, value)
    _git(project, "add", run_manifest_ref, run_final_ref, collected_ref, receipt_ref)
    _git(project, "commit", "-qm", "Record deterministic Task Run and evaluation")
    final_parent = _git(project, "rev-parse", "HEAD")

    provider.task_id = task_id
    provider.child_commit = child_commit
    provider.eval_id = eval_id
    provider.collected_ref = collected_ref
    provider.eval_ref = receipt_ref
    semantic = {
        "questions/Q-0001/question.md": provider._question(),
        "model/CURRENT.md": provider._final_model(),
        "ideas/I-E2E.md": provider._final_idea(),
        "knowledge/claims/C-0001.md": provider._claim(),
        "memory/NOW.md": provider._now(),
    }
    for path, content in semantic.items():
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(project, "add", *semantic)
    expected_refs = sorted([collected_ref, run_final_ref, receipt_ref])
    trailers = observed_refs if observed_refs is not None else expected_refs
    message = "Interpret deterministic Task return and measurement.\n\n" + "\n".join(
        f"AROS-Observed: {ref}" for ref in trailers
    )
    _git(project, "commit", "-qm", message)
    final_commit = _git(project, "rev-parse", "HEAD")

    environment_root = root / "venv"
    distribution_root = environment_root / "lib/python3.12/site-packages/arbor"
    shutil.copytree(
        ROOT / "src",
        distribution_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        ROOT / "skills",
        distribution_root / "skills_suite",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    package = distribution_root / "aros"
    if not installed_task_adapter:
        (package / "task_adapter.py").unlink()
    if installed_task_runner:
        (package / "task_runner.py").write_text("# removed\n", encoding="utf-8")
    package_entries, package_tree_sha256 = _file_tree(package)
    distribution_entries, distribution_tree_sha256 = _file_tree(distribution_root)
    distribution_version = importlib.metadata.version("arbor-agent")
    source_token = (
        distribution_version.split("+g", 1)[1].split(".", 1)[0]
        if "+g" in distribution_version
        else "HEAD"
    )
    source_commit = _git(ROOT, "rev-parse", f"{source_token}^{{commit}}")
    source_modes: dict[str, int] = {}
    for entry in distribution_entries:
        relative = str(entry["path"])
        source_path = (
            "skills/" + relative.removeprefix("skills_suite/")
            if relative.startswith("skills_suite/")
            else "src/" + relative
        )
        raw = _git(ROOT, "ls-tree", source_commit, "--", source_path).split()
        raw_mode = raw[0] if raw else "100644"
        if raw:
            (distribution_root / relative).write_bytes(
                _git_bytes(ROOT, "show", f"{source_commit}:{source_path}")
            )
        source_modes[relative] = 0o755 if raw_mode == "100755" else 0o644
        (distribution_root / relative).chmod(source_modes[relative])
    if source_mismatch_file is not None:
        (distribution_root / source_mismatch_file).write_text(
            "# differs from source commit\n"
        )
    package_entries, package_tree_sha256 = _file_tree(package)
    distribution_entries, distribution_tree_sha256 = _file_tree(distribution_root)
    wheel = root / "artifacts/arbor_agent_fixture.whl"
    wheel.parent.mkdir()
    dist_root = f"arbor_agent-{distribution_version}.dist-info"
    wheel_payloads = {
        **{
            "arbor/" + str(entry["path"]): distribution_root.joinpath(
                str(entry["path"])
            ).read_bytes()
            for entry in distribution_entries
        },
        f"{dist_root}/METADATA": (
            f"Metadata-Version: 2.1\nName: arbor-agent\nVersion: {distribution_version}\n"
        ).encode(),
        f"{dist_root}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: fixture\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode(),
        f"{dist_root}/entry_points.txt": (
            f"[console_scripts]\naros = {console_target}\n"
        ).encode(),
        f"{dist_root}/top_level.txt": b"arbor\n",
        f"{dist_root}/licenses/LICENSE": (ROOT / "LICENSE").read_bytes(),
    }
    record_name = f"{dist_root}/RECORD"
    wheel_payloads[record_name] = _record_payload(wheel_payloads, record_name)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in wheel_payloads.items():
            info = zipfile.ZipInfo(name)
            relative = name.removeprefix("arbor/")
            mode = source_modes.get(relative, 0o644)
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, payload)
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel_inventory_sha256 = _inventory_hash(wheel_payloads)
    bin_dir = environment_root / "bin"
    bin_dir.mkdir(parents=True)
    python_executable = bin_dir / "python"
    python_executable.write_text("# fake interpreter\n")
    python_executable.chmod(0o755)
    shebang_python = bin_dir / "python3"
    shebang_python.symlink_to(python_executable)
    aros_executable = bin_dir / "aros"
    script = (
        f"#!{shebang_python}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        "from arbor.cli.aros_app import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n"
    )
    if aros_script_variant == "comment_import":
        script = script.replace(
            "from arbor.cli.aros_app import main",
            "# from arbor.cli.aros_app import main",
        )
    elif aros_script_variant == "forged_call":
        script = script.replace("sys.exit(main())", "sys.exit(forged())")
    elif aros_script_variant == "side_effect":
        script = script.replace("if __name__", "print('side effect')\nif __name__")
    elif aros_script_variant == "wrong_shebang":
        script = script.replace(f"#!{shebang_python}", "#!/usr/bin/python3")
    aros_executable.write_text(script)
    aros_executable.chmod(0o755)
    aros_sha256 = hashlib.sha256(aros_executable.read_bytes()).hexdigest()
    dist_info_root = distribution_root.parent / dist_root
    for name, payload in wheel_payloads.items():
        if not name.startswith(f"{dist_root}/") or name == record_name:
            continue
        target = distribution_root.parent / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    direct_url = json.dumps(
        {
            "archive_info": {
                "hash": f"sha256={wheel_sha256}",
                "hashes": {"sha256": wheel_sha256},
            },
            "url": wheel.as_uri(),
        },
        sort_keys=True,
    ).encode() + b"\n"
    for relative, payload in {
        "INSTALLER": b"pip\n",
        "REQUESTED": b"",
        "direct_url.json": direct_url,
    }.items():
        target = dist_info_root / relative
        target.write_bytes(payload)
    installed_payloads = {
        **{
            "arbor/" + str(entry["path"]): distribution_root.joinpath(
                str(entry["path"])
            ).read_bytes()
            for entry in distribution_entries
        },
        **{
            f"{dist_root}/" + str(entry["path"]): dist_info_root.joinpath(
                str(entry["path"])
            ).read_bytes()
            for entry in _file_tree(dist_info_root)[0]
        },
    }
    record_payloads = {
        **installed_payloads,
        "../../../bin/aros": aros_executable.read_bytes(),
    }
    installed_payloads[record_name] = _record_payload(
        record_payloads,
        record_name,
    )
    (dist_info_root / "RECORD").write_bytes(installed_payloads[record_name])
    installed_inventory_sha256 = _inventory_hash(
        {**installed_payloads, "../../../bin/aros": aros_executable.read_bytes()}
    )
    if hasattr(verifier, "_runtime_environment_identity"):
        verifier._runtime_environment_identity = lambda: (
            environment_root.resolve(),
            python_executable.absolute(),
        )

    def tool(name: str, value: dict[str, object]) -> dict[str, object]:
        return {"name": name, "input": value}

    tool_uses = [
        tool("Attention", {}),
        tool("Write", {"file_path": "model/CURRENT.md", "content": prereg_model}),
        tool("Write", {"file_path": "ideas/I-E2E.md", "content": prereg_idea}),
        tool(
            "Checkpoint",
            {
                "message": "Preregister deterministic mechanism and test.",
                "paths": ["ideas/I-E2E.md", "model/CURRENT.md"],
            },
        ),
        tool(
            "Task",
            {
                "action": "create",
                "objective": "Produce the deterministic success candidate.",
                "mode": "write",
                "adapter_argv": [
                    "python3",
                    "commissioning/simple_loop/task_adapter.py",
                ],
                "capabilities": {"network": False, "shell": True},
                "deliverables": ["candidate-mode.txt"],
                "acceptance": ["candidate-mode.txt equals success"],
                "timeout_seconds": 120,
                "idempotency_key": "simple-loop-task",
            },
        ),
        tool("Task", {"action": "start", "task_id": task_id}),
        tool("Task", {"action": "status", "task_id": task_id}),
        tool("Task", {"action": "collect", "task_id": task_id}),
        tool(
            "Eval",
            {
                "action": "run",
                "evaluator_id": "simple-loop",
                "version": "1",
                "candidate_commit": child_commit,
                "idempotency_key": "simple-loop-eval",
            },
        ),
        tool("Attention", {}),
        *[
            tool("Write", {"file_path": path, "content": semantic[path]})
            for path in (
                "questions/Q-0001/question.md",
                "model/CURRENT.md",
                "ideas/I-E2E.md",
                "knowledge/claims/C-0001.md",
                "memory/NOW.md",
            )
        ],
        tool(
            "Checkpoint",
            {
                "message": "Interpret deterministic Task return and measurement.",
                "paths": sorted(semantic),
            },
        ),
    ]
    evidence: dict[str, object] = {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(project),
        "package_root": str(package),
        "product": {
            "source_commit": source_commit,
            "source_repository": str(ROOT),
            "distribution": "arbor-agent",
            "distribution_version": distribution_version,
            "wheel_ref": wheel.relative_to(root).as_posix(),
            "wheel_sha256": wheel_sha256,
            "package_root": str(package),
            "package_tree_sha256": package_tree_sha256,
            "distribution_root": str(distribution_root),
            "distribution_tree_sha256": distribution_tree_sha256,
            "aros_executable": str(aros_executable),
            "aros_executable_sha256": aros_sha256,
            "console_script": console_target,
            "python_executable": str(python_executable),
            "environment_root": str(environment_root),
            "dist_info_root": str(dist_info_root),
            "wheel_inventory_sha256": wheel_inventory_sha256,
            "installed_inventory_sha256": installed_inventory_sha256,
            "filesystem_permissions_enforced": True,
        },
        "task": {
            "task_id": task_id,
            "child_commit": child_commit,
            "return_commit": return_commit,
            "collected_ref": collected_ref,
            "collected_sha256": collected["collected_sha256"],
            "run_id": run_id,
            "run_manifest_ref": run_manifest_ref,
            "run_manifest_sha256": manifest["manifest_sha256"],
            "run_final_ref": run_final_ref,
            "run_final_sha256": collected["run_final_sha256"],
        },
        "eval": {
            "eval_id": eval_id,
            "run_id": eval_run_id,
            "candidate_commit": child_commit,
            "receipt_ref": receipt_ref,
            "receipt_sha256": receipt["receipt_sha256"],
            "metric": 1.0,
        },
        "checkpoint": {
            "preregistration_commit": prereg_commit,
            "final_parent": final_parent,
            "final_commit": final_commit,
        },
        "agent": {
            "class": "arbor.core.agent.Agent",
            "instance": 1,
            "provider_instance": 2,
            "destroyed_before_restart": True,
            "stop_reason": "finished",
            "tool_uses": tool_uses,
        },
        "restart": {
            "agent_instance": 3,
            "provider_instance": 4,
            "initial_message_count": 0,
            "stop_reason": "finished",
            "tool_uses": [tool("Attention", {})],
            "packet": {
                "unread_returns": [],
                "recent_evidence_delta": [
                    {
                        "commit": final_commit,
                        "observed_refs": expected_refs,
                        "paths": sorted(semantic),
                    }
                ],
            },
        },
        "commands": [{"returncode": 0}],
    }
    evidence_path = root / "evidence.json"
    _write_json(evidence_path, evidence)
    return verifier, evidence_path, evidence


def _agent_tool_input(
    evidence: dict[str, object],
    name: str,
    action: str | None = None,
) -> dict[str, object]:
    agent = evidence["agent"]
    assert isinstance(agent, dict)
    tools = agent["tool_uses"]
    assert isinstance(tools, list)
    matches = [
        item["input"]
        for item in tools
        if isinstance(item, dict)
        and item.get("name") == name
        and isinstance(item.get("input"), dict)
        and (action is None or item["input"].get("action") == action)
    ]
    assert len(matches) == 1
    value = matches[0]
    assert isinstance(value, dict)
    return value


def test_verifier_accepts_exact_task_owned_run_evidence(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)

    result = verifier.verify(evidence_path)

    assert result == {
        "schema_version": 1,
        "state": "verified",
        "enforcement_class": "cooperative",
        "commit": evidence["checkpoint"]["final_commit"],  # type: ignore[index]
        "task_id": evidence["task"]["task_id"],  # type: ignore[index]
        "eval_id": evidence["eval"]["eval_id"],  # type: ignore[index]
    }


def test_verifier_rejects_foreign_task_id_in_agent_call(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    _agent_tool_input(evidence, "Task", "start")["task_id"] = (
        "TASK-20260807-foreign"
    )
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="Task.*identity|task_id"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize("attention_index", [0, 1])
def test_verifier_rejects_nonempty_agent_attention_input(
    tmp_path: Path,
    attention_index: int,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    agent = evidence["agent"]
    assert isinstance(agent, dict)
    tools = agent["tool_uses"]
    assert isinstance(tools, list)
    attentions = [
        item for item in tools if isinstance(item, dict) and item.get("name") == "Attention"
    ]
    assert len(attentions) == 2
    value = attentions[attention_index]["input"]
    assert isinstance(value, dict)
    value["unexpected"] = True
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="Attention input"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_commit", "f" * 40),
        ("evaluator_id", "foreign-evaluator"),
    ],
)
def test_verifier_rejects_wrong_agent_eval_input(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    _agent_tool_input(evidence, "Eval", "run")[field] = value
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="Eval.*input|candidate"):
        verifier.verify(evidence_path)


def test_verifier_rejects_wrong_preregistration_write_content(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    agent = evidence["agent"]
    assert isinstance(agent, dict)
    tools = agent["tool_uses"]
    assert isinstance(tools, list) and isinstance(tools[1], dict)
    prereg_write = tools[1]["input"]
    assert isinstance(prereg_write, dict)
    prereg_write["content"] = "# forged preregistration\n"
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="preregistration Write"):
        verifier.verify(evidence_path)


def test_verifier_rejects_wrong_preregistration_commit_blob(tmp_path: Path) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        prereg_model_blob="# forged committed preregistration\n",
    )

    with pytest.raises(verifier.VerificationError, match="preregistration.*blob"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize("replacement", [None, "RUN-wrong"])
def test_verifier_rejects_missing_or_wrong_task_run_id(
    tmp_path: Path,
    replacement: str | None,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    task = evidence["task"]
    assert isinstance(task, dict)
    if replacement is None:
        task.pop("run_id")
    else:
        task["run_id"] = replacement
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="run_id|Run identity"):
        verifier.verify(evidence_path)


def test_verifier_rejects_committed_receipt_internal_eval_identity(
    tmp_path: Path,
) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        receipt_eval_id="EVAL-" + "c" * 64,
    )

    with pytest.raises(verifier.VerificationError, match="Eval receipt identity"):
        verifier.verify(evidence_path)


def test_verifier_rejects_evidence_eval_id_receipt_ref_mismatch(
    tmp_path: Path,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    evaluation = evidence["eval"]
    assert isinstance(evaluation, dict)
    evaluation["eval_id"] = "EVAL-" + "c" * 64
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="return refs"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    ("fixture_kwargs", "match"),
    [
        ({"wrong_manifest_hash": True}, "manifest_sha256"),
        ({"wrong_final_hash": True}, "Run final"),
    ],
)
def test_verifier_recomputes_task_run_hashes(
    tmp_path: Path,
    fixture_kwargs: dict[str, bool],
    match: str,
) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        **fixture_kwargs,
    )

    with pytest.raises(verifier.VerificationError, match=match):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    "observed_refs",
    [
        [
            "tasks/TASK-20260807-verifier/collected.json",
            "eval/evaluations/EVAL-" + "b" * 64 + "/receipt.json",
        ],
        [
            "tasks/TASK-20260807-verifier/collected.json",
            "runs/RUN-task-verifier/final.json",
            "eval/evaluations/EVAL-" + "b" * 64 + "/receipt.json",
            "runs/RUN-extra/final.json",
        ],
    ],
)
def test_verifier_rejects_missing_or_extra_observed_trailer(
    tmp_path: Path,
    observed_refs: list[str],
) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        observed_refs=observed_refs,
    )

    with pytest.raises(verifier.VerificationError, match="observed trailers"):
        verifier.verify(evidence_path)


def test_verifier_rejects_installed_task_runner(tmp_path: Path) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        installed_task_runner=True,
    )

    with pytest.raises(verifier.VerificationError, match="task_runner"):
        verifier.verify(evidence_path)


def test_verifier_requires_installed_task_adapter(tmp_path: Path) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        installed_task_adapter=False,
    )

    with pytest.raises(verifier.VerificationError, match="task_adapter"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    ("fixture_kwargs", "match"),
    [
        ({"argv_task_id": "TASK-20260807-other"}, "argv"),
        ({"security_profile": "isolated-linux"}, "trusted-local"),
        ({"final_state": "failed_process"}, "completed"),
        ({"broken_base_lineage": True}, "B-C-R"),
    ],
)
def test_verifier_rejects_wrong_task_run_semantics(
    tmp_path: Path,
    fixture_kwargs: dict[str, object],
    match: str,
) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        **fixture_kwargs,
    )

    with pytest.raises(verifier.VerificationError, match=match):
        verifier.verify(evidence_path)


def test_verifier_rejects_old_task_evidence_without_run_fields(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    task = evidence["task"]
    assert isinstance(task, dict)
    for field in (
        "run_id",
        "run_manifest_ref",
        "run_manifest_sha256",
        "run_final_ref",
        "run_final_sha256",
    ):
        task.pop(field)
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="Task Run evidence"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("missing", "product evidence"),
        ("missing_field", "product evidence"),
        ("wheel_hash", "wheel"),
        ("package_hash", "package tree"),
        ("source_commit", "source commit"),
    ],
)
def test_verifier_rejects_missing_or_forged_product_evidence(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    if mutation == "missing":
        evidence.pop("product")
    elif mutation == "missing_field":
        product.pop("wheel_sha256")
    elif mutation == "wheel_hash":
        product["wheel_sha256"] = "0" * 64
    elif mutation == "package_hash":
        product["package_tree_sha256"] = "0" * 64
    else:
        product["source_commit"] = "0" * 40
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match=match):
        verifier.verify(evidence_path)


def test_verifier_rejects_source_checkout_as_package_root(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    source_package = ROOT / "src/aros"
    evidence["package_root"] = str(source_package)
    product["package_root"] = str(source_package)
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="package root|environment"):
        verifier.verify(evidence_path)


def test_verifier_rejects_product_without_source_repository(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    product.pop("source_repository")
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="source repository"):
        verifier.verify(evidence_path)


def test_verifier_rejects_nonexistent_full_commit_with_version_prefix(
    tmp_path: Path,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    source_commit = product["source_commit"]
    version = product["distribution_version"]
    assert isinstance(source_commit, str) and isinstance(version, str)
    prefix = version.split("+g", 1)[1]
    forged = prefix + "0" * (40 - len(prefix))
    assert forged != source_commit
    product["source_commit"] = forged
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="source commit"):
        verifier.verify(evidence_path)


def test_verifier_rejects_alternate_real_source_commit(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    source_commit = product["source_commit"]
    assert isinstance(source_commit, str)
    product["source_commit"] = _git(ROOT, "rev-parse", f"{source_commit}^")
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="source commit|version"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    "relative",
    ["aros/task_adapter.py", "core/agent.py", "cli/aros_app.py"],
)
def test_verifier_rejects_wheel_python_different_from_source_blob(
    tmp_path: Path,
    relative: str,
) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        source_mismatch_file=relative,
    )

    with pytest.raises(verifier.VerificationError, match="source blob"):
        verifier.verify(evidence_path)


def test_verifier_rejects_wrong_source_repository(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    product["source_repository"] = str(tmp_path / "project")
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="source repository|source commit"):
        verifier.verify(evidence_path)


def test_driver_rejects_dirty_source_repository(tmp_path: Path) -> None:
    module = _module(DRIVER, "driver_source_repository")
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "Source Test")
    _git(source, "config", "user.email", "source@example.invalid")
    marker = source / "marker.txt"
    marker.write_text("clean\n")
    _git(source, "add", "marker.txt")
    _git(source, "commit", "-qm", "source")
    head = _git(source, "rev-parse", "HEAD")

    assert hasattr(module, "_validate_source_repository")
    module._validate_source_repository(source, head)
    marker.write_text("dirty\n")

    with pytest.raises(module.CommissioningError, match="dirty"):
        module._validate_source_repository(source, head)


def test_verifier_rejects_wrong_aros_console_entrypoint(tmp_path: Path) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        console_target="arbor.cli.app:main",
    )

    with pytest.raises(verifier.VerificationError, match="console script|entrypoint"):
        verifier.verify(evidence_path)


def test_driver_rejects_aros_executable_outside_environment(tmp_path: Path) -> None:
    module = _module(DRIVER, "driver_aros_executable")
    executable = tmp_path / "aros"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)

    assert hasattr(module, "_validate_aros_executable")
    with pytest.raises(module.CommissioningError, match="aros executable|environment"):
        module._validate_aros_executable(executable)


@pytest.mark.parametrize(
    "variant",
    ["comment_import", "forged_call", "side_effect", "wrong_shebang"],
)
def test_verifier_rejects_noncanonical_aros_console_wrapper(
    tmp_path: Path,
    variant: str,
) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(
        tmp_path,
        aros_script_variant=variant,
    )

    with pytest.raises(verifier.VerificationError, match="aros executable|wrapper|interpreter"):
        verifier.verify(evidence_path)


def test_verifier_rejects_canonical_aros_wrapper_outside_environment(
    tmp_path: Path,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    executable = Path(str(product["aros_executable"]))
    outside = tmp_path / "outside-aros"
    shutil.copyfile(executable, outside)
    outside.chmod(0o755)
    product["aros_executable"] = str(outside)
    product["aros_executable_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="aros executable|environment"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "startup.pth",
        "sitecustomize.py",
        "sitecustomize.pyc",
        "top_level_module.py",
        "arbor_agent.data/purelib/injected.py",
        "arbor//noncanonical.py",
    ],
)
def test_verifier_rejects_unsafe_wheel_member(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    wheel = evidence_path.parent / str(product["wheel_ref"])
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(unsafe_name, b"injected\n")
    product["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="unsafe|unexpected"):
        verifier.verify(evidence_path)


def test_verifier_rejects_duplicate_wheel_member(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    wheel = evidence_path.parent / str(product["wheel_ref"])
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("arbor/core/agent.py", b"duplicate\n")
    product["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="duplicate"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize("mode", [0o100755, 0o120777])
def test_verifier_rejects_executable_or_symlink_wheel_member(
    tmp_path: Path,
    mode: int,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    wheel = evidence_path.parent / str(product["wheel_ref"])
    info = zipfile.ZipInfo("arbor/injected.py")
    info.external_attr = mode << 16
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(info, b"injected\n")
    product["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="unsafe|RECORD|source.*set"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    ("member", "mode"),
    [
        (
            "arbor/skills_suite/arbor-agent-tools/scripts/arbor_state.py",
            0o100644,
        ),
        ("arbor/core/agent.py", 0o100755),
    ],
)
def test_verifier_rejects_wheel_mode_different_from_source(
    tmp_path: Path,
    member: str,
    mode: int,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    wheel = evidence_path.parent / str(product["wheel_ref"])
    replacement = wheel.with_suffix(".replacement.whl")
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(replacement, "w") as target:
        for item in source.infolist():
            if item.filename == member:
                item.external_attr = mode << 16
            target.writestr(item, source.read(item))
    replacement.replace(wheel)
    wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
    product["wheel_sha256"] = wheel_sha256
    direct_path = Path(str(product["dist_info_root"])) / "direct_url.json"
    direct = json.loads(direct_path.read_text())
    direct["archive_info"] = {
        "hash": f"sha256={wheel_sha256}",
        "hashes": {"sha256": wheel_sha256},
    }
    direct_path.write_text(json.dumps(direct, sort_keys=True) + "\n")
    _rewrite_fixture_installed_record(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(
        verifier.VerificationError,
        match="source.*mode|wheel.*mode|installed.*mode",
    ):
        verifier.verify(evidence_path)


def test_verifier_rejects_executable_dist_info_member(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    wheel = evidence_path.parent / str(product["wheel_ref"])
    replacement = wheel.with_suffix(".replacement.whl")
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(replacement, "w") as target:
        for item in source.infolist():
            if item.filename.endswith(".dist-info/METADATA"):
                item.external_attr = 0o100755 << 16
            target.writestr(item, source.read(item))
    replacement.replace(wheel)
    product["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="dist-info.*mode|unsafe"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize("mutation", ["omission", "hash"])
def test_verifier_rejects_invalid_wheel_record(
    tmp_path: Path,
    mutation: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    wheel = evidence_path.parent / str(product["wheel_ref"])
    replacement = wheel.with_suffix(".replacement.whl")
    with zipfile.ZipFile(wheel) as source:
        infos = source.infolist()
        payloads = {item.filename: source.read(item) for item in infos}
    record_name = next(name for name in payloads if name.endswith(".dist-info/RECORD"))
    rows = list(csv.reader(io.StringIO(payloads[record_name].decode())))
    if mutation == "omission":
        rows.pop(0)
    else:
        rows[0][1] = "sha256=" + "A" * 43
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    payloads[record_name] = output.getvalue().encode()
    with zipfile.ZipFile(replacement, "w") as archive:
        for item in infos:
            archive.writestr(item, payloads[item.filename])
    replacement.replace(wheel)
    product["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="RECORD"):
        verifier.verify(evidence_path)


def test_verifier_rejects_installed_arbor_bytecode_cache(tmp_path: Path) -> None:
    verifier, evidence_path, _evidence = _verifier_fixture(tmp_path)
    cache = Path(
        str(_evidence["product"]["distribution_root"])  # type: ignore[index]
    ) / "__pycache__/injected.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"bytecode")

    with pytest.raises(verifier.VerificationError, match="bytecode|cache"):
        verifier.verify(evidence_path)


def test_verifier_rejects_dist_info_bytecode_cache(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    cache = Path(str(product["dist_info_root"])) / "__pycache__/injected.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"bytecode")

    with pytest.raises(verifier.VerificationError, match="bytecode|cache"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_verifier_rejects_unrecorded_nested_dist_info_path(
    tmp_path: Path,
    kind: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    nested = Path(str(product["dist_info_root"])) / "nested/foreign"
    nested.parent.mkdir()
    if kind == "file":
        nested.write_text("foreign\n")
    else:
        nested.mkdir()

    with pytest.raises(verifier.VerificationError, match="dist-info|inventory"):
        verifier.verify(evidence_path)


@pytest.mark.parametrize(
    "mutation",
    ["foreign", "missing", "duplicate", "wrong_hash", "wrong_size"],
)
def test_verifier_rejects_inexact_installed_aros_record(
    tmp_path: Path,
    mutation: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    record = Path(str(product["dist_info_root"])) / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    aros_index = next(i for i, row in enumerate(rows) if row[0] == "../../../bin/aros")
    if mutation == "foreign":
        rows.append(["../../../bin/foreign", "", ""])
    elif mutation == "missing":
        rows.pop(aros_index)
    elif mutation == "duplicate":
        rows.append(list(rows[aros_index]))
    elif mutation == "wrong_hash":
        rows[aros_index][1] = "sha256=" + "A" * 43
    else:
        rows[aros_index][2] = "1"
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    record.write_text(output.getvalue())
    _refresh_installed_inventory_hash(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="RECORD|aros"):
        verifier.verify(evidence_path)


def test_verifier_rejects_extra_generated_dist_info_file(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    dist_info = Path(str(product["dist_info_root"]))
    (dist_info / "FOREIGN").write_text("foreign\n")
    _refresh_installed_inventory_hash(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="dist-info|inventory"):
        verifier.verify(evidence_path)


def test_false_mode_evidence_cannot_mask_installed_mode_drift(tmp_path: Path) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    Path(str(product["distribution_root"]), "core/agent.py").chmod(0o755)
    product["filesystem_permissions_enforced"] = False
    _write_json(evidence_path, evidence)

    with pytest.raises(verifier.VerificationError, match="permission|mode"):
        verifier.verify(evidence_path)


def test_normalized_mode_probe_allows_class_drift_with_exact_bytes(
    tmp_path: Path,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    Path(str(product["distribution_root"]), "core/agent.py").chmod(0o755)
    product["filesystem_permissions_enforced"] = False
    _write_json(evidence_path, evidence)
    assert hasattr(verifier, "_probe_filesystem_modes")
    verifier._probe_filesystem_modes = lambda _root: False

    assert verifier.verify(evidence_path)["state"] == "verified"


@pytest.mark.parametrize("mutation", ["changed", "missing", "extra"])
def test_verifier_rejects_installed_distribution_file_mismatch(
    tmp_path: Path,
    mutation: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    distribution = Path(str(product["distribution_root"]))
    target = distribution / "core/agent.py"
    if mutation == "changed":
        target.write_text("# changed after installation\n")
    elif mutation == "missing":
        target.unlink()
    else:
        target = distribution / "core/extra.py"
        target.write_text("# extra installed file\n")
    _entries, tree_hash = _file_tree(distribution)
    product["distribution_tree_sha256"] = tree_hash
    _write_json(evidence_path, evidence)

    with pytest.raises(
        verifier.VerificationError,
        match="wheel.*installed|distribution|RECORD",
    ):
        verifier.verify(evidence_path)


@pytest.mark.parametrize("relative", ["core/agent.py", "cli/aros_app.py"])
def test_verifier_rejects_mutated_wheel_distribution_file(
    tmp_path: Path,
    relative: str,
) -> None:
    verifier, evidence_path, evidence = _verifier_fixture(tmp_path)
    product = evidence["product"]
    assert isinstance(product, dict)
    wheel = evidence_path.parent / str(product["wheel_ref"])
    replacement = wheel.with_suffix(".replacement.whl")
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(replacement, "w") as target:
        for item in source.infolist():
            payload = source.read(item)
            if item.filename == f"arbor/{relative}":
                payload = b"# mutated wheel file\n"
            target.writestr(item, payload)
    replacement.replace(wheel)
    product["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)

    with pytest.raises(
        verifier.VerificationError,
        match="wheel.*installed|source blob|RECORD",
    ):
        verifier.verify(evidence_path)
