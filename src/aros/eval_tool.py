"""Principal-facing system call for visible AROS evaluation."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

from ..core.tools.base import PathAuthorizer, Tool
from .eval import EvalError, EvalService, ExistingEvaluation


_ACTIONS = ("register", "run", "status", "observe", "audit")


class EvalTool(Tool):
    """Expose visible evaluation operations without interpreting measurements."""

    name = "Eval"
    description = (
        "Register and run visible evaluation apparatus. The apparatus produces "
        "factual measurements; the Principal interprets them. status, observe, and "
        "audit return factual evaluation evidence. Lost evaluations are never retried."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "action": {"const": "register"},
                    "manifest_ref": {
                        "type": "string",
                        "description": (
                            "Tracked visible evaluator manifest; required for register."
                        ),
                    },
                },
                "required": ["action", "manifest_ref"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "run"},
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
                },
                "required": [
                    "action",
                    "evaluator_id",
                    "version",
                    "candidate_commit",
                    "idempotency_key",
                ],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "status"},
                    "eval_id": {
                        "type": "string",
                        "description": "Evaluation ID; required for status.",
                    },
                },
                "required": ["action", "eval_id"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "observe"},
                    "eval_id": {
                        "type": "string",
                        "description": "Evaluation ID; required for observe.",
                    },
                    "stream": {
                        "type": "string",
                        "enum": ["stdout", "stderr"],
                        "description": (
                            "Visible Run stream for observe (default: stdout)."
                        ),
                    },
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 65_536,
                        "description": (
                            "Maximum visible stream bytes (default: 65536)."
                        ),
                    },
                },
                "required": ["action", "eval_id"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "audit"},
                    "eval_id": {
                        "type": "string",
                        "description": "Evaluation ID; required for audit.",
                    },
                },
                "required": ["action", "eval_id"],
                "additionalProperties": False,
            },
        ],
    }
    is_read_only = False
    persist_threshold = float("inf")

    def __init__(
        self,
        *,
        cwd: str,
        workspace_dir: str | None = None,
        path_authorizer: PathAuthorizer | None = None,
        persist_results: bool = True,
        commit_paths: Callable[[tuple[str, ...], str], dict[str, object]]
        | None = None,
        record_observation: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            cwd=cwd,
            workspace_dir=workspace_dir,
            path_authorizer=path_authorizer,
            persist_results=persist_results,
        )
        if commit_paths is not None and not callable(commit_paths):
            raise TypeError("commit_paths must be callable or None")
        if record_observation is not None and not callable(record_observation):
            raise TypeError("record_observation must be callable or None")
        self.commit_paths = commit_paths
        self.record_observation = record_observation
        self.input_schema = copy.deepcopy(type(self).input_schema)

    def to_api_schema(self) -> dict[str, Any]:
        """Return an export that cannot mutate this tool's schema."""
        return copy.deepcopy(super().to_api_schema())

    def _record_terminal_ref(self, result: dict[str, object]) -> None:
        ref = result.get("receipt_ref")
        if not isinstance(ref, str) and result.get("valid") is True:
            checked = result.get("checked_refs")
            if isinstance(checked, list):
                ref = next(
                    (
                        item
                        for item in checked
                        if isinstance(item, str)
                        and item.startswith("eval/evaluations/")
                        and item.endswith("/receipt.json")
                    ),
                    None,
                )
        if isinstance(ref, str) and self.record_observation is not None:
            self.record_observation(ref)

    async def execute(self, **kwargs: Any) -> str:
        """Dispatch one visible evaluation operation."""
        action = kwargs.get("action")
        if action is None:
            raise EvalError("action is required")
        if action not in _ACTIONS:
            raise EvalError(f"unknown evaluation action: {action!r}")

        required = {
            "register": ("manifest_ref",),
            "run": (
                "evaluator_id",
                "version",
                "candidate_commit",
                "idempotency_key",
            ),
            "status": ("eval_id",),
            "observe": ("eval_id",),
            "audit": ("eval_id",),
        }[action]
        missing = [field for field in required if not kwargs.get(field)]
        if missing:
            fields = ", ".join(missing)
            raise EvalError(f"{fields} required for evaluation action {action!r}")

        service = EvalService(self.cwd)
        if action == "register":
            result: dict[str, object] | ExistingEvaluation = service.register(
                kwargs["manifest_ref"],
                actor="principal",
            )
        elif action == "run":
            result, paths, message = service.run_with_commit(
                kwargs["evaluator_id"],
                kwargs["version"],
                kwargs["candidate_commit"],
                actor="principal",
                idempotency_key=kwargs["idempotency_key"],
            )
            if isinstance(result, ExistingEvaluation):
                result = result.status
            if paths is not None and message is not None:
                result = dict(result)
                if self.commit_paths is not None:
                    result["checkpoint"] = self.commit_paths(paths, message)
                if self.record_observation is not None:
                    self.record_observation(paths[0])
        elif action == "status":
            result = service.status(kwargs["eval_id"])
        elif action == "observe":
            output = service.observe(
                kwargs["eval_id"],
                stream=kwargs.get("stream", "stdout"),
                max_bytes=kwargs.get("max_bytes", 65_536),
            )
            self._record_terminal_ref(service.status(kwargs["eval_id"]))
            return output
        else:
            result = service.audit(kwargs["eval_id"])
        if isinstance(result, ExistingEvaluation):
            result = result.status
        self._record_terminal_ref(result)
        return json.dumps(result, ensure_ascii=False, indent=2)
