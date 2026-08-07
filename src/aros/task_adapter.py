"""Thin Task command adapter executed by the shared durable Run carrier."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

_ENVIRONMENT_KEYS = (
    "BLIS_NUM_THREADS",
    "CUDA_VISIBLE_DEVICES",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "PATH",
    "TZ",
    "VECLIB_MAXIMUM_THREADS",
)


def build_adapter_environment(
    runtime: Path,
    *,
    task_id: str,
    brief_path: Path,
    worktree: Path,
    base_commit: str,
    brief_sha256: str,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    ambient = os.environ if source is None else source
    environment = {key: ambient[key] for key in _ENVIRONMENT_KEYS if key in ambient}
    environment.update(
        {
            "HOME": str(runtime / "home"),
            "TMPDIR": str(runtime / "tmp"),
            "AROS_TASK_ID": task_id,
            "AROS_TASK_BRIEF": str(brief_path),
            "AROS_TASK_WORKTREE": str(worktree),
            "AROS_TASK_BASE_COMMIT": base_commit,
            "AROS_TASK_BRIEF_SHA256": brief_sha256,
        }
    )
    return environment


def load_adapter_context(workspace: Path, task_id: str) -> dict[str, object]:
    from .tasks import TaskService

    return TaskService(workspace).adapter_context(task_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)
    context = load_adapter_context(args.workspace, args.task_id)
    if not isinstance(context, dict):
        raise ValueError("Task adapter context is invalid")
    command = context.get("argv")
    worktree = context.get("worktree")
    environment = context.get("environment")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
        or not isinstance(worktree, str)
        or not worktree
        or not isinstance(environment, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        )
    ):
        raise ValueError("Task adapter context is invalid")
    os.chdir(worktree)
    os.execvpe(command[0], command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
