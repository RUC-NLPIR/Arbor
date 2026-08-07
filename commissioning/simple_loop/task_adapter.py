from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _json_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def main() -> None:
    task_id = os.environ["AROS_TASK_ID"]
    brief_sha256 = os.environ["AROS_TASK_BRIEF_SHA256"]
    base_commit = os.environ["AROS_TASK_BASE_COMMIT"]
    Path("candidate-mode.txt").write_text("success\n", encoding="utf-8")
    _git("add", "--", "candidate-mode.txt")
    _git("commit", "-qm", "produce deterministic candidate")
    child_commit = _git("rev-parse", "HEAD")
    returned: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief_sha256,
        "base_commit": base_commit,
        "child_commit": child_commit,
        "summary": "Produced the deterministic success candidate.",
        "work_performed": ["wrote candidate-mode.txt"],
        "changed_files": ["candidate-mode.txt"],
        "evidence": ["child commit records exact candidate bytes"],
        "deviations": [],
        "uncertainty": [],
        "follow_up": ["evaluate the exact child commit"],
    }
    returned["return_sha256"] = _json_sha256(returned)
    return_path = Path("tasks") / task_id / "return.json"
    return_path.parent.mkdir(parents=True, exist_ok=True)
    return_path.write_text(
        json.dumps(returned, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _git("add", "--", return_path.as_posix())
    _git("commit", "-qm", "record deterministic task return")


if __name__ == "__main__":
    main()
