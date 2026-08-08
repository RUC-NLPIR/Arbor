from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commissioning.cache_campaign.manifests import freeze_manifests  # noqa: E402
from commissioning.cache_campaign.records import canonical_bytes  # noqa: E402


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(2)


def _error(message: object) -> None:
    single_line = " ".join(str(message).splitlines())
    print(f"error: {single_line}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(add_help=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--task-output", type=Path, required=True)
    parser.add_argument("--host-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        candidate = args.input.resolve(strict=True)
        raw_task_root = args.task_root.absolute()
        if raw_task_root.is_symlink() or not raw_task_root.is_dir():
            raise ValueError("task root must be an existing real directory")
        task_root = raw_task_root.resolve(strict=True)
        task_output = args.task_output.absolute()
        host_output = args.host_output.absolute()
        if os.path.lexists(task_output) or os.path.lexists(host_output):
            raise ValueError("output paths must not exist")
        task, _ = freeze_manifests(candidate, task_root, task_output, host_output)
        result = {
            "r3_commitment_sha256": task["r3_commitment_sha256"],
            "task_manifest": str(task_output / "task.json"),
        }
        sys.stdout.buffer.write(canonical_bytes(result) + b"\n")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _error(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
