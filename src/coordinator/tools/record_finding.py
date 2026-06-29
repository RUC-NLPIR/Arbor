"""RecordFinding — let the agent jot a concrete, situational discovery mid-run.

This is the live half (A) of experience capture. When the coordinator or executor
notices something specific and worth remembering for next time — a dataset quirk
that helps the metric, a trap an executor fell into, a gotcha in the harness — it
records it here. Kept specific on purpose; finalize also mines the trajectory (B)
for findings that were never explicitly logged.
"""

from __future__ import annotations

from typing import Any

from ...core.tools.base import Tool


class RecordFindingTool(Tool):
    """Record a concrete discovery or pitfall for future runs to reuse."""

    name = "RecordFinding"
    is_read_only = True  # only appends to a notes file; safe to parallelize
    description = (
        "Record a CONCRETE, situational discovery worth remembering next time you "
        "work on this dataset / task / harness. Two typical kinds:\n"
        "- 'leverage': a specific property you found that helps the metric "
        "(e.g. 'this dataset's labels are noisy above index 9000 — drop them').\n"
        "- 'pitfall': a specific trap to avoid (e.g. 'the executor kept editing "
        "eval.py — remind it the harness is protected').\n"
        "Keep it specific — the value is the detail, not a general principle. Do "
        "NOT log routine progress or generic advice."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "'leverage' or 'pitfall' (free-form ok)."},
            "about": {"type": "string", "description": "What it concerns: the dataset, an executor, the harness, etc."},
            "note": {"type": "string", "description": "The concrete finding, in one or two sentences."},
        },
        "required": ["note"],
    }

    def __init__(self, *, cwd: str, workspace_dir: str | None = None):
        super().__init__(cwd=cwd, workspace_dir=workspace_dir)

    async def execute(self, **kwargs: Any) -> str:
        note = (kwargs.get("note") or "").strip()
        if not note:
            return "Error: 'note' is required."
        try:
            from ...experience import record_finding
            record_finding(self.workspace_dir or self.cwd,
                           kind=kwargs.get("kind", ""), about=kwargs.get("about", ""), note=note)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return f"Could not record finding: {exc}"
        return f"Recorded {kwargs.get('kind') or 'finding'}: {note[:80]}"
