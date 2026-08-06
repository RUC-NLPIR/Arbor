from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "commissioning/principal_loop/task_adapter.py"
SCORER = ROOT / "commissioning/principal_loop/evaluation/score.py"
DRIVER = ROOT / "scripts/commission_aros_principal_loop.py"
VERIFIER = ROOT / "scripts/verify_aros_principal_loop_commissioning.py"
PROVIDER = ROOT / "commissioning/principal_loop/provider.py"


def _provider_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aros_principal_loop_provider",
        PROVIDER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verifier_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aros_principal_loop_verifier",
        VERIFIER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provider_result(
    messages: list[dict[str, object]],
    response: Any,
    content: str,
    *,
    is_error: bool = False,
) -> list[dict[str, object]]:
    call = response.get_tool_calls()[0]
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
                    **({"is_error": True} if is_error else {}),
                }
            ],
        },
    ]


def test_commissioning_provider_has_no_reality_interface_imports() -> None:
    source = PROVIDER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not imported & {
        "os",
        "pathlib",
        "shutil",
        "subprocess",
        "arbor.aros",
    }


def test_primary_provider_starts_with_attention_and_rejects_error_result() -> None:
    module = _provider_module()
    provider = module.PrincipalLoopProvider()
    messages: list[dict[str, object]] = [{"role": "user", "content": "go"}]

    first = asyncio.run(provider.create(system="boot", messages=messages))

    call = first.get_tool_calls()[0]
    assert (call.name, call.input) == ("Research", {"action": "attention"})
    messages = _provider_result(messages, first, "failed", is_error=True)
    try:
        asyncio.run(provider.create(system="boot", messages=messages))
    except ValueError as error:
        assert "tool result is_error" in str(error)
    else:
        raise AssertionError("provider accepted an error tool result")


def test_primary_provider_advances_only_from_exact_tool_results() -> None:
    module = _provider_module()
    provider = module.PrincipalLoopProvider()
    messages: list[dict[str, object]] = [{"role": "user", "content": "go"}]
    response = asyncio.run(provider.create(system="boot", messages=messages))
    observed: list[tuple[str, dict[str, object]]] = []

    def advance(value: object) -> Any:
        nonlocal messages, response
        call = response.get_tool_calls()[0]
        observed.append((call.name, call.input))
        content = value if isinstance(value, str) else json.dumps(value)
        messages = _provider_result(messages, response, content)
        response = asyncio.run(provider.create(system="boot", messages=messages))
        return response

    advance(
        {
            "snapshot": {"candidate": {"head": "0" * 40}},
            "unassimilated_returns": [],
            "recent_evidence_delta": [],
        }
    )
    assert response.get_tool_calls()[0].name == "Task"
    advance(
        {
            "task_id": "TASK-live",
            "admission_required": False,
        }
    )
    advance({"task_id": "TASK-live", "state": "running"})
    advance({"task_id": "TASK-live", "state": "running"})
    assert response.get_tool_calls()[0].input == {
        "action": "status",
        "task_id": "TASK-live",
    }
    advance({"task_id": "TASK-live", "state": "completed"})
    child_commit = "1" * 40
    return_commit = "2" * 40
    advance(
        {
            "task_id": "TASK-live",
            "child_commit": child_commit,
            "return_commit": return_commit,
            "collected_sha256": "a" * 64,
            "admission_required": False,
        }
    )
    eval_id = "EVAL-" + "b" * 64
    advance(
        {
            "eval_id": eval_id,
            "candidate_commit": child_commit,
            "measurement_state": "valid",
            "metric": 1.0,
            "receipt_sha256": "c" * 64,
            "admission_required": False,
        }
    )
    collected_ref = "tasks/TASK-live/collected.json"
    eval_ref = f"eval/evaluations/{eval_id}/receipt.json"
    semantic_base = "3" * 40
    advance(
        {
            "snapshot": {"candidate": {"head": semantic_base}},
            "unassimilated_returns": [
                {"ref": collected_ref},
                {"ref": eval_ref},
            ],
            "recent_evidence_delta": [],
        }
    )
    advance("initial claim")
    advance("initial now")
    claim_write = response.get_tool_calls()[0]
    assert claim_write.name == "Write"
    assert eval_ref in claim_write.input["content"]
    advance("Overwrote claim")
    now_write = response.get_tool_calls()[0]
    assert now_write.name == "Write"
    assert child_commit in now_write.input["content"]
    advance("Overwrote now")
    proposal_write = response.get_tool_calls()[0]
    proposal = json.loads(proposal_write.input["content"])
    assert proposal["base_commit"] == semantic_base
    assert {item["observation_ref"] for item in proposal["assimilations"]} == {
        collected_ref,
        eval_ref,
    }
    advance("Created proposal")
    advance({"mechanically_valid": True})
    final = advance({"commit": "4" * 40})

    assert final.get_tool_calls() == []
    assert final.get_text() == "Cooperative research transition admitted."
    assert [name for name, _ in observed] == [
        "Research",
        "Task",
        "Task",
        "Task",
        "Task",
        "Task",
        "Eval",
        "Research",
        "Read",
        "Read",
        "Write",
        "Write",
        "Write",
        "Research",
        "Research",
    ]


def test_restart_provider_requires_admitted_attention_without_pending_returns() -> None:
    module = _provider_module()
    provider = module.PrincipalLoopProvider(restart=True)
    messages: list[dict[str, object]] = [{"role": "user", "content": "recover"}]
    attention = asyncio.run(provider.create(system="boot", messages=messages))

    messages = _provider_result(
        messages,
        attention,
        json.dumps(
            {
                "unassimilated_returns": [],
                "recent_evidence_delta": [
                    {"transition_id": "T-E2E-ASSIMILATE"}
                ],
            }
        ),
    )
    final = asyncio.run(provider.create(system="boot", messages=messages))

    assert final.get_tool_calls() == []
    assert "T-E2E-ASSIMILATE" in final.get_text()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_scorer_emits_one_strict_metric(tmp_path: Path) -> None:
    (tmp_path / "candidate-mode.txt").write_text("success\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCORER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "metric": 1.0,
        "sample_count": 1,
    }
    assert result.stderr == ""


def test_scorer_fails_closed_without_success_input(tmp_path: Path) -> None:
    (tmp_path / "candidate-mode.txt").write_text("baseline\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCORER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_task_adapter_writes_strict_b_c_r_topology(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "adapter@example.invalid")
    _git(tmp_path, "config", "user.name", "Adapter Test")
    (tmp_path / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "baseline.txt")
    _git(tmp_path, "commit", "-qm", "baseline")
    base = _git(tmp_path, "rev-parse", "HEAD")
    task_id = "TASK-20260805-commissioning-adapter-test"
    brief_sha256 = "a" * 64
    environment = {
        **os.environ,
        "AROS_TASK_ID": task_id,
        "AROS_TASK_BRIEF_SHA256": brief_sha256,
        "AROS_TASK_BASE_COMMIT": base,
    }

    subprocess.run(
        [sys.executable, str(ADAPTER)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    return_commit = _git(tmp_path, "rev-parse", "HEAD")
    child_commit = _git(tmp_path, "rev-parse", "HEAD^")
    assert _git(tmp_path, "rev-parse", "HEAD^^") == base
    assert _git(tmp_path, "diff-tree", "--no-commit-id", "--name-only", "-r", child_commit) == "candidate-mode.txt"
    assert _git(tmp_path, "diff-tree", "--no-commit-id", "--name-only", "-r", return_commit) == f"tasks/{task_id}/return.json"
    returned = json.loads((tmp_path / "tasks" / task_id / "return.json").read_bytes())
    assert returned["child_commit"] == child_commit
    assert returned["base_commit"] == base
    assert returned["brief_sha256"] == brief_sha256
    unhashed = {key: value for key, value in returned.items() if key != "return_sha256"}
    canonical = json.dumps(
        unhashed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert returned["return_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_verifier_rejects_unrelated_task_and_measurement(tmp_path: Path) -> None:
    evidence = {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(tmp_path),
        "task": {
            "task_id": "TASK-test",
            "child_commit": "1" * 40,
            "return_commit": "3" * 40,
            "collected_ref": "tasks/TASK-test/collected.json",
            "collected_sha256": "a" * 64,
        },
        "eval": {
            "eval_id": "EVAL-" + "b" * 64,
            "candidate_commit": "2" * 40,
            "receipt_ref": "eval/evaluations/EVAL-" + "b" * 64 + "/receipt.json",
            "receipt_sha256": "c" * 64,
            "metric": 1.0,
        },
        "checkpoint": {
            "transition_id": "T-E2E-ASSIMILATE",
            "base_commit": "4" * 40,
            "commit": "5" * 40,
            "receipt_sha256": "d" * 64,
        },
        "restart": {
            "complete_packet": {},
            "missing_cache_packet": {},
            "rebuilt_packet": {},
        },
        "commands": [],
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "candidate_commit" in result.stderr


def _minimal_live_evidence(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "enforcement_class": "cooperative",
        "project": str(tmp_path),
        "task": {
            "task_id": "TASK-live",
            "child_commit": "1" * 40,
            "return_commit": "2" * 40,
            "collected_ref": "tasks/TASK-live/collected.json",
            "collected_sha256": "a" * 64,
        },
        "eval": {
            "eval_id": "EVAL-" + "b" * 64,
            "candidate_commit": "1" * 40,
            "receipt_ref": "eval/evaluations/EVAL-" + "b" * 64 + "/receipt.json",
            "receipt_sha256": "c" * 64,
            "metric": 1.0,
        },
        "checkpoint": {
            "transition_id": "T-E2E-ASSIMILATE",
            "base_commit": "3" * 40,
            "commit": "4" * 40,
            "receipt_sha256": "d" * 64,
        },
        "restart": {
            "agent_instance": 2,
            "provider_instance": 4,
            "initial_message_count": 0,
            "stop_reason": "finished",
            "result": "recovered",
            "tool_uses": [
                {"name": "Research", "input": {"action": "attention"}}
            ],
            "message_sha256": "e" * 64,
            "packet": {
                "unassimilated_returns": [],
                "recent_evidence_delta": [
                    {"transition_id": "T-E2E-ASSIMILATE"}
                ],
            },
        },
        "commands": [{"returncode": 0}],
    }


def _run_verifier(tmp_path: Path, evidence: dict[str, object]) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        capture_output=True,
        text=True,
    )


def test_verifier_requires_live_agent_section_before_repository_io(
    tmp_path: Path,
) -> None:
    result = _run_verifier(tmp_path, _minimal_live_evidence(tmp_path))

    assert result.returncode != 0
    assert "agent section" in result.stderr


def test_verifier_rejects_restart_reusing_primary_object_identity(
    tmp_path: Path,
) -> None:
    evidence = _minimal_live_evidence(tmp_path)
    evidence["agent"] = {
        "class": "arbor.core.agent.Agent",
        "instance": 2,
        "provider_instance": 4,
        "destroyed_before_restart": True,
        "stop_reason": "finished",
        "result": "admitted",
        "tool_uses": [],
        "message_sha256": "f" * 64,
    }

    result = _run_verifier(tmp_path, evidence)

    assert result.returncode != 0
    assert "fresh Agent/provider" in result.stderr


def _valid_primary_tool_uses() -> list[dict[str, object]]:
    return [
        {"name": "Research", "input": {"action": "attention"}},
        {"name": "Task", "input": {"action": "create"}},
        {"name": "Task", "input": {"action": "start"}},
        {"name": "Task", "input": {"action": "status"}},
        {"name": "Task", "input": {"action": "collect"}},
        {"name": "Eval", "input": {"action": "run"}},
        {"name": "Research", "input": {"action": "attention"}},
        {
            "name": "Read",
            "input": {"file_path": "knowledge/claims/C-0001.md"},
        },
        {"name": "Read", "input": {"file_path": "memory/NOW.md"}},
        {
            "name": "Write",
            "input": {
                "file_path": "knowledge/claims/C-0001.md",
                "content": "claim\n",
            },
        },
        {
            "name": "Write",
            "input": {"file_path": "memory/NOW.md", "content": "now\n"},
        },
        {
            "name": "Write",
            "input": {
                "file_path": "transitions/T-E2E-ASSIMILATE/proposal.json",
                "content": "{}\n",
            },
        },
        {
            "name": "Research",
            "input": {"action": "transition_audit"},
        },
        {"name": "Research", "input": {"action": "checkpoint"}},
    ]


def test_verifier_requires_exact_agent_tool_sequence_and_write_payloads() -> None:
    module = _verifier_module()
    tool_uses = _valid_primary_tool_uses()

    payloads = module._validate_primary_tool_sequence(tool_uses)

    assert payloads == {
        "knowledge/claims/C-0001.md": b"claim\n",
        "memory/NOW.md": b"now\n",
        "transitions/T-E2E-ASSIMILATE/proposal.json": b"{}\n",
    }
    with pytest.raises(module.VerificationError, match="Agent tool sequence"):
        module._validate_primary_tool_sequence(
            [item for item in tool_uses if item["name"] != "Eval"]
        )


def test_driver_exposes_one_explicit_aros_entry_and_runtime() -> None:
    result = subprocess.run(
        [sys.executable, str(DRIVER), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "--aros" in result.stdout
    assert "--runtime" in result.stdout
    assert "opencode" not in result.stdout.lower()


def test_driver_uses_native_agent_and_has_no_direct_tool_or_semantic_path() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    direct_execute = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    ]

    assert "build_principal_agent" in source
    assert "run_principal" in source
    assert "initialize_knowledge_bank" in source
    assert 'driver.json_command(\n        "init"' not in source
    assert methods.isdisjoint(
        {
            "_checkpoint_service",
            "_record_tool_result",
            "task_tool",
            "eval_tool",
            "cooperative_checkpoint",
            "_claim",
            "_now",
        }
    )
    assert direct_execute == []


def test_driver_rebuilds_disposable_index_between_destroy_and_restart() -> None:
    source = DRIVER.read_text(encoding="utf-8")

    destroyed = source.index("del agent, provider")
    rebuilt = source.index('driver.json_command("audit", "--rebuild-index"')
    restarted = source.index("restart_provider = provider_type(restart=True)")

    assert destroyed < rebuilt < restarted
