"""Principal-facing native Research system call."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ..core.tools.base import PathAuthorizer, Tool
from .attention import (
    DEFAULT_ATTENTION_MAX_CHARS,
    MAX_ATTENTION_MAX_CHARS,
    MIN_ATTENTION_MAX_CHARS,
    AttentionAuthorityContext,
    ResearchAttentionService,
)
from .checkpoint import AdmissionGateway, CheckpointError, CheckpointService
from .transitions import TransitionAuditService
from .worktrees import RepositoryBinding


_ACTION_FIELDS = {
    "attention": frozenset({"action", "max_chars"}),
    "transition_audit": frozenset({"action", "proposal_ref"}),
    "checkpoint": frozenset({"action", "proposal_ref", "message"}),
}
_REQUIRED_FIELDS = {
    "attention": frozenset({"action"}),
    "transition_audit": frozenset({"action", "proposal_ref"}),
    "checkpoint": frozenset({"action", "proposal_ref", "message"}),
}


class ResearchTool(Tool):
    """Expose bounded attention, transition audit, and admitted checkpointing."""

    name = "Research"
    description = (
        "Observe bounded research state, audit an explicit transition proposal, "
        "or request a checkpoint through host-supplied admission authority."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "action": {"const": "attention"},
                    "max_chars": {
                        "type": "integer",
                        "minimum": MIN_ATTENTION_MAX_CHARS,
                        "maximum": MAX_ATTENTION_MAX_CHARS,
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "transition_audit"},
                    "proposal_ref": {"type": "string"},
                },
                "required": ["action", "proposal_ref"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"const": "checkpoint"},
                    "proposal_ref": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["action", "proposal_ref", "message"],
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
        canonical_repository: RepositoryBinding,
        canonical_ref: str,
        admission_gateway: AdmissionGateway | None = None,
        attention_context: AttentionAuthorityContext | None = None,
        workspace_dir: str | None = None,
        path_authorizer: PathAuthorizer | None = None,
        persist_results: bool = True,
    ) -> None:
        super().__init__(
            cwd=cwd,
            workspace_dir=workspace_dir,
            path_authorizer=path_authorizer,
            persist_results=persist_results,
        )
        if attention_context is not None and not isinstance(
            attention_context,
            AttentionAuthorityContext,
        ):
            raise TypeError(
                "attention_context must be an AttentionAuthorityContext or None"
            )
        self.input_schema = copy.deepcopy(type(self).input_schema)
        self.candidate_root = Path(cwd).expanduser().resolve()
        self.canonical_repository = canonical_repository
        self.canonical_ref = canonical_ref
        self.admission_gateway = admission_gateway
        self.attention_context = attention_context
        candidate = str(self.candidate_root)
        self.attention_service = ResearchAttentionService(
            candidate,
            canonical_repository=canonical_repository,
        )
        self.audit_service = TransitionAuditService(
            candidate,
            canonical_ref=canonical_ref,
        )
        self.checkpoint_service = CheckpointService(
            candidate,
            canonical_repository=canonical_repository,
            canonical_ref=canonical_ref,
            audit_service=self.audit_service,
            gateway=admission_gateway,
        )

    def to_api_schema(self) -> dict[str, Any]:
        """Return an export that cannot mutate the sealed model schema."""
        return copy.deepcopy(super().to_api_schema())

    async def execute(self, **kwargs: Any) -> str:
        """Dispatch one exact Research action through constructor-owned services."""
        action = kwargs.get("action")
        if action not in _ACTION_FIELDS:
            raise ValueError(f"unknown Research action: {action!r}")
        provided = frozenset(kwargs)
        unexpected = provided - _ACTION_FIELDS[action]
        if unexpected:
            raise ValueError(
                "unexpected Research arguments: " + ", ".join(sorted(unexpected))
            )
        missing = _REQUIRED_FIELDS[action] - provided
        if missing:
            raise ValueError(
                "missing Research arguments: " + ", ".join(sorted(missing))
            )

        if action == "attention":
            result = self.attention_service.build(
                max_chars=kwargs.get("max_chars", DEFAULT_ATTENTION_MAX_CHARS),
                context=self.attention_context,
            )
            return self.attention_service.render_text(result)
        elif action == "transition_audit":
            result = self.audit_service.audit(kwargs["proposal_ref"])
        else:
            if self.admission_gateway is None:
                raise CheckpointError(
                    "checkpoint requires an injected admission gateway"
                )
            result = self.checkpoint_service.checkpoint(
                kwargs["proposal_ref"],
                kwargs["message"],
            )
        return json.dumps(result, ensure_ascii=False, indent=2)


__all__ = ["ResearchTool"]
