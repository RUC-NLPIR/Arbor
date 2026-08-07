"""Replacement deterministic AROS scientific-loop commissioning."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


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
    advance({"task_id": "TASK-live", "state": "running"})
    advance({"task_id": "TASK-live", "state": "completed"})
    advance(
        {
            "task_id": "TASK-live",
            "child_commit": "3" * 40,
            "return_commit": "4" * 40,
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
            "unread_returns": [{"ref": collected_ref}, {"ref": eval_ref}],
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
    messages = _result(messages, response, {"commit": "7" * 40})
    final = asyncio.run(provider.create(system="boot", messages=messages))
    assert final.get_tool_calls() == []
    assert final.get_text() == "Deterministic research loop checkpointed."


def test_restart_provider_requires_no_unread_returns_and_exact_recent_refs() -> None:
    module = _module(PROVIDER, "simple_restart_provider")
    provider = module.SimpleLoopProvider(restart=True)
    messages: list[dict[str, object]] = [{"role": "user", "content": "recover"}]
    response = asyncio.run(provider.create(system="boot", messages=messages))
    task_ref = "tasks/TASK-live/collected.json"
    eval_ref = "eval/evaluations/EVAL-live/receipt.json"
    messages = _result(
        messages,
        response,
        {
            "unread_returns": [],
            "recent_evidence_delta": [
                {"commit": "7" * 40, "observed_refs": [eval_ref, task_ref]}
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
