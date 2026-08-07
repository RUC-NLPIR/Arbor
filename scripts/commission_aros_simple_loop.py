from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import weakref
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "commissioning/simple_loop"
VERIFIER = ROOT / "scripts/verify_aros_simple_loop.py"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ENVIRONMENT_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
)


class CommissioningError(RuntimeError):
    pass


def _controlled_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in _ENVIRONMENT_KEYS if key in os.environ}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _validate_aros_executable(path: Path) -> Path:
    supplied = path.absolute()
    executable = supplied.resolve(strict=True)
    environment_bin = Path(sys.prefix).absolute() / "bin"
    if (
        supplied != executable
        or Path(sys.executable).absolute().parent != environment_bin
        or executable.parent != environment_bin
        or executable.name != "aros"
        or not stat.S_ISREG(executable.lstat().st_mode)
        or executable.lstat().st_nlink != 1
        or not os.access(executable, os.X_OK)
    ):
        raise CommissioningError("aros executable is outside the driver environment")
    return executable


class Driver:
    def __init__(self, aros: Path, runtime: Path) -> None:
        self.aros = _validate_aros_executable(aros)
        self.runtime = runtime.absolute()
        self.project = self.runtime / "project"
        self.commands: list[dict[str, object]] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 240,
        record: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_controlled_environment(),
        )
        if record:
            self.commands.append(
                {
                    "sequence": len(self.commands) + 1,
                    "argv": argv,
                    "returncode": result.returncode,
                    "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                    "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
                }
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CommissioningError(f"command failed: {' '.join(argv)}: {detail}")
        return result

    def json_command(self, *args: str) -> dict[str, object]:
        result = self.run([str(self.aros), *args, "--cwd", str(self.project)])
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise CommissioningError("AROS command did not return an object")
        return value

    def git(self, *args: str) -> str:
        return self.run(
            ["git", "--no-replace-objects", "-C", str(self.project), *args],
            record=False,
        ).stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _file_tree(root: Path) -> tuple[list[dict[str, object]], str]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise CommissioningError(f"installed package entry is not physical: {path}")
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": metadata.st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return entries, hashlib.sha256(raw).hexdigest()


def _filesystem_permissions_enforced(root: Path) -> bool:
    probe = root / ".aros-commissioning-mode-probe"
    try:
        with probe.open("xb"):
            pass
        probe.chmod(0o600)
        return stat.S_IMODE(probe.stat().st_mode) == 0o600
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass


def _wheel_tree(path: Path) -> tuple[list[dict[str, object]], str]:
    entries: list[dict[str, object]] = []
    console_targets: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            if item.filename.startswith("arbor/"):
                payload = archive.read(item)
                entries.append(
                    {
                        "path": item.filename.removeprefix("arbor/"),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            elif item.filename.endswith(".dist-info/entry_points.txt"):
                section = ""
                for line in archive.read(item).decode("utf-8").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        section = stripped[1:-1]
                    elif section == "console_scripts" and "=" in stripped:
                        name, target = stripped.split("=", 1)
                        if name.strip() == "aros":
                            console_targets.append(target.strip())
    if console_targets != ["arbor.cli.aros_app:main"]:
        raise CommissioningError("wheel aros console script entrypoint differs")
    return sorted(entries, key=lambda item: str(item["path"])), console_targets[0]


def _validate_source_repository(root: Path, source_commit: str) -> Path:
    supplied = root.absolute()
    source_repository = supplied.resolve(strict=True)

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(source_repository), *args],
            capture_output=True,
            text=True,
            check=False,
            env=_controlled_environment(),
        )
        if result.returncode != 0:
            raise CommissioningError("unable to validate product source repository")
        return result.stdout.strip()

    if (
        supplied != source_repository
        or git("rev-parse", "--show-toplevel") != str(source_repository)
        or git("rev-parse", "HEAD") != source_commit
    ):
        raise CommissioningError("product source repository identity differs")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise CommissioningError("product source repository is dirty")
    return source_repository


def _provider_class() -> type[Any]:
    spec = importlib.util.spec_from_file_location(
        "aros_simple_loop_provider",
        FIXTURE / "provider.py",
    )
    if spec is None or spec.loader is None:
        raise CommissioningError("cannot load deterministic provider")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = getattr(module, "SimpleLoopProvider", None)
    if not isinstance(provider, type):
        raise CommissioningError("deterministic provider is invalid")
    return provider


def _verification_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "aros_simple_loop_verifier",
        VERIFIER,
    )
    if spec is None or spec.loader is None:
        raise CommissioningError("cannot load deterministic verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _first_tool_result(messages: list[dict[str, Any]]) -> dict[str, object]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            value = json.loads(str(block.get("content")))
            if isinstance(value, dict):
                return value
    raise CommissioningError("Agent messages contain no object tool result")


def commission(
    aros: Path,
    runtime: Path,
    wheel: Path,
    source_commit: str,
) -> Path:
    if runtime.exists():
        raise CommissioningError(f"runtime already exists: {runtime}")
    wheel = wheel.absolute().resolve(strict=True)
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise CommissioningError(f"wheel must be an existing file: {wheel}")
    if _COMMIT.fullmatch(source_commit) is None:
        raise CommissioningError("product source commit is invalid")
    source_repository = _validate_source_repository(ROOT, source_commit)
    runtime.mkdir(parents=True)
    retained_wheel = runtime / "artifacts" / wheel.name
    retained_wheel.parent.mkdir()
    with wheel.open("rb") as source, retained_wheel.open("xb") as target:
        shutil.copyfileobj(source, target)
    driver = Driver(aros, runtime)

    sys.dont_write_bytecode = True
    verifier_module = _verification_module()
    (
        wheel_entries,
        wheel_payloads,
        wheel_modes,
        distribution_version,
        console_scripts,
        dist_root,
    ) = (
        verifier_module._wheel_inventory(retained_wheel)
    )
    console_target = console_scripts["aros"]
    purelib = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    permissions_enforced = _filesystem_permissions_enforced(purelib)
    (
        _installed_entries,
        installed_payloads,
        distribution_root,
        dist_info_root,
        distribution_tree_sha256,
        installed_inventory_sha256,
    ) = verifier_module._installed_inventory(
        purelib,
        dist_root,
        wheel_modes,
        permissions_enforced,
        Path(sys.prefix).absolute() / "bin",
        console_scripts,
        retained_wheel,
    )
    wheel_arbor = {
        name: payload for name, payload in wheel_payloads.items() if name.startswith("arbor/")
    }
    installed_arbor = {
        name: payload
        for name, payload in installed_payloads.items()
        if name.startswith("arbor/")
    }
    if wheel_arbor != installed_arbor:
        raise CommissioningError("wheel and installed arbor trees differ before import")
    wheel_inventory_sha256 = hashlib.sha256(
        json.dumps(wheel_entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    import arbor.aros
    from arbor.aros.attention import AttentionAuthorityContext
    from arbor.aros.intake import initialize_knowledge_bank
    from arbor.aros.principal import build_principal_agent, run_principal
    from arbor.aros.workspace import boot_workspace

    package_root = Path(arbor.aros.__file__).resolve(strict=True).parent
    if package_root.parent != distribution_root:
        raise CommissioningError("imported arbor.aros differs from preflight distribution")
    environment_root = Path(sys.prefix).resolve(strict=True)
    if (
        not package_root.is_relative_to(environment_root)
        or "site-packages" not in package_root.parts
    ):
        raise CommissioningError("arbor.aros was not imported from the clean environment")
    package_entries, package_tree_sha256 = _file_tree(package_root)
    wheel_sha256 = hashlib.sha256(retained_wheel.read_bytes()).hexdigest()
    aros_sha256 = hashlib.sha256(driver.aros.read_bytes()).hexdigest()

    initialize_knowledge_bank(
        driver.project,
        "Does the deterministic candidate produce the expected valid measurement?",
    )
    driver.git("config", "user.name", "AROS Commissioning")
    driver.git("config", "user.email", "commissioning@example.invalid")

    adapter = driver.project / "commissioning/simple_loop/task_adapter.py"
    scorer = driver.project / "commissioning/simple_loop/evaluation/score.py"
    adapter.parent.mkdir(parents=True)
    scorer.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE / "task_adapter.py", adapter)
    shutil.copyfile(FIXTURE / "evaluation/score.py", scorer)
    driver.git("add", "commissioning/simple_loop/task_adapter.py", "commissioning/simple_loop/evaluation/score.py")
    driver.git("commit", "-qm", "Add deterministic worker and evaluator")
    apparatus_commit = driver.git("rev-parse", "HEAD")
    manifest_ref = "eval/suites/simple-loop/1/manifest.json"
    _write_json(
        driver.project / manifest_ref,
        {
            "schema_version": 1,
            "evaluator_id": "simple-loop",
            "evaluator_version": "1",
            "visibility": "visible",
            "apparatus_commit": apparatus_commit,
            "apparatus_paths": [
                {
                    "path": "commissioning/simple_loop/evaluation/score.py",
                    "blob_sha256": hashlib.sha256(scorer.read_bytes()).hexdigest(),
                }
            ],
            "scorer_argv": [
                "python3",
                "../apparatus/commissioning/simple_loop/evaluation/score.py",
            ],
            "scorer_cwd": ".",
            "inputs": ["candidate-mode.txt"],
            "environment_ref": "isolated-evaluator-v1",
            "seed_policy": {"kind": "fixed", "seed": 7},
            "resource_limits": {"timeout_seconds": 120},
            "success_exit_codes": [0],
            "raw_outputs": ["stdout", "stderr"],
            "metric_output": {
                "source": "scorer_stdout",
                "parser": "aros.scalar-metric-v1",
                "metric_name": "simple_loop_quality",
                "minimum": 0,
                "maximum": 1,
                "minimum_samples": 1,
            },
            "known_limitations": ["commissioning fixture only"],
            "calibration_refs": [],
        },
    )
    driver.git("add", manifest_ref)
    driver.git("commit", "-qm", "Register deterministic evaluator apparatus")
    driver.json_command("eval", "register", "--manifest", manifest_ref, "--actor", "principal")

    context = AttentionAuthorityContext(
        authority={
            "state": "available",
            "enforcement_class": "cooperative",
            "issuer": "commissioning-host",
        },
        remaining_budget={"state": "not_configured"},
        institutional_obligations=(),
    )
    provider_type = _provider_class()
    provider = provider_type()
    agent = build_principal_agent(
        provider,
        driver.project,
        boot_workspace(driver.project, context=context),
        max_turns=80,
        allow_checkpoint=True,
        attention_context=context,
    )
    primary_result = asyncio.run(run_principal(agent, "Complete the deterministic research loop."))
    if agent.stop_reason != "finished":
        raise CommissioningError(f"primary Agent stopped with {agent.stop_reason!r}")
    primary = {
        "class": f"{type(agent).__module__}.{type(agent).__qualname__}",
        "instance": id(agent),
        "provider_instance": id(provider),
        "stop_reason": agent.stop_reason,
        "result": primary_result,
        "tool_uses": json.loads(json.dumps(agent.tool_uses)),
        "message_sha256": _digest(agent.messages),
    }
    task_id = provider.task_id
    run_id = provider.run_id
    run_manifest_ref = provider.run_manifest_ref
    run_manifest_sha256 = provider.run_manifest_sha256
    run_final_ref = provider.run_final_ref
    run_final_sha256 = provider.run_final_sha256
    eval_id = provider.eval_id
    child_commit = provider.child_commit
    return_commit = provider.return_commit
    collected_ref = provider.collected_ref
    eval_ref = provider.eval_ref
    if not all(
        isinstance(item, str)
        for item in (
            task_id,
            run_id,
            run_manifest_ref,
            run_manifest_sha256,
            run_final_ref,
            run_final_sha256,
            eval_id,
            child_commit,
            return_commit,
            collected_ref,
            eval_ref,
        )
    ):
        raise CommissioningError("provider did not retain exact lineage")

    agent_ref = weakref.ref(agent)
    provider_ref = weakref.ref(provider)
    del agent, provider
    gc.collect()
    if agent_ref() is not None or provider_ref() is not None:
        raise CommissioningError("primary Agent or provider survived destruction")
    primary["destroyed_before_restart"] = True

    final_commit = driver.git("rev-parse", "HEAD")
    final_parent = driver.git("rev-parse", "HEAD^")
    prereg_commit = driver.git("log", "--format=%H", "--grep=^Preregister deterministic mechanism and test.$", "-1")
    collected = json.loads(driver.git("show", f"{final_commit}:{collected_ref}"))
    run_manifest = json.loads(driver.git("show", f"{final_commit}:{run_manifest_ref}"))
    run_final = json.loads(driver.git("show", f"{final_commit}:{run_final_ref}"))
    evaluation = json.loads(driver.git("show", f"{final_commit}:{eval_ref}"))
    retained_run = {
        "run_id": run_id,
        "run_manifest_ref": run_manifest_ref,
        "run_manifest_sha256": run_manifest_sha256,
        "run_final_ref": run_final_ref,
        "run_final_sha256": run_final_sha256,
    }
    if any(collected.get(field) != value for field, value in retained_run.items()):
        raise CommissioningError("provider and committed Task Run lineage differ")
    if (
        run_manifest.get("run_id") != run_id
        or run_manifest.get("manifest_sha256") != run_manifest_sha256
        or run_final.get("run_id") != run_id
        or run_final.get("manifest_sha256") != run_manifest_sha256
        or run_final.get("state") != "completed"
        or _digest(run_final) != run_final_sha256
    ):
        raise CommissioningError("committed Task Run manifest/final lineage differs")
    if (
        collected.get("child_commit") != child_commit
        or collected.get("return_commit") != return_commit
        or evaluation.get("candidate_commit") != child_commit
    ):
        raise CommissioningError("Task B-C-R and Eval candidate lineage differs")
    for ref in (run_manifest_ref, run_final_ref, collected_ref, eval_ref):
        if driver.git("show", f"{final_parent}:{ref}") != driver.git(
            "show",
            f"{final_commit}:{ref}",
        ):
            raise CommissioningError(f"record was not committed before checkpoint: {ref}")
    if driver.git("status", "--porcelain=v1", "--untracked-files=all"):
        raise CommissioningError("main project is dirty after final checkpoint")
    preserved = driver.json_command("task", "preserve", task_id)
    if (
        preserved.get("state") != "preserved"
        or preserved.get("clean") is not True
        or preserved.get("head_commit") != return_commit
        or preserved.get("branch_ref") != collected.get("branch_ref")
    ):
        raise CommissioningError("task worktree was not cleanly preserved")

    restart_provider = provider_type(restart=True)
    restart_agent = build_principal_agent(
        restart_provider,
        driver.project,
        boot_workspace(driver.project, context=context),
        max_turns=4,
        attention_context=context,
    )
    initial_messages = len(restart_agent.messages)
    restart_result = asyncio.run(run_principal(restart_agent, "Recover durable research state."))
    if restart_agent.stop_reason != "finished":
        raise CommissioningError(f"restart Agent stopped with {restart_agent.stop_reason!r}")
    restart_packet = _first_tool_result(restart_agent.messages)

    evidence = {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(driver.project),
        "package_root": str(package_root),
        "product": {
            "source_commit": source_commit,
            "source_repository": str(source_repository),
            "distribution": "arbor-agent",
            "distribution_version": distribution_version,
            "wheel_ref": retained_wheel.relative_to(runtime).as_posix(),
            "wheel_sha256": wheel_sha256,
            "package_root": str(package_root),
            "package_tree_sha256": package_tree_sha256,
            "distribution_root": str(distribution_root),
            "distribution_tree_sha256": distribution_tree_sha256,
            "aros_executable": str(driver.aros),
            "aros_executable_sha256": aros_sha256,
            "console_script": console_target,
            "python_executable": str(Path(sys.executable).absolute()),
            "environment_root": str(Path(sys.prefix).absolute()),
            "dist_info_root": str(dist_info_root),
            "wheel_inventory_sha256": wheel_inventory_sha256,
            "installed_inventory_sha256": installed_inventory_sha256,
            "filesystem_permissions_enforced": permissions_enforced,
        },
        "task": {
            "task_id": task_id,
            "child_commit": child_commit,
            "return_commit": return_commit,
            "collected_ref": collected_ref,
            "collected_sha256": collected["collected_sha256"],
            "run_id": run_id,
            "run_manifest_ref": run_manifest_ref,
            "run_manifest_sha256": run_manifest_sha256,
            "run_final_ref": run_final_ref,
            "run_final_sha256": run_final_sha256,
        },
        "eval": {
            "eval_id": eval_id,
            "run_id": evaluation["run_id"],
            "candidate_commit": evaluation["candidate_commit"],
            "receipt_ref": eval_ref,
            "receipt_sha256": evaluation["receipt_sha256"],
            "metric": evaluation["metric"],
        },
        "checkpoint": {
            "preregistration_commit": prereg_commit,
            "final_parent": final_parent,
            "final_commit": final_commit,
        },
        "agent": primary,
        "restart": {
            "agent_instance": id(restart_agent),
            "provider_instance": id(restart_provider),
            "initial_message_count": initial_messages,
            "stop_reason": restart_agent.stop_reason,
            "result": restart_result,
            "tool_uses": json.loads(json.dumps(restart_agent.tool_uses)),
            "message_sha256": _digest(restart_agent.messages),
            "packet": restart_packet,
        },
        "commands": driver.commands,
    }
    evidence_path = runtime / "evidence.json"
    _validate_source_repository(source_repository, source_commit)
    _write_json(evidence_path, evidence)
    verifier_argv = [
        sys.executable,
        "-I",
        "-S",
        str(VERIFIER),
        str(evidence_path),
    ]
    driver.run(verifier_argv)
    _write_json(evidence_path, evidence)
    verification = driver.run(
        verifier_argv,
        record=False,
    )
    print(verification.stdout.strip())
    return evidence_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aros", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        evidence = commission(
            args.aros,
            args.runtime,
            args.wheel,
            args.source_commit,
        )
    except (OSError, ValueError, CommissioningError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "commissioned", "evidence": str(evidence)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
