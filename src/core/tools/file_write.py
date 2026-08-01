"""FileWrite tool — create or overwrite files.
Description ported from Claude Code's FileWriteTool."""

from __future__ import annotations

import os
from typing import Any

from .base import Tool


class FileWriteTool(Tool):
    name = "Write"
    description = (
        "Writes a file to the local filesystem.\n"
        "\n"
        "Usage:\n"
        "- This tool will overwrite the existing file if there is one at the "
        "provided path.\n"
        "- If this is an existing file, you MUST use the Read tool first to "
        "read the file's contents. This ensures you understand what you're "
        "overwriting.\n"
        "- Prefer the Edit tool for modifying existing files — it only sends "
        "the diff. Only use this tool to create new files or for complete "
        "rewrites.\n"
        "- NEVER create documentation files (*.md) or README files unless "
        "explicitly requested.\n"
        "- Only use emojis if the user explicitly requests it."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "The complete content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    }
    is_read_only = False
    max_result_chars = 1_000

    async def execute(self, **kwargs: Any) -> str:
        file_path: str = kwargs["file_path"]
        content: str = kwargs["content"]

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.cwd, file_path)

        file_path, blocked = self.authorize_path(file_path)
        if blocked:
            return f"BLOCKED: {blocked}"

        # Create parent directories
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        existed = os.path.exists(file_path)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return f"Error writing file: {e}"

        action = "Overwrote" if existed else "Created"
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"{action} {file_path} ({line_count} lines)."
