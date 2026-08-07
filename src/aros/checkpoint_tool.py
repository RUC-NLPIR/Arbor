"""Principal-facing cooperative Git checkpoint."""

from __future__ import annotations

import json
from typing import Any

from ..core.tools.base import Tool
from .checkpoint import GitCheckpoint
from .observed import ObservedRefs


class CheckpointTool(Tool):
    name = "Checkpoint"
    description = (
        "Commit an intentional set of workspace files on the attached Git branch. "
        "The host automatically records Task, Run, and Eval returns seen in this "
        "session. This is cooperative same-user Git control, not protected authority."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1},
            "paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "uniqueItems": True,
            },
        },
        "required": ["message", "paths"],
        "additionalProperties": False,
    }
    persist_threshold = float("inf")

    def __init__(
        self,
        *,
        observed: ObservedRefs,
        checkpoint: GitCheckpoint | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not isinstance(observed, ObservedRefs):
            raise TypeError("observed must be ObservedRefs")
        self.observed = observed
        self.checkpoint = checkpoint or GitCheckpoint(self.cwd)

    async def execute(self, **kwargs: Any) -> str:
        refs = self.observed.snapshot()
        result = self.checkpoint.commit(
            message=kwargs.get("message"),
            paths=kwargs.get("paths"),
            observed_refs=refs,
        )
        self.observed.clear(refs)
        return json.dumps(result, ensure_ascii=False, indent=2)


__all__ = ["CheckpointTool"]
