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
FIXTURE = ROOT / "commissioning/simple_loop"
VERIFIER = ROOT / "scripts/verify_aros_simple_loop.py"


class CommissioningError(RuntimeError):
    pass


class Driver:
    def __init__(self, aros: Path, runtime: Path) -> None:
        self.aros = aros.resolve(strict=True)
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


def commission(aros: Path, runtime: Path) -> Path:
    if runtime.exists():
        raise CommissioningError(f"runtime already exists: {runtime}")
    runtime.mkdir(parents=True)
    driver = Driver(aros, runtime)

    import arbor.aros
    from arbor.aros.attention import AttentionAuthorityContext
    from arbor.aros.intake import initialize_knowledge_bank
    from arbor.aros.principal import build_principal_agent, run_principal
    from arbor.aros.workspace import boot_workspace

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
    eval_id = provider.eval_id
    child_commit = provider.child_commit
    return_commit = provider.return_commit
    collected_ref = provider.collected_ref
    eval_ref = provider.eval_ref
    if not all(
        isinstance(item, str)
        for item in (task_id, eval_id, child_commit, return_commit, collected_ref, eval_ref)
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
    evaluation = json.loads(driver.git("show", f"{final_commit}:{eval_ref}"))

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
        "package_root": str(Path(arbor.aros.__file__).resolve().parent),
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
    _write_json(evidence_path, evidence)
    verification = driver.run([sys.executable, str(VERIFIER), str(evidence_path)], record=False)
    print(verification.stdout.strip())
    return evidence_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aros", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = commission(args.aros, args.runtime)
    except (OSError, ValueError, CommissioningError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "commissioned", "evidence": str(evidence)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
