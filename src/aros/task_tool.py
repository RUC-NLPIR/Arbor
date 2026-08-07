"""Principal-facing system call for durable AROS child tasks."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from ..core.tools.base import Tool
from .observations import ObservationCatalog, ObservationError
from .tasks import TaskError, TaskService


_ACTIONS = [
    "create",
    "start",
    "status",
    "list",
    "message",
    "stop",
    "collect",
    "preserve",
    "prune",
]
_TASK_TRUST_BOUNDARY = (
    "Task adapters are trusted-local and application-scoped, not a security sandbox. "
    "Network and shell capability flags are audit declarations and are not enforced. "
    "Secrets and untrusted adapters are unsupported. Daemonizing or new-session "
    "descendants that do not drain fail closed as lost with no terminal receipt."
)


class TaskTool(Tool):
    """Expose durable child-task lifecycle operations to the Principal."""

    name = "Task"
    description = (
        "Control durable child tasks. create freezes an immutable brief without "
        "launching it; start launches a previously created task. status/list inspect "
        "task state, message appends a mailbox record, stop requests termination, "
        "and collect/preserve/prune manage final child material. "
        + _TASK_TRUST_BOUNDARY
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "description": _TASK_TRUST_BOUNDARY,
        "properties": {
            "action": {"type": "string", "enum": _ACTIONS},
            "task_id": {"type": "string"},
            "objective": {"type": "string"},
            "mode": {"type": "string", "enum": ["read_only", "write"]},
            "adapter_argv": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
            "capabilities": {
                "type": "object",
                "properties": {
                    "network": {"type": "boolean"},
                    "shell": {"type": "boolean"},
                },
                "required": ["network", "shell"],
                "additionalProperties": False,
            },
            "deliverables": {
                "type": "array",
                "items": {"type": "string"},
            },
            "acceptance": {
                "type": "array",
                "items": {"type": "string"},
            },
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
            "idempotency_key": {"type": "string"},
            "message": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    is_read_only = False
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
        paths: tuple[str, ...] | None,
        message: str | None,
        *,
        observations: tuple[str, ...] = (),
    ) -> dict[str, object]:
        result = dict(record)
        committed = paths is None and message is None
        if paths is not None and message is not None and self.commit_paths is not None:
            checkpoint = self.commit_paths(paths, message)
            checkpoint_commit = (
                checkpoint.get("commit") if isinstance(checkpoint, dict) else None
            )
            if observations and (
                not isinstance(checkpoint, dict)
                or not isinstance(checkpoint_commit, str)
                or re.fullmatch(r"[0-9a-f]{40}", checkpoint_commit) is None
                or checkpoint.get("paths") != list(paths)
                or checkpoint.get("enforcement_class") != "cooperative"
            ):
                raise TaskError("invalid task collection checkpoint result")
            result["checkpoint"] = checkpoint
            committed = True
        if committed and self.record_observation is not None:
            try:
                catalog = ObservationCatalog(self.cwd)
                for observation in dict.fromkeys(observations):
                    catalog.resolve(observation)
            except (OSError, ObservationError, RuntimeError, ValueError) as error:
                raise TaskError("invalid committed task observation") from error
            for observation in dict.fromkeys(observations):
                self.record_observation(observation)
        return result

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action")
        if action is None:
            raise TaskError("action is required")
        if action not in _ACTIONS:
            raise TaskError(f"unknown task action: {action!r}")

        required = {
            "create": ("objective", "mode", "adapter_argv", "idempotency_key"),
            "start": ("task_id",),
            "status": ("task_id",),
            "list": (),
            "message": ("task_id", "message"),
            "stop": ("task_id", "reason"),
            "collect": ("task_id",),
            "preserve": ("task_id",),
            "prune": ("task_id",),
        }[action]
        missing = [field for field in required if not kwargs.get(field)]
        if missing:
            fields = ", ".join(missing)
            raise TaskError(f"{fields} required for task action {action!r}")
        if action in {"start", "collect"} and self.commit_paths is None:
            raise TaskError(f"commit_paths required for task action {action!r}")

        service = TaskService(self.cwd)
        if action == "create":
            record, paths, message = service.create_with_commit(
                kwargs["objective"],
                actor="principal",
                mode=kwargs["mode"],
                adapter_argv=kwargs["adapter_argv"],
                capabilities=kwargs.get(
                    "capabilities",
                    {"network": False, "shell": False},
                ),
                deliverables=kwargs.get("deliverables", []),
                acceptance=kwargs.get("acceptance", []),
                timeout_seconds=kwargs.get("timeout_seconds", 3600),
                idempotency_key=kwargs["idempotency_key"],
            )
            result = self._committed_result(record, paths, message)
        elif action == "start":
            result = service.start(
                kwargs["task_id"],
                actor="principal",
                commit_paths=self.commit_paths,
            )
            final_ref = result.get("final_ref")
            if isinstance(final_ref, str) and self.record_observation is not None:
                self.record_observation(final_ref)
        elif action == "status":
            result = service.status(
                kwargs["task_id"],
                commit_paths=self.commit_paths,
            )
            final_ref = result.get("final_ref")
            if isinstance(final_ref, str) and self.record_observation is not None:
                self.record_observation(final_ref)
        elif action == "list":
            result = service.list()
        elif action == "message":
            result = service.message(
                kwargs["task_id"],
                kwargs["message"],
                "principal",
            )
        elif action == "stop":
            result = service.stop(
                kwargs["task_id"],
                actor="principal",
                reason=kwargs["reason"],
            )
        elif action == "collect":
            assert self.commit_paths is not None
            collected_record, checkpoint = service.collect_and_commit(
                kwargs["task_id"],
                self.commit_paths,
            )
            run_final_ref = collected_record.get("run_final_ref")
            observations: tuple[str, ...] = (
                (str(run_final_ref),)
                if isinstance(run_final_ref, str)
                else ()
            )
            if isinstance(collected_record.get("collected_sha256"), str):
                observations += (f"tasks/{kwargs['task_id']}/collected.json",)
            result = self._committed_result(
                collected_record,
                None,
                None,
                observations=observations,
            )
            if checkpoint is not None:
                result["checkpoint"] = checkpoint
        elif action == "preserve":
            result = service.preserve(kwargs["task_id"])
        else:
            result = service.prune(kwargs["task_id"])
        return json.dumps(result, ensure_ascii=False, indent=2)
