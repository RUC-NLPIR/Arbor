"""CLI contract tests for visible AROS evaluation."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from arbor.aros.eval import EvalError, ExistingEvaluation
from arbor.cli.commands import aros_cmd


runner = CliRunner()


class FakeEvalService:
    instances: list["FakeEvalService"] = []
    error: Exception | None = None

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[Any, ...]] = []
        self.instances.append(self)

    def _raise_error(self) -> None:
        if self.error is not None:
            raise self.error

    def register(self, manifest_ref: str, *, actor: str) -> dict[str, object]:
        self._raise_error()
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
        self._raise_error()
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
        self._raise_error()
        self.calls.append(("status", eval_id))
        return {"eval_id": eval_id, "evaluation_state": "running"}

    def observe(self, eval_id: str, *, stream: str, max_bytes: int) -> str:
        self._raise_error()
        self.calls.append(("observe", eval_id, stream, max_bytes))
        return "raw visible output\n"

    def audit(self, eval_id: str) -> dict[str, object]:
        self._raise_error()
        self.calls.append(("audit", eval_id))
        return {"schema_version": 1, "eval_id": eval_id, "valid": True}


@pytest.fixture(autouse=True)
def fake_eval_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeEvalService.instances.clear()
    FakeEvalService.error = None
    monkeypatch.setattr(aros_cmd, "EvalService", FakeEvalService, raising=False)


def test_eval_help_exposes_exactly_the_visible_actions_and_interpretation_boundary(
) -> None:
    result = runner.invoke(aros_cmd.aros_app, ["eval", "--help"])

    assert result.exit_code == 0, result.output
    assert [command.name for command in aros_cmd.eval_app.registered_commands] == [
        "register",
        "run",
        "status",
        "observe",
        "audit",
    ]
    help_text = " ".join(result.output.replace("│", " ").lower().split())
    group_help, command_help = help_text.split("commands", maxsplit=1)
    assert "apparatus produces factual measurements" in group_help
    assert "principal interprets" in group_help
    assert "lost evaluations are never retried" in group_help
    for action in ("register", "run", "status", "observe", "audit"):
        assert action in command_help
    for unavailable in ("admit", "protected", "administrator", "mcp"):
        assert unavailable not in help_text


def test_eval_register_forwards_exact_manifest_actor_and_cwd(tmp_path: Path) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "eval",
            "register",
            "--manifest",
            "eval/suites/quality/1/manifest.json",
            "--actor",
            "owner",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeEvalService.instances[0].root == tmp_path.resolve()
    assert FakeEvalService.instances[0].calls == [
        ("register", "eval/suites/quality/1/manifest.json", "owner"),
    ]
    assert json.loads(result.output) == {
        "evaluator_id": "quality",
        "evaluator_version": "1",
    }


def test_eval_run_forwards_exact_candidate_version_key_actor_and_cwd(
    tmp_path: Path,
) -> None:
    candidate = "a" * 40
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "eval",
            "run",
            "quality",
            "1",
            candidate,
            "--idempotency-key",
            "visible-1",
            "--actor",
            "principal",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeEvalService.instances[0].root == tmp_path.resolve()
    assert FakeEvalService.instances[0].calls == [
        ("run", "quality", "1", candidate, "principal", "visible-1"),
    ]
    assert json.loads(result.output) == {
        "eval_id": "EVAL-new",
        "evaluation_state": "completed",
    }


def test_eval_run_returns_existing_lost_status_without_retrying(tmp_path: Path) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "eval",
            "run",
            "quality",
            "1",
            "b" * 40,
            "--idempotency-key",
            "lost-key",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeEvalService.instances[0].calls == [
        ("run", "quality", "1", "b" * 40, "human", "lost-key"),
    ]
    assert json.loads(result.output) == {
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
def test_eval_status_and_audit_forward_directly_as_json(
    tmp_path: Path,
    action: str,
    expected_call: tuple[Any, ...],
    expected_result: dict[str, object],
) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        ["eval", action, "EVAL-test", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert FakeEvalService.instances[0].root == tmp_path.resolve()
    assert FakeEvalService.instances[0].calls == [expected_call]
    assert json.loads(result.output) == expected_result


def test_eval_observe_forwards_exact_bounded_stream_and_prints_verbatim(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "eval",
            "observe",
            "EVAL-test",
            "--stream",
            "stderr",
            "--max-bytes",
            "2048",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "raw visible output\n"
    assert FakeEvalService.instances[0].calls == [
        ("observe", "EVAL-test", "stderr", 2048),
    ]


def test_eval_observe_defaults_to_stdout_and_the_service_bound(tmp_path: Path) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        ["eval", "observe", "EVAL-test", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert FakeEvalService.instances[0].calls == [
        ("observe", "EVAL-test", "stdout", 65_536),
    ]


@pytest.mark.parametrize("max_bytes", ["0", "65537"])
def test_eval_observe_rejects_out_of_bounds_before_service_construction(
    tmp_path: Path,
    max_bytes: str,
) -> None:
    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "eval",
            "observe",
            "EVAL-test",
            "--max-bytes",
            max_bytes,
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert FakeEvalService.instances == []


@pytest.mark.parametrize(
    "args",
    [
        [
            "eval",
            "register",
            "--manifest",
            "eval/suites/quality/1/manifest.json",
        ],
        [
            "eval",
            "run",
            "quality",
            "1",
            "a" * 40,
            "--idempotency-key",
            "visible-1",
        ],
        ["eval", "status", "EVAL-test"],
        ["eval", "observe", "EVAL-test"],
        ["eval", "audit", "EVAL-test"],
    ],
)
def test_eval_service_errors_fail_consistently_with_exit_code_two(
    tmp_path: Path,
    args: list[str],
) -> None:
    FakeEvalService.error = EvalError("invalid visible evaluation")

    result = runner.invoke(
        aros_cmd.aros_app,
        [*args, "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert "error: invalid visible evaluation" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["eval", "register"],
        ["eval", "run", "quality", "1", "a" * 40],
        ["eval", "status"],
        ["eval", "observe"],
        ["eval", "audit"],
    ],
)
def test_eval_commands_require_their_exact_request_fields(args: list[str]) -> None:
    result = runner.invoke(aros_cmd.aros_app, args)

    assert result.exit_code == 2
    assert FakeEvalService.instances == []


def test_eval_cli_is_a_thin_eval_service_adapter() -> None:
    source = Path("src/cli/commands/aros_cmd.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handlers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("eval_")
    ]
    used_names = {
        node.id
        for handler in handlers
        for node in ast.walk(handler)
        if isinstance(node, ast.Name)
    }
    forbidden = {
        "coordinator",
        "executor",
        "eval_records",
        "worktrees",
        "receipts",
        "subprocess",
        "mcp",
    }
    assert handlers
    assert used_names.isdisjoint(forbidden)
