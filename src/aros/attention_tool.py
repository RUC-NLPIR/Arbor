"""Principal-facing bounded restart attention."""

from __future__ import annotations

from typing import Any

from ..core.tools.base import Tool
from .attention import (
    DEFAULT_ATTENTION_MAX_CHARS,
    MAX_ATTENTION_MAX_CHARS,
    MIN_ATTENTION_MAX_CHARS,
    AttentionAuthorityContext,
    ResearchAttentionService,
)
from .worktrees import RepositoryBinding


class AttentionTool(Tool):
    name = "Attention"
    description = (
        "Read a bounded restart packet derived from canonical research files, "
        "Git history, and current Task, Run, and Eval facts."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "minimum": MIN_ATTENTION_MAX_CHARS,
                "maximum": MAX_ATTENTION_MAX_CHARS,
            }
        },
        "additionalProperties": False,
    }
    is_read_only = True
    persist_threshold = float("inf")

    def __init__(
        self,
        *,
        canonical_repository: RepositoryBinding | None = None,
        context: AttentionAuthorityContext | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.context = context
        self.service = ResearchAttentionService(
            self.cwd,
            canonical_repository=canonical_repository,
        )

    async def execute(self, **kwargs: Any) -> str:
        packet = self.service.build(
            max_chars=kwargs.get("max_chars", DEFAULT_ATTENTION_MAX_CHARS),
            context=self.context,
        )
        return self.service.render_text(packet)


__all__ = ["AttentionTool"]
