"""FileEdit tool — precise string replacement with fuzzy matching.
Description and features ported from Claude Code's FileEditTool."""

from __future__ import annotations

import os
import re
from typing import Any

from .base import Tool

# ---------------------------------------------------------------------------
# Quote normalization for fuzzy matching
# ---------------------------------------------------------------------------

# Mapping of "fancy" quotes to their ASCII equivalents
_QUOTE_MAP = {
    "\u2018": "'",   # left single
    "\u2019": "'",   # right single
    "\u201c": '"',   # left double
    "\u201d": '"',   # right double
    "\u2032": "'",   # prime
    "\u2033": '"',   # double prime
    "\u00ab": '"',   # left guillemet
    "\u00bb": '"',   # right guillemet
}

_QUOTE_RE = re.compile("|".join(re.escape(k) for k in _QUOTE_MAP))


def _normalize_quotes(text: str) -> str:
    """Replace fancy/curly quotes with ASCII equivalents."""
    return _QUOTE_RE.sub(lambda m: _QUOTE_MAP[m.group()], text)


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    """Find the search string in file content with fuzzy quote matching.

    Returns the actual string from the file that matches, or None.
    This handles the common case where the LLM outputs straight quotes
    but the file contains curly quotes, or vice versa.
    """
    # 1. Exact match
    if search_string in file_content:
        return search_string

    # 2. Try with normalized quotes
    normalized_search = _normalize_quotes(search_string)
    normalized_file = _normalize_quotes(file_content)

    idx = normalized_file.find(normalized_search)
    if idx != -1:
        # Return the actual string from the file (preserving original quotes)
        return file_content[idx: idx + len(search_string)]

    # 3. Try with stripped trailing whitespace per line
    search_lines = search_string.split("\n")
    search_stripped = "\n".join(line.rstrip() for line in search_lines)
    file_stripped = "\n".join(line.rstrip() for line in file_content.split("\n"))

    idx = file_stripped.find(search_stripped)
    if idx != -1:
        # Map back to original file content position
        # This is approximate but handles the common case
        return file_content[idx: idx + len(search_stripped)]

    return None


class FileEditTool(Tool):
    name = "Edit"
    description = (
        "Performs exact string replacements in files.\n"
        "\n"
        "Usage:\n"
        "- You must use your Read tool at least once in the conversation "
        "before editing. Understand the file before modifying it.\n"
        "- When editing text from Read tool output, ensure you preserve the "
        "exact indentation (tabs/spaces) as it appears AFTER the line number "
        "prefix. The line number prefix format is: line number + tab. "
        "Everything after that tab is the actual file content to match. "
        "Never include any part of the line number prefix in old_string or "
        "new_string.\n"
        "- ALWAYS prefer editing existing files in the codebase. NEVER write "
        "new files unless explicitly required.\n"
        "- The edit will FAIL if `old_string` is not unique in the file. "
        "Either provide a larger string with more surrounding context to make "
        "it unique, or use `replace_all` to change every instance of "
        "`old_string`.\n"
        "- Use `replace_all` for replacing and renaming strings across the "
        "file. This parameter is useful if you want to rename a variable.\n"
        "- Only use emojis if the user explicitly requests it."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "The exact text to find and replace. Must match the file "
                    "content exactly (including indentation)."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "The text to replace old_string with.",
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "If true, replace all occurrences. "
                    "Default: false (requires unique match)."
                ),
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    is_read_only = False
    max_result_chars = 5_000

    async def execute(self, **kwargs: Any) -> str:
        file_path: str = kwargs["file_path"]
        old_string: str = kwargs["old_string"]
        new_string: str = kwargs["new_string"]
        replace_all: bool = kwargs.get("replace_all", False)

        if not os.path.isabs(file_path):
            file_path = os.path.join(self.cwd, file_path)

        file_path, blocked = self.authorize_path(file_path)
        if blocked:
            return f"BLOCKED: {blocked}"

        if old_string == new_string:
            return "Error: old_string and new_string are identical. No change needed."

        if not os.path.exists(file_path):
            return f"Error: File not found: {file_path}"

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return f"Error reading file: {e}"

        # Try exact match first
        count = content.count(old_string)

        if count == 0:
            # Try fuzzy matching (quote normalization, trailing whitespace)
            actual = _find_actual_string(content, old_string)
            if actual is not None and actual != old_string:
                # Found via fuzzy match — use the actual string
                old_string = actual
                count = content.count(old_string)
            else:
                # Try to give helpful diagnostics
                stripped = old_string.strip()
                if stripped and content.count(stripped) > 0:
                    return (
                        f"Error: old_string not found in {file_path}. "
                        f"However, a stripped version was found {content.count(stripped)} time(s). "
                        f"Check indentation and leading/trailing whitespace. "
                        f"Use Read to see the exact file content."
                    )
                # Check if it's a partial match
                first_line = old_string.split("\n")[0]
                if first_line and first_line in content:
                    return (
                        f"Error: old_string not found in {file_path}. "
                        f"The first line was found but the full multi-line match failed. "
                        f"The file may have changed. Use Read to get the current content."
                    )
                return (
                    f"Error: old_string not found in {file_path}. "
                    f"Use Read to view the current file contents before editing."
                )

        if count > 1 and not replace_all:
            return (
                f"Error: old_string appears {count} times in {file_path}. "
                f"Provide more surrounding context to make it unique, "
                f"or set replace_all=true to replace all occurrences."
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        replacements = count if replace_all else 1
        return (
            f"Successfully edited {file_path} "
            f"({replacements} replacement{'s' if replacements > 1 else ''})."
        )
