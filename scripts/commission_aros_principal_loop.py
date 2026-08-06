from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import weakref
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "commissioning" / "principal_loop"
VERIFIER = ROOT / "scripts" / "verify_aros_principal_loop_commissioning.py"


class CommissioningError(RuntimeError):
    pass


class Driver:
    def __init__(self, aros: Path, runtime: Path):
        self.aros = aros.resolve(strict=True)
        self.runtime = runtime.absolute()
        self.project = self.runtime / "project"
        self.commands: list[dict[str, object]] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 180,
        record: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate()
            raise CommissioningError(f"command timed out: {' '.join(argv)}") from error
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if record:
            command_receipt: dict[str, object] = {
                "sequence": len(self.commands) + 1,
                "pid": process.pid,
                "argv": argv,
                "returncode": result.returncode,
                "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            }
            if result.returncode != 0:
                command_receipt["stderr_excerpt"] = stderr[:4_096]
            self.commands.append(command_receipt)
        if result.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit {result.returncode}"
            raise CommissioningError(f"command failed: {' '.join(argv)}: {detail}")
        return result

    def json_command(
        self,
        *args: str,
        timeout: float = 180,
        record: bool = True,
    ) -> dict[str, object]:
        arguments = list(args)
        try:
            separator = arguments.index("--")
        except ValueError:
            arguments.extend(("--cwd", str(self.project)))
        else:
            arguments[separator:separator] = ["--cwd", str(self.project)]
        result = self.run(
            [str(self.aros), *arguments],
            timeout=timeout,
            record=record,
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CommissioningError(f"AROS command returned non-JSON: {args}") from error
        if not isinstance(value, dict):
            raise CommissioningError(f"AROS command returned non-object JSON: {args}")
        return value

    def git(self, *args: str) -> str:
        return self.run(
            ["git", "-C", str(self.project), *args],
            record=False,
        ).stdout.strip()

def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

def _provider_class() -> type[Any]:
    path = FIXTURE / "provider.py"
    spec = importlib.util.spec_from_file_location(
        "aros_principal_loop_provider",
        path,
    )
    if spec is None or spec.loader is None:
        raise CommissioningError("cannot load deterministic Principal provider")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = getattr(module, "PrincipalLoopProvider", None)
    if not isinstance(provider, type):
        raise CommissioningError("deterministic Principal provider is invalid")
    return provider


def _json_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _first_tool_result(messages: list[dict[str, Any]]) -> dict[str, object]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw = block.get("content")
            if not isinstance(raw, str):
                raise CommissioningError("Agent tool result content is invalid")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise CommissioningError("Agent tool result is not a JSON object")
            return value
    raise CommissioningError("Agent messages contain no JSON tool result")


def commission(aros: Path, runtime: Path) -> Path:
    driver = Driver(aros, runtime)
    if runtime.exists():
        raise CommissioningError(f"runtime must not already exist: {runtime}")
    runtime.mkdir(parents=True)
    driver.project.mkdir()
    driver.git("init", "-q", "-b", "main")
    driver.git("config", "user.email", "commissioning@example.invalid")
    driver.git("config", "user.name", "AROS Commissioning")
    driver.json_command(
        "init",
        "--mission",
        "Prove one real cooperative Task-to-Eval-to-assimilation restart loop.",
    )
    driver.git("add", ".")
    driver.git("commit", "-qm", "initialize AROS commissioning workspace")

    adapter_target = driver.project / "commissioning/principal_loop/task_adapter.py"
    scorer_target = driver.project / "commissioning/principal_loop/evaluation/score.py"
    adapter_target.parent.mkdir(parents=True)
    scorer_target.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE / "task_adapter.py", adapter_target)
    shutil.copyfile(FIXTURE / "evaluation/score.py", scorer_target)
    claim_path = driver.project / "knowledge/claims/C-0001.md"
    claim_path.parent.mkdir(parents=True, exist_ok=True)
    claim_path.write_text(
        "---\nid: C-0001\n---\n# Claim C-0001\n\n"
        "## Statement\n\nThe deterministic candidate has not yet been measured.\n\n"
        "## Evidence links\n\n## Counterevidence\n",
        encoding="utf-8",
    )
    driver.git(
        "add",
        "commissioning/principal_loop/task_adapter.py",
        "commissioning/principal_loop/evaluation/score.py",
        "knowledge/claims/C-0001.md",
    )
    driver.git("commit", "-qm", "add deterministic commissioning fixtures")
    apparatus_commit = driver.git("rev-parse", "HEAD")
    scorer_sha256 = hashlib.sha256(scorer_target.read_bytes()).hexdigest()

    manifest_ref = "eval/suites/principal-loop/1/manifest.json"
    _write_json(
        driver.project / manifest_ref,
        {
            "schema_version": 1,
            "evaluator_id": "principal-loop",
            "evaluator_version": "1",
            "visibility": "visible",
            "apparatus_commit": apparatus_commit,
            "apparatus_paths": [
                {
                    "path": "commissioning/principal_loop/evaluation/score.py",
                    "blob_sha256": scorer_sha256,
                }
            ],
            "scorer_argv": [
                "python3",
                "../apparatus/commissioning/principal_loop/evaluation/score.py",
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
                "metric_name": "principal_loop_quality",
                "minimum": 0,
                "maximum": 1,
                "minimum_samples": 1,
            },
            "known_limitations": [
                "commissioning fixture, not a scientific benchmark"
            ],
            "calibration_refs": [],
        },
    )
    driver.git("add", manifest_ref)
    driver.git("commit", "-qm", "add commissioning evaluator manifest")
    driver.json_command("eval", "register", "--manifest", manifest_ref, "--actor", "principal")

    from arbor.aros.attention import AttentionAuthorityContext
    from arbor.aros.principal import build_principal_agent, run_principal
    from arbor.aros.workspace import boot_workspace
    from arbor.aros.worktrees import bind_repository, read_repository_snapshot
    from arbor.cli.commands.aros_cmd import HumanDirectGateway

    provider_type = _provider_class()
    repository = bind_repository(driver.project)
    canonical_ref = read_repository_snapshot(repository).get("ref")
    if not isinstance(canonical_ref, str):
        raise CommissioningError("Principal requires an attached canonical ref")
    context = AttentionAuthorityContext(
        authority={
            "state": "available",
            "enforcement_class": "cooperative",
            "issuer": "human-direct",
        },
        remaining_budget={
            "state": "not_configured",
            "enforcement_class": "cooperative",
        },
        institutional_obligations=(),
    )
    provider = provider_type()
    agent = build_principal_agent(
        provider,
        driver.project,
        boot_workspace(driver.project),
        max_turns=100,
        canonical_repository=repository,
        canonical_ref=canonical_ref,
        admission_gateway=HumanDirectGateway(),
        attention_context=context,
    )
    primary_result = asyncio.run(
        run_principal(agent, "Complete the commissioned research transition.")
    )
    if agent.stop_reason != "finished":
        raise CommissioningError(
            f"primary Agent stopped with reason {agent.stop_reason!r}"
        )
    primary_agent_id = id(agent)
    primary_provider_id = id(provider)
    primary_agent_class = f"{type(agent).__module__}.{type(agent).__qualname__}"
    primary_tool_uses = json.loads(json.dumps(agent.tool_uses))
    primary_message_sha256 = _json_sha256(agent.messages)
    task_id = provider.task_id
    child_commit = provider.child_commit
    return_commit = provider.return_commit
    eval_id = provider.eval_id
    collected_ref = provider.collected_ref
    eval_ref = provider.eval_ref
    semantic_base = provider.base_commit
    if not all(
        isinstance(value, str)
        for value in (
            task_id,
            child_commit,
            return_commit,
            eval_id,
            collected_ref,
            eval_ref,
            semantic_base,
        )
    ):
        raise CommissioningError("primary Agent did not retain exact lineage")
    primary_agent_ref = weakref.ref(agent)
    primary_provider_ref = weakref.ref(provider)
    del agent, provider
    gc.collect()
    if primary_agent_ref() is not None or primary_provider_ref() is not None:
        raise CommissioningError("primary Agent/provider survived destruction")

    final_commit = driver.git("rev-parse", "HEAD")
    transition_id = "T-E2E-ASSIMILATE"
    collected = json.loads(
        driver.run(
            ["git", "-C", str(driver.project), "show", f"{final_commit}:{collected_ref}"],
            record=False,
        ).stdout
    )
    evaluation = json.loads(
        driver.run(
            ["git", "-C", str(driver.project), "show", f"{final_commit}:{eval_ref}"],
            record=False,
        ).stdout
    )
    admission = json.loads(
        driver.run(
            [
                "git",
                "-C",
                str(driver.project),
                "show",
                f"{final_commit}:transitions/{transition_id}/admission.json",
            ],
            record=False,
        ).stdout
    )

    restart_repository = bind_repository(driver.project)
    restart_ref = read_repository_snapshot(restart_repository).get("ref")
    if not isinstance(restart_ref, str):
        raise CommissioningError("restart Principal lacks an attached ref")
    restart_provider = provider_type(restart=True)
    restart_agent = build_principal_agent(
        restart_provider,
        driver.project,
        boot_workspace(driver.project),
        max_turns=4,
        canonical_repository=restart_repository,
        canonical_ref=restart_ref,
        admission_gateway=HumanDirectGateway(),
        attention_context=context,
    )
    restart_initial_messages = len(restart_agent.messages)
    restart_result = asyncio.run(
        run_principal(restart_agent, "Recover the admitted research state.")
    )
    if restart_agent.stop_reason != "finished":
        raise CommissioningError(
            f"restart Agent stopped with reason {restart_agent.stop_reason!r}"
        )
    restart_packet = _first_tool_result(restart_agent.messages)
    restart_agent_id = id(restart_agent)
    restart_provider_id = id(restart_provider)
    if (
        restart_agent_id == primary_agent_id
        or restart_provider_id == primary_provider_id
    ):
        raise CommissioningError("restart reused a primary object identity")

    evidence = {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(driver.project),
        "task": {
            "task_id": task_id,
            "child_commit": child_commit,
            "return_commit": return_commit,
            "collected_ref": collected_ref,
            "collected_sha256": collected["collected_sha256"],
        },
        "eval": {
            "eval_id": eval_id,
            "candidate_commit": evaluation["candidate_commit"],
            "receipt_ref": eval_ref,
            "receipt_sha256": evaluation["receipt_sha256"],
            "metric": evaluation["metric"],
        },
        "checkpoint": {
            "transition_id": transition_id,
            "base_commit": semantic_base,
            "commit": final_commit,
            "receipt_sha256": admission["receipt_sha256"],
        },
        "agent": {
            "class": primary_agent_class,
            "instance": primary_agent_id,
            "provider_instance": primary_provider_id,
            "destroyed_before_restart": True,
            "stop_reason": "finished",
            "result": primary_result,
            "tool_uses": primary_tool_uses,
            "message_sha256": primary_message_sha256,
        },
        "restart": {
            "agent_instance": restart_agent_id,
            "provider_instance": restart_provider_id,
            "initial_message_count": restart_initial_messages,
            "stop_reason": "finished",
            "result": restart_result,
            "tool_uses": json.loads(json.dumps(restart_agent.tool_uses)),
            "message_sha256": _json_sha256(restart_agent.messages),
            "packet": restart_packet,
        },
        "commands": driver.commands,
    }
    evidence_path = runtime / "evidence.json"
    _write_json(evidence_path, evidence)
    verification = driver.run(
        [sys.executable, str(VERIFIER), str(evidence_path)],
        record=False,
    )
    print(verification.stdout.strip())
    return evidence_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one real cooperative Arbor-native AROS principal loop."
    )
    parser.add_argument("--aros", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = commission(args.aros, args.runtime)
    except (OSError, ValueError, CommissioningError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "commissioned", "evidence": str(evidence)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
