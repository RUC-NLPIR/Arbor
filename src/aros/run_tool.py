"""Principal-facing system call for durable AROS runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..core.tools.base import Tool
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
        commit_paths: Callable[[tuple[str, ...], str], dict[str, object]]
        | None = None,
        record_observation: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if commit_paths is not None and not callable(commit_paths):
            raise TypeError("commit_paths must be callable or None")
        if record_observation is not None and not callable(record_observation):
            raise TypeError("record_observation must be callable or None")
        self.commit_paths = commit_paths
        self.record_observation = record_observation

    def _committed_result(
        self,
        record: dict[str, object],
        paths: tuple[str, ...],
        message: str,
        *,
        observation: str | None = None,
    ) -> dict[str, object]:
        result = dict(record)
        if self.commit_paths is not None:
            result["checkpoint"] = self.commit_paths(paths, message)
        if observation is not None and self.record_observation is not None:
            self.record_observation(observation)
        return result

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action")
        if action == "stop" and not kwargs.get("reason"):
            raise RunError("reason is required for stop")

        service = RunService(self.cwd)
        if action == "start":
            manifest, paths, message = service.prepare_with_commit(
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
            checkpoint = (
                self.commit_paths(paths, message)
                if self.commit_paths is not None
                else None
            )
            started = service.start(str(manifest["run_id"]), actor="principal")
            result = dict(started)
            if checkpoint is not None:
                result["checkpoint"] = checkpoint
        elif action == "status":
            run_id = kwargs.get("run_id")
            status = service.status(run_id)
            terminal = (
                service.terminal_with_commit(run_id)
                if status.get("state")
                in {"completed", "failed_process", "timed_out", "cancelled"}
                else None
            )
            if terminal is None:
                result = status
            else:
                _final, paths, message = terminal
                result = self._committed_result(
                    status,
                    paths,
                    message,
                    observation=paths[0],
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
