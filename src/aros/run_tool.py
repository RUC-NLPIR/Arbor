"""Principal-facing system call for durable AROS runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..core.tools.base import Tool
from .operational import OperationalIntent
from .runs import RunError, RunService


class RunTool(Tool):
    """Expose durable process control without interpreting experiment results."""

    name = "Run"
    description = (
        "Control durable background experiments. Use action='start' with an exact "
        "argv array and idempotency_key. Runs default to isolated-linux; select "
        "trusted-local explicitly. status/list inspect process state; tail returns "
        "raw stdout or stderr; stop requires a reason. Process completion is "
        "operational evidence, not a scientific verdict."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "status", "list", "tail", "stop"],
            },
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact command argv; required for start.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Stable deduplication key; required for start.",
            },
            "cwd": {
                "type": "string",
                "description": "Workspace-relative command directory (default: '.').",
            },
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Hard process timeout in seconds (default: 3600).",
            },
            "label": {
                "type": "string",
                "description": "Optional short run label.",
            },
            "security_profile": {
                "type": "string",
                "enum": ["isolated-linux", "trusted-local"],
                "default": "isolated-linux",
                "description": "Run isolation profile (default: isolated-linux).",
            },
            "writable_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Workspace-relative paths writable by the run.",
            },
            "run_id": {
                "type": "string",
                "description": "Run ID; required for status, tail, and stop.",
            },
            "stream": {
                "type": "string",
                "enum": ["stdout", "stderr"],
                "description": "Log stream for tail (default: stdout).",
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum trailing log bytes (default: 65536).",
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "description": "Auditable stop reason; required for stop.",
            },
            "signal_name": {
                "type": "string",
                "enum": ["TERM", "INT", "KILL"],
                "description": "Stop signal (default: TERM).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    persist_threshold = float("inf")

    def __init__(
        self,
        *,
        operational_admission: Callable[[OperationalIntent], dict[str, object]]
        | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if operational_admission is not None and not callable(operational_admission):
            raise TypeError("operational_admission must be callable or None")
        self.operational_admission = operational_admission

    def _operational_result(
        self,
        record: dict[str, object],
        intent: OperationalIntent,
    ) -> dict[str, object]:
        result = {
            **record,
            "admission_required": self.operational_admission is None,
            "operational_intent": intent.to_json(),
        }
        if self.operational_admission is not None:
            result["operational_checkpoint"] = self.operational_admission(intent)
        return result

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action")
        if action == "stop" and not kwargs.get("reason"):
            raise RunError("reason is required for stop")

        service = RunService(self.cwd)
        if action == "start":
            manifest, intent = service.prepare_with_operational_intent(
                kwargs.get("argv"),
                cwd=kwargs.get("cwd", "."),
                timeout_seconds=kwargs.get("timeout_seconds", 3600),
                idempotency_key=kwargs.get("idempotency_key"),
                actor="principal",
                label=kwargs.get("label"),
                security_profile=kwargs.get(
                    "security_profile",
                    "isolated-linux",
                ),
                writable_paths=kwargs.get("writable_paths", []),
            )
            started = service.start(str(manifest["run_id"]), actor="principal")
            result = self._operational_result(started, intent)
        elif action == "status":
            run_id = kwargs.get("run_id")
            status = service.status(run_id)
            intent = (
                service.terminal_operational_intent(run_id)
                if status.get("state")
                in {"completed", "failed_process", "timed_out", "cancelled"}
                else None
            )
            result = (
                self._operational_result(status, intent)
                if intent is not None
                else status
            )
        elif action == "list":
            result = service.list()
        elif action == "tail":
            return service.tail(
                kwargs.get("run_id"),
                stream=kwargs.get("stream", "stdout"),
                max_bytes=kwargs.get("max_bytes", 65_536),
            )
        elif action == "stop":
            result = service.stop(
                kwargs.get("run_id"),
                actor="principal",
                reason=kwargs["reason"],
                signal_name=kwargs.get("signal_name", "TERM"),
            )
        else:
            raise RunError(f"unknown run action: {action!r}")
        return json.dumps(result, ensure_ascii=False, indent=2)
