from __future__ import annotations

import argparse
import gc
import hashlib
import json
import shutil
import subprocess
import sys
import weakref
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TASK_ADAPTER = ROOT / "commissioning/principal_loop/task_adapter.py"
SCORER = ROOT / "commissioning/principal_loop/evaluation/score.py"


class CommissioningError(RuntimeError):
    pass


class Driver:
    def __init__(self, runtime: Path) -> None:
        self.runtime = runtime.absolute()
        self.project = self.runtime / "project"

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.project), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise CommissioningError((result.stderr or result.stdout).strip())
        return result.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _message_sha256(messages: object) -> str:
    raw = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _checkpoint_commits(messages: list[dict[str, object]]) -> list[str]:
    commits: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw = block.get("content")
            if not isinstance(raw, str):
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and value.get("state") == "admitted"
                and isinstance(value.get("commit"), str)
            ):
                commits.append(str(value["commit"]))
    return commits


def _require_clean_wheel_interpreter(aros: Path) -> None:
    if Path(sys.executable).absolute().parent != aros.absolute().parent:
        raise CommissioningError("driver must run under the clean-wheel interpreter")


def _instruction() -> str:
    return """Complete exactly one scientific turn for Q-0001.

Use Research.attention first. Read the Question, local source, candidate-mode.txt,
commissioning/real_principal/task_adapter.py, evaluator code, and
eval/suites/real-principal/1/manifest.json. Do not use Bash. Do not create more
than one Idea, Task, or Eval.

Before any Task call, read existing model/CURRENT.md and then use complete Write
calls to replace it and create ideas/I-0001-real-principal.md. You choose the
scientific mechanism, rival/null, scoped prediction, uncertainty, and rationale.
The Model must include headings: Scope and boundary; Proposed mechanism; Rival
or null; Premeasurement prediction; Planned controls; Remaining uncertainty;
Evidence and artifact refs. The Idea must include headings: Target question;
Why worth testing; Proposed action; Expected observations under focal and rival;
Minimal controls and evaluator; Cost and capabilities; What failure would teach;
Task and Eval links.

Write transitions/T-REAL-PREREGISTER/proposal.json with schema_version=1, the
current Attention HEAD as base_commit, workspace_paths containing exactly
model/CURRENT.md and ideas/I-0001-real-principal.md, and assimilations=[]. Call
Research.transition_audit and Research.checkpoint with message "Preregister real
Principal prediction and controls." Do not start Task until that checkpoint is
admitted.

Then use Task exactly once: create write mode with adapter argv ["python3",
"commissioning/real_principal/task_adapter.py"], no network, shell capability
true, one deterministic idempotency key, start/status until terminal, then
collect. Use Eval exactly once: first register
eval/suites/real-principal/1/manifest.json, then run evaluator real-principal
version 1 against the Task child_commit with one deterministic idempotency key.
Inspect Eval status/audit and raw visible output as needed. Process success is not
scientific success; use only the valid MeasurementReceipt metric.

After measurement, call Research.attention again. Read the existing Question,
Model, Idea, and NOW. Use complete Write calls for:
- questions/Q-0001/question.md
- model/CURRENT.md
- ideas/I-0001-real-principal.md
- knowledge/claims/C-0001.md
- memory/NOW.md
- transitions/T-REAL-ASSIMILATE/proposal.json

Preserve the exact human Question. Choose the answer, mechanism revision,
relation/scope, counterevidence, assumptions, uncertainty, and what the result
cannot establish. Claim C-0001 must contain one strict JSON EvidenceLink under
"## Evidence links" naming the Eval receipt, relation supports/challenges/bounds
or context, and a nonempty scope. The final proposal uses the latest Attention
HEAD, lists the five semantic paths (not proposal) in workspace_paths, and has
exactly two assimilations: Task collection with rationale under the Idea or NOW,
and Eval receipt with rationale knowledge/claims/C-0001.md#Evidence links. Each
affected path must be changed and contain its rationale anchor.

Audit the final proposal and checkpoint with message "Assimilate one real
Principal Task and measurement." Stop after the admitted checkpoint. Never fill
a metric yourself, never retry Task/Eval, and never claim Q-0001 resolved unless
the scoped evidence actually satisfies your written criterion."""


def _restart_instruction() -> str:
    return """Recover the current research state without changing it. Use only
Research.attention and Read tools. Explain the exact Key Research Question,
current mechanism and rival, Task/measurement evidence, strongest limitation,
remaining uncertainty, and one next possibility. Do not call Write, Edit, Task,
Run, Eval, transition audit, or checkpoint."""


def commission(aros: Path, runtime: Path, human_review: Path) -> Path:
    aros = aros.resolve(strict=True)
    _require_clean_wheel_interpreter(aros)
    runtime = runtime.absolute()
    if runtime.exists():
        raise CommissioningError(f"runtime must not already exist: {runtime}")
    runtime.mkdir(parents=True)
    driver = Driver(runtime)
    driver.project.mkdir()
    driver.git("init", "-q", "-b", "main")
    driver.git("config", "user.name", "Real Principal Fixture")
    driver.git("config", "user.email", "real-principal@example.invalid")

    (driver.project / "candidate-mode.txt").write_text("baseline\n", encoding="utf-8")
    adapter = driver.project / "commissioning/real_principal/task_adapter.py"
    scorer = driver.project / "commissioning/real_principal/evaluation/score.py"
    adapter.parent.mkdir(parents=True)
    scorer.parent.mkdir(parents=True)
    shutil.copyfile(TASK_ADAPTER, adapter)
    shutil.copyfile(SCORER, scorer)
    driver.git("add", "candidate-mode.txt", "commissioning/real_principal")
    driver.git("commit", "-qm", "add real Principal fixture")
    fixture_commit = driver.git("rev-parse", "HEAD")

    source = runtime / "local-source.md"
    source.write_text(
        "# Deterministic mediator prior\n\nChanging candidate-mode.txt from baseline "
        "to success is predicted to activate the measured mediator. This source "
        "does not establish that the evaluator is valid or that the Question is "
        "resolved.\n",
        encoding="utf-8",
    )
    question = (
        "Does changing candidate mode activate the measured mediator under the "
        "fixed evaluator?"
    )
    from arbor.aros.intake import initialize_knowledge_bank

    intake = initialize_knowledge_bank(
        driver.project,
        question,
        [source],
    )
    scorer_sha256 = hashlib.sha256(scorer.read_bytes()).hexdigest()
    manifest_ref = "eval/suites/real-principal/1/manifest.json"
    _write_json(
        driver.project / manifest_ref,
        {
            "schema_version": 1,
            "evaluator_id": "real-principal",
            "evaluator_version": "1",
            "visibility": "visible",
            "apparatus_commit": fixture_commit,
            "apparatus_paths": [
                {
                    "path": "commissioning/real_principal/evaluation/score.py",
                    "blob_sha256": scorer_sha256,
                }
            ],
            "scorer_argv": [
                "python3",
                "../apparatus/commissioning/real_principal/evaluation/score.py",
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
                "metric_name": "real_principal_quality",
                "minimum": 0,
                "maximum": 1,
                "minimum_samples": 1,
            },
            "known_limitations": ["deterministic scientific commissioning fixture"],
            "calibration_refs": [],
        },
    )
    driver.git("add", manifest_ref)
    driver.git("commit", "-qm", "add fixed real Principal evaluator")
    apparatus_commit = driver.git("rev-parse", "HEAD")

    principal_started = True
    from typer.testing import CliRunner

    from arbor.aros.principal import build_principal_agent as native_build
    from arbor.cli.commands import aros_cmd

    del principal_started
    agents: list[tuple[object, int]] = []

    def capture_build(*args: object, **kwargs: object) -> object:
        agent = native_build(*args, **kwargs)
        agents.append((agent, len(agent.messages)))
        return agent

    aros_cmd.build_principal_agent = capture_build
    runner = CliRunner()
    primary_result = runner.invoke(
        aros_cmd.aros_app,
        [
            "start",
            _instruction(),
            "--cwd",
            str(driver.project),
            "--provider",
            "openai-responses",
            "--model",
            "gpt-5.6-luna",
            "--reasoning-effort",
            "max",
            "--cooperative-human-direct",
            "--max-turns",
            "40",
        ],
    )
    if primary_result.exit_code != 0 or len(agents) != 1:
        _write_json(
            runtime / "failure.json",
            {"stage": "primary", "exit_code": primary_result.exit_code},
        )
        raise CommissioningError(
            primary_result.output or f"primary exited {primary_result.exit_code}"
        )
    primary_agent, primary_initial_messages = agents.pop()
    primary_messages = json.loads(json.dumps(primary_agent.messages))
    primary = {
        "class": f"{type(primary_agent).__module__}.{type(primary_agent).__qualname__}",
        "initial_message_count": primary_initial_messages,
        "provider_class": f"{type(primary_agent.provider).__module__}.{type(primary_agent.provider).__qualname__}",
        "provider": primary_agent.config.provider,
        "model": primary_agent.provider.model,
        "reasoning_effort": primary_agent.config.reasoning_effort,
        "turns": primary_agent.total_turns,
        "input_tokens": primary_agent.total_input_tokens,
        "output_tokens": primary_agent.total_output_tokens,
        "stop_reason": primary_agent.stop_reason,
        "tool_uses": json.loads(json.dumps(primary_agent.tool_uses)),
        "message_sha256": _message_sha256(primary_messages),
        "checkpoint_commits": _checkpoint_commits(primary_messages),
    }
    primary_ref = weakref.ref(primary_agent)
    del primary_agent
    gc.collect()
    if primary_ref() is not None:
        raise CommissioningError("primary Agent/provider survived destruction")

    restart_result = runner.invoke(
        aros_cmd.aros_app,
        [
            "start",
            _restart_instruction(),
            "--cwd",
            str(driver.project),
            "--provider",
            "openai-responses",
            "--model",
            "gpt-5.6-luna",
            "--reasoning-effort",
            "max",
            "--max-turns",
            "6",
        ],
    )
    if restart_result.exit_code != 0 or len(agents) != 1:
        _write_json(
            runtime / "failure.json",
            {"stage": "restart", "exit_code": restart_result.exit_code},
        )
        raise CommissioningError(
            restart_result.output or f"restart exited {restart_result.exit_code}"
        )
    restart_agent, restart_initial_messages = agents.pop()
    restart = {
        "initial_message_count": restart_initial_messages,
        "provider": restart_agent.config.provider,
        "model": restart_agent.provider.model,
        "reasoning_effort": restart_agent.config.reasoning_effort,
        "turns": restart_agent.total_turns,
        "input_tokens": restart_agent.total_input_tokens,
        "output_tokens": restart_agent.total_output_tokens,
        "stop_reason": restart_agent.stop_reason,
        "assistant_texts": list(restart_agent.assistant_texts),
        "tool_uses": json.loads(json.dumps(restart_agent.tool_uses)),
        "message_sha256": _message_sha256(restart_agent.messages),
    }

    evidence = {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(driver.project),
        "fixture_commit": fixture_commit,
        "intake_commit": intake["commit"],
        "apparatus_commit": apparatus_commit,
        "final_commit": driver.git("rev-parse", "HEAD"),
        "question": question,
        "primary": primary,
        "restart": restart,
        "aros_executable": str(aros),
        "human_review": str(human_review.absolute()),
    }
    evidence_path = runtime / "evidence.json"
    _write_json(evidence_path, evidence)
    print(json.dumps({"state": "recorded", "evidence": str(evidence_path)}))
    return evidence_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aros", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--human-review", required=True, type=Path)
    args = parser.parse_args()
    try:
        commission(args.aros, args.runtime, args.human_review)
    except (OSError, ValueError, CommissioningError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
