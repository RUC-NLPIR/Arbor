"""Principal-facing system call for durable AROS child tasks."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..core.tools.base import Tool
from .operational import OperationalIntent
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
        intent: OperationalIntent | None,
    ) -> dict[str, object]:
        if intent is None:
            return record
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

        service = TaskService(self.cwd)
        if action == "create":
            record, intent = service.create_with_operational_intent(
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
            result = self._operational_result(record, intent)
        elif action == "start":
            result = service.start(kwargs["task_id"], actor="principal")
        elif action == "status":
            result = service.status(kwargs["task_id"])
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
            record, intent = service.collect_with_operational_intent(
                kwargs["task_id"]
            )
            result = self._operational_result(record, intent)
        elif action == "preserve":
            result = service.preserve(kwargs["task_id"])
        else:
            result = service.prune(kwargs["task_id"])
        return json.dumps(result, ensure_ascii=False, indent=2)
