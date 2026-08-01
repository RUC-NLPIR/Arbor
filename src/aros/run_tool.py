"""Principal-facing system call for durable AROS runs."""

from __future__ import annotations

import json
from typing import Any

from ..core.tools.base import Tool
from .runs import RunError, RunService


class RunTool(Tool):
    """Expose durable process control without interpreting experiment results."""

    name = "Run"
    description = (
        "Control durable background experiments. Use action='start' with an exact "
        "argv array and idempotency_key; status/list inspect process state; tail "
        "returns raw stdout or stderr; stop requires a reason. Process completion "
        "is operational evidence, not a scientific verdict."
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

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action")
        if action == "stop" and not kwargs.get("reason"):
            raise RunError("reason is required for stop")

        service = RunService(self.cwd)
        if action == "start":
            manifest = service.prepare(
                kwargs.get("argv"),
                cwd=kwargs.get("cwd", "."),
                timeout_seconds=kwargs.get("timeout_seconds", 3600),
                idempotency_key=kwargs.get("idempotency_key"),
                actor="principal",
                label=kwargs.get("label"),
            )
            result = service.start(str(manifest["run_id"]), actor="principal")
        elif action == "status":
            result = service.status(kwargs.get("run_id"))
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
