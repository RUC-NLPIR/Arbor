"""Principal-facing system call tests for visible AROS evaluation."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from arbor.aros import eval_tool
from arbor.aros.eval import EvalError, ExistingEvaluation
from arbor.aros.eval_tool import EvalTool


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
        return {"eval_id": "EVAL-new", "evaluation_state": "completed"}

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


def test_eval_tool_exposes_only_visible_evaluation_actions(tmp_path: Path) -> None:
    tool = EvalTool(cwd=str(tmp_path))

    assert tool.name == "Eval"
    assert tool.is_read_only is False
    assert tool.persist_threshold == float("inf")
    assert tool.input_schema == {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["register", "run", "status", "observe", "audit"],
            },
            "manifest_ref": {
                "type": "string",
                "description": "Tracked visible evaluator manifest; required for register.",
            },
            "evaluator_id": {
                "type": "string",
                "description": "Registered evaluator ID; required for run.",
            },
            "version": {
                "type": "string",
                "description": "Registered evaluator version; required for run.",
            },
            "candidate_commit": {
                "type": "string",
                "description": "Exact candidate Git commit; required for run.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "One-attempt request key; required for run.",
            },
            "eval_id": {
                "type": "string",
                "description": "Evaluation ID; required for status, observe, and audit.",
            },
            "stream": {
                "type": "string",
                "enum": ["stdout", "stderr"],
                "description": "Visible Run stream for observe (default: stdout).",
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65536,
                "description": "Maximum visible stream bytes (default: 65536).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    text = " ".join(tool.description.lower().split())
    assert "apparatus produces factual measurements" in text
    assert "principal interprets" in text
    assert "lost evaluations are never retried" in text
    for unavailable in ("admit", "protected", "administrator", "mcp"):
        assert unavailable not in text


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
        "eval_id": "EVAL-new",
        "evaluation_state": "completed",
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
