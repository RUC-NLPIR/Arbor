"""Principal-facing system call tests for visible AROS evaluation."""

from __future__ import annotations

import ast
import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from arbor.aros import eval_tool
from arbor.aros.eval import EvalError, ExistingEvaluation
from arbor.aros.eval_tool import EvalTool
from arbor.aros.operational import build_operational_intent


class FakeEvalService:
    instances: list["FakeEvalService"] = []

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.calls: list[tuple[Any, ...]] = []
        self.instances.append(self)

    def register(self, manifest_ref: str, *, actor: str) -> dict[str, object]:
        self.calls.append(("register", manifest_ref, actor))
        return {"evaluator_id": "quality", "evaluator_version": "1"}

    def run(
        self,
        evaluator_id: str,
        version: str,
        candidate_commit: str,
        *,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, object] | ExistingEvaluation:
        self.calls.append(
            (
                "run",
                evaluator_id,
                version,
                candidate_commit,
                actor,
                idempotency_key,
            )
        )
        if idempotency_key == "lost-key":
            return ExistingEvaluation(
                {
                    "eval_id": "EVAL-lost",
                    "evaluation_state": "lost",
                    "referenced_process_state": "running",
                    "measurement_state": "not_available",
                }
            )
        return {
            "eval_id": "EVAL-" + "a" * 64,
            "evaluation_state": "completed",
            "receipt_sha256": "b" * 64,
        }

    def run_with_operational_intent(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        result = self.run(*args, **kwargs)
        projection = result.status if isinstance(result, ExistingEvaluation) else result
        receipt_sha256 = projection.get("receipt_sha256")
        if not isinstance(receipt_sha256, str):
            return result, None
        return result, build_operational_intent(
            (f"eval/evaluations/{projection['eval_id']}/receipt.json",),
            receipt_sha256,
        )

    def status(self, eval_id: str) -> dict[str, object]:
        self.calls.append(("status", eval_id))
        return {"eval_id": eval_id, "evaluation_state": "running"}

    def observe(self, eval_id: str, *, stream: str, max_bytes: int) -> str:
        self.calls.append(("observe", eval_id, stream, max_bytes))
        return "raw visible output\n"

    def audit(self, eval_id: str) -> dict[str, object]:
        self.calls.append(("audit", eval_id))
        return {"schema_version": 1, "eval_id": eval_id, "valid": True}


@pytest.fixture(autouse=True)
def fake_eval_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeEvalService.instances.clear()
    monkeypatch.setattr(eval_tool, "EvalService", FakeEvalService)


def _execute(tool: EvalTool, **kwargs: Any) -> str:
    return asyncio.run(tool.execute(**kwargs))


def _action_rule(schema: dict[str, Any], action: str) -> dict[str, Any]:
    return next(
        branch["properties"]["action"]
        for branch in schema["oneOf"]
        if branch["properties"]["action"].get("const") == action
    )


def test_eval_tool_exposes_only_visible_evaluation_actions(tmp_path: Path) -> None:
    tool = EvalTool(cwd=str(tmp_path))

    assert tool.name == "Eval"
    assert tool.is_read_only is False
    assert tool.persist_threshold == float("inf")
    assert eval_tool._ACTIONS == (
        "register",
        "run",
        "status",
        "observe",
        "audit",
    )
    assert isinstance(eval_tool._ACTIONS, tuple)
    schema = tool.input_schema
    assert set(schema) == {"type", "oneOf"}
    assert schema["type"] == "object"
    expected = [
        ("register", {"action", "manifest_ref"}, ["action", "manifest_ref"]),
        (
            "run",
            {
                "action",
                "evaluator_id",
                "version",
                "candidate_commit",
                "idempotency_key",
            },
            [
                "action",
                "evaluator_id",
                "version",
                "candidate_commit",
                "idempotency_key",
            ],
        ),
        ("status", {"action", "eval_id"}, ["action", "eval_id"]),
        (
            "observe",
            {"action", "eval_id", "stream", "max_bytes"},
            ["action", "eval_id"],
        ),
        ("audit", {"action", "eval_id"}, ["action", "eval_id"]),
    ]
    assert len(schema["oneOf"]) == len(expected)
    for branch, (action, fields, required) in zip(
        schema["oneOf"], expected, strict=True
    ):
        assert set(branch) == {
            "type",
            "properties",
            "required",
            "additionalProperties",
        }
        assert branch["type"] == "object"
        assert branch["additionalProperties"] is False
        assert set(branch["properties"]) == fields
        assert branch["properties"]["action"] == {"const": action}
        assert branch["required"] == required
    observe = schema["oneOf"][3]["properties"]
    assert observe["stream"] == {
        "type": "string",
        "enum": ["stdout", "stderr"],
        "description": "Visible Run stream for observe (default: stdout).",
    }
    assert observe["max_bytes"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 65_536,
        "description": "Maximum visible stream bytes (default: 65536).",
    }
    text = " ".join(tool.description.lower().split())
    assert "apparatus produces factual measurements" in text
    assert "principal interprets" in text
    assert "lost evaluations are never retried" in text
    for unavailable in ("admit", "protected", "administrator", "mcp"):
        assert unavailable not in text


def test_eval_tool_one_of_schema_rejects_cross_action_or_incomplete_requests(
    tmp_path: Path,
) -> None:
    schema = EvalTool(cwd=str(tmp_path)).input_schema
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    valid = [
        {
            "action": "register",
            "manifest_ref": "eval/suites/quality/1/manifest.json",
        },
        {
            "action": "run",
            "evaluator_id": "quality",
            "version": "1",
            "candidate_commit": "a" * 40,
            "idempotency_key": "visible-1",
        },
        {"action": "status", "eval_id": "EVAL-test"},
        {"action": "observe", "eval_id": "EVAL-test"},
        {
            "action": "observe",
            "eval_id": "EVAL-test",
            "stream": "stderr",
            "max_bytes": 65_536,
        },
        {"action": "audit", "eval_id": "EVAL-test"},
    ]
    invalid = [
        {},
        {"action": "admit", "eval_id": "EVAL-test"},
        {"action": "register"},
        {
            "action": "register",
            "manifest_ref": "eval/suites/quality/1/manifest.json",
            "eval_id": "EVAL-test",
        },
        {
            "action": "run",
            "evaluator_id": "quality",
            "version": "1",
            "idempotency_key": "visible-1",
        },
        {"action": "status"},
        {"action": "status", "eval_id": "EVAL-test", "stream": "stdout"},
        {"action": "observe"},
        {"action": "observe", "eval_id": "EVAL-test", "stream": "combined"},
        {"action": "observe", "eval_id": "EVAL-test", "max_bytes": 0},
        {"action": "observe", "eval_id": "EVAL-test", "max_bytes": 65_537},
        {"action": "audit"},
        {"action": "audit", "eval_id": "EVAL-test", "manifest_ref": "x"},
    ]

    for request in valid:
        assert not list(validator.iter_errors(request)), request
    for request in invalid:
        assert list(validator.iter_errors(request)), request


def test_eval_tool_instance_schema_mutation_is_isolated_from_runtime_and_peers(
    tmp_path: Path,
) -> None:
    baseline = copy.deepcopy(EvalTool.input_schema)
    first = EvalTool(cwd=str(tmp_path))
    second = EvalTool(cwd=str(tmp_path))
    action_rule = _action_rule(first.input_schema, "status")
    if isinstance(action_rule.get("enum"), list):
        action_rule["enum"][:] = ["rerun"]
    else:
        action_rule["const"] = "rerun"
    first.input_schema["mutated"] = True

    output = _execute(first, action="status", eval_id="EVAL-test")

    assert json.loads(output) == {
        "eval_id": "EVAL-test",
        "evaluation_state": "running",
    }
    assert second.input_schema == baseline
    assert EvalTool.input_schema == baseline


def test_eval_tool_api_schema_export_is_detached_from_instance_and_class(
    tmp_path: Path,
) -> None:
    baseline = copy.deepcopy(EvalTool.input_schema)
    tool = EvalTool(cwd=str(tmp_path))
    peer = EvalTool(cwd=str(tmp_path))
    exported = tool.to_api_schema()
    exported["input_schema"].clear()

    output = _execute(tool, action="status", eval_id="EVAL-test")

    assert json.loads(output)["evaluation_state"] == "running"
    assert tool.input_schema == baseline
    assert peer.input_schema == baseline
    assert EvalTool.input_schema == baseline
    assert tool.to_api_schema()["input_schema"] == baseline


def test_register_and_run_forward_exact_requests_as_principal(tmp_path: Path) -> None:
    tool = EvalTool(cwd=str(tmp_path))

    registered = _execute(
        tool,
        action="register",
        manifest_ref="eval/suites/quality/1/manifest.json",
    )
    completed = _execute(
        tool,
        action="run",
        evaluator_id="quality",
        version="1",
        candidate_commit="a" * 40,
        idempotency_key="visible-1",
    )

    assert FakeEvalService.instances[0].root == tmp_path
    assert FakeEvalService.instances[0].calls == [
        ("register", "eval/suites/quality/1/manifest.json", "principal"),
    ]
    assert FakeEvalService.instances[1].calls == [
        ("run", "quality", "1", "a" * 40, "principal", "visible-1"),
    ]
    assert json.loads(registered) == {
        "evaluator_id": "quality",
        "evaluator_version": "1",
    }
    assert json.loads(completed) == {
        "eval_id": "EVAL-" + "a" * 64,
        "evaluation_state": "completed",
        "receipt_sha256": "b" * 64,
        "admission_required": True,
        "operational_intent": {
            "schema_version": 1,
            "workspace_paths": [
                "eval/evaluations/EVAL-" + "a" * 64 + "/receipt.json"
            ],
            "record_sha256": "b" * 64,
        },
    }


def test_eval_run_admits_receipt_only_after_terminal_result(tmp_path: Path) -> None:
    calls: list[object] = []

    def admit(intent: object) -> dict[str, object]:
        assert FakeEvalService.instances[0].calls[-1][0] == "run"
        calls.append(intent)
        return {"state": "admitted", "commit": "c" * 40}

    tool = EvalTool(cwd=str(tmp_path), operational_admission=admit)

    output = json.loads(
        _execute(
            tool,
            action="run",
            evaluator_id="quality",
            version="1",
            candidate_commit="a" * 40,
            idempotency_key="visible-callback",
        )
    )

    assert len(calls) == 1
    assert output["admission_required"] is False
    assert output["operational_checkpoint"] == {
        "state": "admitted",
        "commit": "c" * 40,
    }


def test_run_returns_existing_lost_status_without_retrying(tmp_path: Path) -> None:
    tool = EvalTool(cwd=str(tmp_path))

    output = _execute(
        tool,
        action="run",
        evaluator_id="quality",
        version="1",
        candidate_commit="b" * 40,
        idempotency_key="lost-key",
    )

    assert FakeEvalService.instances[0].calls == [
        ("run", "quality", "1", "b" * 40, "principal", "lost-key"),
    ]
    assert json.loads(output) == {
        "eval_id": "EVAL-lost",
        "evaluation_state": "lost",
        "referenced_process_state": "running",
        "measurement_state": "not_available",
    }


@pytest.mark.parametrize(
    ("action", "expected_call", "expected_result"),
    [
        (
            "status",
            ("status", "EVAL-test"),
            {"eval_id": "EVAL-test", "evaluation_state": "running"},
        ),
        (
            "audit",
            ("audit", "EVAL-test"),
            {"schema_version": 1, "eval_id": "EVAL-test", "valid": True},
        ),
    ],
)
def test_status_and_audit_forward_directly_as_json(
    tmp_path: Path,
    action: str,
    expected_call: tuple[Any, ...],
    expected_result: dict[str, object],
) -> None:
    tool = EvalTool(cwd=str(tmp_path))

    output = _execute(tool, action=action, eval_id="EVAL-test")

    assert FakeEvalService.instances[0].calls == [expected_call]
    assert json.loads(output) == expected_result


def test_observe_returns_only_the_exact_bounded_visible_stream(tmp_path: Path) -> None:
    tool = EvalTool(cwd=str(tmp_path))

    default_output = _execute(tool, action="observe", eval_id="EVAL-test")
    bounded_output = _execute(
        tool,
        action="observe",
        eval_id="EVAL-test",
        stream="stderr",
        max_bytes=2048,
    )

    assert default_output == "raw visible output\n"
    assert bounded_output == "raw visible output\n"
    assert FakeEvalService.instances[0].calls == [
        ("observe", "EVAL-test", "stdout", 65_536),
    ]
    assert FakeEvalService.instances[1].calls == [
        ("observe", "EVAL-test", "stderr", 2048),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"action": "unknown"},
        {"action": "register"},
        {"action": "run"},
        {
            "action": "run",
            "evaluator_id": "quality",
            "version": "1",
            "candidate_commit": "a" * 40,
        },
        {"action": "status"},
        {"action": "observe"},
        {"action": "audit"},
    ],
)
def test_eval_tool_rejects_unknown_actions_and_missing_fields(
    tmp_path: Path,
    kwargs: dict[str, Any],
) -> None:
    tool = EvalTool(cwd=str(tmp_path))

    with pytest.raises(EvalError, match="required|unknown"):
        _execute(tool, **kwargs)

    assert FakeEvalService.instances == []


def test_eval_tool_is_a_thin_eval_service_adapter() -> None:
    source = Path("src/aros/eval_tool.py").read_text(encoding="utf-8")
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

    forbidden = (
        "coordinator",
        "executor",
        "eval_records",
        "worktrees",
        "receipts",
        "runs",
        "subprocess",
        "mcp",
    )
    assert not any(part in module for module in imported for part in forbidden)
