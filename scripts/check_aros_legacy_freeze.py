#!/usr/bin/env python3
"""Reject additions to frozen legacy AROS control paths."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


FROZEN_ROOTS = (
    "src/coordinator",
    "src/executor",
    "src/run.py",
    "src/review.py",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    return parser.parse_args()


def _is_deletion_only(line: str) -> tuple[bool, str]:
    columns = line.split("\t", 2)
    if len(columns) != 3 or not columns[2]:
        raise ValueError(f"malformed git numstat row: {line!r}")

    additions, deletions, path = columns
    if additions == "-" and deletions == "-":
        return False, path
    if not (
        additions.isascii()
        and additions.isdecimal()
        and deletions.isascii()
        and deletions.isdecimal()
    ):
        raise ValueError(f"malformed git numstat counts: {line!r}")
    return int(additions) == 0 and int(deletions) > 0, path


def main() -> int:
    args = _arguments()
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--numstat",
                "--end-of-options",
                args.base,
                "--",
                *FROZEN_ROOTS,
            ],
            cwd=args.repo,
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, UnicodeError) as error:
        print(f"legacy freeze check failed: {error}", file=sys.stderr)
        return 2

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        print(
            f"legacy freeze check failed for base {args.base!r}: {detail}",
            file=sys.stderr,
        )
        return 2

    violations: list[str] = []
    try:
        for line in result.stdout.splitlines():
            deletion_only, path = _is_deletion_only(line)
            if not deletion_only:
                violations.append(path)
    except ValueError as error:
        print(f"legacy freeze check failed: {error}", file=sys.stderr)
        return 2

    for path in violations:
        print(f"legacy semantic freeze violation: {path}", file=sys.stderr)
    return 2 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
