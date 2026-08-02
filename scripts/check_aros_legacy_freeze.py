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
    "src/cli/commands/run.py",
)
GROWTH_ROOTS = (
    "src/aros",
)
GROWTH_FILES = (
    "src/cli/aros_app.py",
    "src/cli/commands/aros_cmd.py",
)
AROS_RETIREMENT_GATE_E4 = "6e406e7fc783f6c7df5fa348dbed6e68790ba90a"
AROS_RETIREMENT_GATE_E4_PATH = "src/cli/app.py"
AROS_RETIREMENT_GATE_E4_MODE = "100644"
Change = tuple[str, tuple[str, ...], str, str, str, str]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    return parser.parse_args()


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or result.stderr:
        detail = (result.stderr.strip() or result.stdout.strip()).decode(
            errors="replace"
        )
        raise ValueError(detail or f"git {args[0]} exited {result.returncode}")
    return result.stdout


def _git(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode()


def _nul_fields(output: str) -> list[str]:
    if not output:
        return []
    if not output.endswith("\0"):
        raise ValueError("malformed NUL-delimited Git output")
    fields = output[:-1].split("\0")
    if any(not field for field in fields):
        raise ValueError("empty field in NUL-delimited Git output")
    return fields


def _parse_raw(output: str) -> list[Change]:
    fields = _nul_fields(output)
    changes: list[Change] = []
    position = 0
    while position < len(fields):
        header = fields[position]
        position += 1
        parts = header.split()
        if len(parts) != 5 or not parts[0].startswith(":"):
            raise ValueError(f"malformed Git raw header: {header!r}")
        old_mode = parts[0][1:]
        new_mode, old_oid, new_oid, status = parts[1:]
        if any(
            len(mode) != 6 or any(character not in "01234567" for character in mode)
            for mode in (old_mode, new_mode)
        ):
            raise ValueError(f"malformed Git mode: {header!r}")
        if any(
            not oid or any(character not in "0123456789abcdef" for character in oid)
            for oid in (old_oid, new_oid)
        ):
            raise ValueError(f"malformed Git object ID: {header!r}")
        kind = status[0]
        if kind in {"R", "C"}:
            if not status[1:].isascii() or not status[1:].isdecimal():
                raise ValueError(f"malformed Git status: {status!r}")
            path_count = 2
        elif len(status) == 1 and kind in {"A", "D", "M", "T", "U", "X", "B"}:
            path_count = 1
        else:
            raise ValueError(f"unknown Git status: {status!r}")
        if position + path_count > len(fields):
            raise ValueError(f"missing path for Git status: {status!r}")
        paths = tuple(fields[position : position + path_count])
        position += path_count
        changes.append((status, paths, old_mode, new_mode, old_oid, new_oid))
    return changes


def _is_frozen(path: str) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in FROZEN_ROOTS)


def _is_source(path: str) -> bool:
    return path == "src" or path.startswith("src/")


def _allows_growth(path: str) -> bool:
    return path in GROWTH_FILES or any(
        path == root or path.startswith(f"{root}/")
        for root in GROWTH_ROOTS
    )


def _worktree_blob_oid(repo: Path, path: str) -> str:
    return _git(
        repo,
        "hash-object",
        "-w",
        f"--path={path}",
        "--",
        path,
    ).strip()


def _is_approved_e4_untracked(repo: Path, path: str) -> bool:
    if path != AROS_RETIREMENT_GATE_E4_PATH:
        return False
    current_path = repo / path
    return (
        current_path.is_file()
        and not current_path.is_symlink()
        and not current_path.stat().st_mode & 0o111
        and _worktree_blob_oid(repo, path) == AROS_RETIREMENT_GATE_E4
    )


def _text_lines(
    repo: Path,
    path: str,
    *,
    blob_oid: str | None = None,
) -> list[bytes] | None:
    if blob_oid is None:
        current_path = repo / path
        if current_path.is_symlink() or not current_path.is_file():
            return None
        normalized_oid = _worktree_blob_oid(repo, path)
        content = _git_bytes(repo, "cat-file", "blob", normalized_oid)
    else:
        content = _git_bytes(repo, "cat-file", "blob", blob_oid)
    if b"\0" in content:
        return None
    return content.splitlines(keepends=True)


def _contains_added_lines(before: list[bytes], after: list[bytes]) -> bool:
    candidates = iter(before)
    return not all(any(candidate == line for candidate in candidates) for line in after)


def _violates_source_growth(
    repo: Path,
    change: Change,
    *,
    staged: bool,
) -> bool:
    status, paths, old_mode, new_mode, old_oid, new_oid = change
    kind = status[0]
    path = paths[-1]
    if kind == "D" or not _is_source(path):
        return False
    if path == AROS_RETIREMENT_GATE_E4_PATH:
        resulting_oid = new_oid if staged else _worktree_blob_oid(repo, path)
        return not (
            new_mode == AROS_RETIREMENT_GATE_E4_MODE
            and resulting_oid == AROS_RETIREMENT_GATE_E4
        )
    if _allows_growth(path):
        return False
    if kind in {"A", "R", "C", "T"} or new_mode in {"120000", "160000"}:
        return True
    if new_mode not in {"100644", "100755"}:
        return False
    after = _text_lines(
        repo,
        path,
        blob_oid=new_oid if staged else None,
    )
    if after is None:
        return kind == "M"
    if old_mode not in {"100644", "100755"}:
        return bool(after)
    before_content = _git_bytes(repo, "cat-file", "blob", old_oid)
    if b"\0" in before_content:
        return kind == "M" or bool(after)
    return _contains_added_lines(
        before_content.splitlines(keepends=True),
        after,
    )


def _is_text_deletion_only(
    repo: Path,
    change: Change,
    *,
    staged: bool,
) -> bool:
    _, paths, old_mode, new_mode, old_oid, new_oid = change
    if old_mode != new_mode or old_mode not in {"100644", "100755"}:
        return False
    before = _text_lines(repo, paths[0], blob_oid=old_oid)
    after = _text_lines(
        repo,
        paths[0],
        blob_oid=new_oid if staged else None,
    )
    if before is None or after is None:
        return False
    if len(after) >= len(before):
        return False
    candidates = iter(before)
    return all(any(candidate == line for candidate in candidates) for line in after)


def _find_violations(
    repo: Path,
    changes: list[Change],
    untracked: list[str],
    *,
    staged: bool,
) -> list[str]:
    if any(not _is_source(path) for path in untracked):
        raise ValueError("Git returned an out-of-scope untracked path")
    violations = [
        path
        for path in untracked
        if _is_frozen(path)
        or (not _allows_growth(path) and not _is_approved_e4_untracked(repo, path))
    ]
    for change in changes:
        status, paths, _, _, _, _ = change
        kind = status[0]
        frozen_paths = [path for path in paths if _is_frozen(path)]
        if frozen_paths:
            is_r100_move_out = (
                status == "R100"
                and _is_source(paths[0])
                and not _is_source(paths[1])
            )
            if is_r100_move_out:
                pass
            elif kind in {"R", "C"}:
                violations.extend(paths)
            elif kind == "D":
                pass
            elif kind == "M":
                path = paths[0]
                if not _is_text_deletion_only(repo, change, staged=staged):
                    violations.append(path)
            else:
                violations.extend(frozen_paths)
        if _violates_source_growth(repo, change, staged=staged):
            violations.append(paths[-1])
    return list(dict.fromkeys(violations))


def _diff_changes(repo: Path, base: str, *, staged: bool) -> list[Change]:
    cached = ("--cached",) if staged else ()
    return _parse_raw(
        _git(
            repo,
            "diff",
            *cached,
            "--raw",
            "-z",
            "--no-abbrev",
            "--find-renames=50%",
            "--find-copies=50%",
            "--find-copies-harder",
            "-l1000",
            "--end-of-options",
            base,
            "--",
        )
    )


def main() -> int:
    args = _arguments()
    try:
        repo = args.repo.resolve()
        top_level_output = _git(repo, "rev-parse", "--show-toplevel")
        top_level_lines = top_level_output.splitlines()
        if len(top_level_lines) != 1:
            raise ValueError("Git returned an invalid top level")
        top_level = Path(top_level_lines[0]).resolve()
        if repo != top_level:
            raise ValueError(
                f"--repo must resolve exactly to the Git top level: {repo} != {top_level}"
            )

        changes = _diff_changes(repo, args.base, staged=False)
        staged_changes = _diff_changes(repo, args.base, staged=True)
        untracked = _nul_fields(
            _git(
                repo,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                "src",
            )
        )
        violations = _find_violations(
            repo,
            changes,
            untracked,
            staged=False,
        )
        violations.extend(
            _find_violations(
                repo,
                staged_changes,
                [],
                staged=True,
            )
        )
        violations = list(dict.fromkeys(violations))
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(
            f"legacy freeze check failed for base {args.base!r}: {error}",
            file=sys.stderr,
        )
        return 2

    for path in violations:
        print(f"legacy source freeze violation: {path}", file=sys.stderr)
    return 2 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
