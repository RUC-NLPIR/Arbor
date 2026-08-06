from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


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

    def _checkpoint_service(self):  # type: ignore[no-untyped-def]
        from arbor.aros.checkpoint import CheckpointService
        from arbor.aros.worktrees import bind_repository, read_repository_snapshot
        from arbor.cli.commands.aros_cmd import HumanDirectGateway

        repository = bind_repository(self.project)
        canonical_ref = read_repository_snapshot(repository).get("ref")
        if not isinstance(canonical_ref, str):
            raise CommissioningError("Agent tool requires an attached canonical ref")
        return CheckpointService(
            self.project,
            canonical_repository=repository,
            canonical_ref=canonical_ref,
            gateway=HumanDirectGateway(),
        )

    def _record_tool_result(
        self,
        name: str,
        action: object,
        output: str,
    ) -> dict[str, object]:
        value = json.loads(output)
        if not isinstance(value, dict):
            raise CommissioningError(f"{name} returned non-object JSON")
        self.commands.append(
            {
                "sequence": len(self.commands) + 1,
                "pid": os.getpid(),
                "argv": [name, str(action)],
                "returncode": 0,
                "stdout_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            }
        )
        return value

    def task_tool(self, **kwargs: object) -> dict[str, object]:
        from arbor.aros.task_tool import TaskTool

        checkpoint = self._checkpoint_service()
        tool = TaskTool(
            cwd=str(self.project),
            operational_admission=checkpoint.checkpoint_operational,
            persist_results=False,
        )
        output = asyncio.run(tool.execute(**kwargs))
        return self._record_tool_result("TaskTool", kwargs.get("action"), output)

    def eval_tool(self, **kwargs: object) -> dict[str, object]:
        from arbor.aros.eval_tool import EvalTool

        checkpoint = self._checkpoint_service()
        tool = EvalTool(
            cwd=str(self.project),
            operational_admission=checkpoint.checkpoint_operational,
            persist_results=False,
        )
        output = asyncio.run(tool.execute(**kwargs))
        return self._record_tool_result("EvalTool", kwargs.get("action"), output)

    def cooperative_checkpoint(
        self,
        proposal_ref: str,
        message: str,
    ) -> dict[str, object]:
        return self.json_command(
            "checkpoint",
            "--proposal",
            proposal_ref,
            "--message",
            message,
            "--cooperative-human-direct",
            timeout=240,
        )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _claim(eval_ref: str, candidate_commit: str) -> str:
    link = json.dumps(
        {
            "observation_ref": eval_ref,
            "relation": "supports",
            "scope": (
                f"candidate {candidate_commit}; fixed seed 7; "
                "visible principal-loop evaluator v1"
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "---\nid: C-0001\n---\n# Claim C-0001\n\n"
        "## Statement\n\n"
        "The deterministic candidate produced the expected success value and "
        "received a valid metric of 1.0.\n\n"
        f"## Evidence links\n\n{link}\n\n"
        "## Counterevidence\n"
    )


def _now(task_id: str, child_commit: str, return_commit: str, eval_id: str) -> str:
    return (
        "# Current State\n\n"
        "## Assimilated task return\n\n"
        f"Task `{task_id}` returned candidate commit `{child_commit}` and "
        f"return commit `{return_commit}`.\n\n"
        "## Measurement\n\n"
        f"Evaluation `{eval_id}` measured `principal_loop_quality=1.0` with "
        "state `valid` for the same candidate commit.\n"
    )


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

    brief = driver.task_tool(
        action="create",
        objective="Produce one deterministic success candidate and strict return.",
        mode="write",
        adapter_argv=[
            "python3",
            "commissioning/principal_loop/task_adapter.py",
        ],
        capabilities={"network": False, "shell": True},
        deliverables=["candidate-mode.txt"],
        acceptance=["candidate-mode.txt equals success"],
        timeout_seconds=120,
        idempotency_key="principal-loop-task",
    )
    task_id = str(brief["task_id"])
    if brief.get("admission_required") is not False:
        raise CommissioningError("Task brief operational admission did not complete")
    driver.json_command("task", "start", task_id, "--actor", "principal", timeout=240)
    deadline = time.monotonic() + 120
    while True:
        status = driver.json_command("task", "status", task_id, record=False)
        if status.get("state") in {"completed", "failed", "lost", "timed_out", "stopped"}:
            break
        if time.monotonic() >= deadline:
            raise CommissioningError("Task did not reach a terminal state")
        time.sleep(0.2)
    if status.get("state") != "completed":
        raise CommissioningError(f"Task terminal state is {status.get('state')}")
    collected = driver.task_tool(action="collect", task_id=task_id)
    collected_ref = f"tasks/{task_id}/collected.json"
    if collected.get("admission_required") is not False:
        raise CommissioningError("Task collection operational admission did not complete")

    child_commit = str(collected["child_commit"])
    evaluation = driver.eval_tool(
        action="run",
        evaluator_id="principal-loop",
        version="1",
        candidate_commit=child_commit,
        idempotency_key="principal-loop-eval",
    )
    if evaluation.get("admission_required") is not False:
        raise CommissioningError("Eval operational admission did not complete")
    if evaluation.get("measurement_state") != "valid" or evaluation.get("metric") != 1.0:
        raise CommissioningError("Eval did not produce the expected valid metric")
    eval_id = str(evaluation["eval_id"])
    eval_ref = f"eval/evaluations/{eval_id}/receipt.json"

    claim_path.write_text(_claim(eval_ref, child_commit), encoding="utf-8")
    (driver.project / "memory/NOW.md").write_text(
        _now(task_id, child_commit, str(collected["return_commit"]), eval_id),
        encoding="utf-8",
    )
    semantic_base = driver.git("rev-parse", "HEAD")
    transition_id = "T-E2E-ASSIMILATE"
    proposal_ref = f"transitions/{transition_id}/proposal.json"
    _write_json(
        driver.project / proposal_ref,
        {
            "schema_version": 1,
            "base_commit": semantic_base,
            "workspace_paths": [
                "knowledge/claims/C-0001.md",
                "memory/NOW.md",
            ],
            "assimilations": [
                {
                    "observation_ref": eval_ref,
                    "affected_paths": [
                        "knowledge/claims/C-0001.md",
                        "memory/NOW.md",
                    ],
                    "rationale": "knowledge/claims/C-0001.md#Evidence links",
                },
                {
                    "observation_ref": collected_ref,
                    "affected_paths": ["memory/NOW.md"],
                    "rationale": "memory/NOW.md#Assimilated task return",
                },
            ],
        },
    )
    audit = driver.json_command("transition", "audit", proposal_ref, timeout=240)
    if audit.get("mechanically_valid") is not True:
        raise CommissioningError("final assimilation transition is invalid")
    checkpoint = driver.cooperative_checkpoint(
        proposal_ref,
        "Assimilate deterministic Task return and valid measurement.",
    )
    final_commit = str(checkpoint["commit"])
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

    driver.json_command("audit", "--rebuild-index", timeout=240)
    complete_packet = driver.json_command("boot", "--json", "--max-chars", "8000")
    cache = driver.project / ".aros/indexes/transition-index.json"
    cache.replace(cache.with_suffix(".json.saved"))
    missing_cache_packet = driver.json_command(
        "boot",
        "--json",
        "--max-chars",
        "8000",
    )
    driver.json_command("audit", "--rebuild-index", timeout=240)
    rebuilt_packet = driver.json_command("boot", "--json", "--max-chars", "8000")

    evidence = {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(driver.project),
        "task": {
            "task_id": task_id,
            "child_commit": child_commit,
            "return_commit": collected["return_commit"],
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
        "restart": {
            "complete_packet": complete_packet,
            "missing_cache_packet": missing_cache_packet,
            "rebuilt_packet": rebuilt_packet,
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
