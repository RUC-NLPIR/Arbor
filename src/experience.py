"""Experience capture (self-evolution): two halves.

1. **Live** — ``append_experience`` logs a lesson each time the coordinator
   updates a node mid-run (status/insight), so the research *process* is kept,
   not just the surviving tree.
2. **Consolidate** — ``load_experience`` reads the notes back at finalize; the
   distiller folds them together with the hypothesis tree into reusable skills.

The session dir is recovered from the tree's json_path (<session>/.coordinator/
idea_tree.json). Best-effort throughout — never break a run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXPERIENCE_FILENAME = "experience.jsonl"


def _session_dir(tree_json_path: str | None) -> Path | None:
    if not tree_json_path:
        return None
    p = Path(tree_json_path)
    # <session>/.coordinator/idea_tree.json -> <session>
    return p.parent.parent if p.parent.name == ".coordinator" else p.parent


def append_experience(tree_json_path: str | None, *, node_id: str, updates: dict[str, Any]) -> None:
    sd = _session_dir(tree_json_path)
    if sd is None:
        return
    rec = {
        "node_id": node_id,
        "status": updates.get("status"),
        "insight": (updates.get("insight") or "").strip(),
        "result": (updates.get("result") or "").strip() if updates.get("result") else "",
        "score": updates.get("score"),
    }
    with open(sd / EXPERIENCE_FILENAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_experience(session_dir: Path) -> list[dict[str, Any]]:
    p = Path(session_dir) / EXPERIENCE_FILENAME
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


FINDINGS_FILENAME = "findings.jsonl"


def record_finding(session_dir: str | Path | None, *, kind: str, about: str, note: str,
                   source: str = "agent") -> None:
    """Append one concrete, situational finding (a discovery or a pitfall).

    These are kept specific on purpose — a dataset quirk you can exploit, a trap an
    executor fell into — because their value is the specificity, for the next run on
    the same/similar target. ``kind`` is free-form ('leverage' | 'pitfall' | ...).
    """
    if not session_dir or not (note or "").strip():
        return
    rec = {"kind": (kind or "").strip(), "about": (about or "").strip(),
           "note": note.strip(), "source": source}
    with open(Path(session_dir) / FINDINGS_FILENAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_findings(session_dir: Path) -> list[dict[str, Any]]:
    p = Path(session_dir) / FINDINGS_FILENAME
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
