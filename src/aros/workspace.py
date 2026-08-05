"""Minimal, versioned workspace support for AROS.

The workspace is the durable project memory.  This module deliberately reads
only explicit project views; chat transcripts, IdeaTree state, and provider
memory are not continuity inputs.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Any

from .attention import (
    DEFAULT_ATTENTION_MAX_CHARS,
    AttentionAuthorityContext,
    ResearchAttentionService,
)


DEFAULT_BOOT_MAX_CHARS = DEFAULT_ATTENTION_MAX_CHARS
_MAX_CHANGES = 100
_MAX_WORKTREES = 50
_MAX_RUNS = 20
_IGNORE_ENTRIES = ("/.aros/", "/.worktree/")
_VIEWS = {
    "mission": "AROS.md",
    "now": "memory/NOW.md",
    "frontier": "questions/FRONTIER.md",
}
_ACTIVE_RUN_STATES = {"prepared", "launched", "running"}
_RUN_SUMMARY_FIELDS = (
    "run_id",
    "state",
    "updated_at",
    "started_at",
    "finished_at",
    "exit_code",
    "reason",
    "final_ref",
    "process_pid",
)

_NOW_TEMPLATE = """# Current State

<!-- Record only evidence-linked state needed by a new principal to continue. -->
"""

_FRONTIER_TEMPLATE = """---
focus_question:
---
# Research Frontier

<!-- The focus is optional. Keep every live branch visible; this is not a queue. -->
"""

_CURRENT_MODEL_TEMPLATE = """# Current Model

<!-- Describe the current explanatory model here when one exists. -->
"""

_AGENTS_TEMPLATE = """# AROS Workspace

Act as the scientific principal for this workspace.

- On boot, read `AROS.md` and `memory/NOW.md`, then inspect Git and active work.
- Treat project files and Git history as durable memory, not chat transcripts.
- Put write-heavy child work only in `.worktree/`.
- Preserve evidence and update `memory/NOW.md` before a material checkpoint.
"""


def init_workspace(root: str | Path, mission: str) -> dict[str, object]:
    """Create the smallest useful AROS workspace without inventing research state.

    ``root`` must already be the top level of a Git repository.  Existing
    project files are always preserved.
    """
    if not isinstance(mission, str) or not mission.strip():
        raise ValueError("mission must be a non-empty string")

    workspace = Path(root).expanduser().resolve()
    _require_git_root(workspace)

    for relative in (
        "memory",
        "questions",
        "model",
        "knowledge/claims",
        "ideas",
        "memory/decisions",
        "transitions",
        ".aros",
        ".worktree",
    ):
        _ensure_scaffold_directory(workspace, relative)

    files = {
        "AGENTS.md": _AGENTS_TEMPLATE,
        "AROS.md": f"# AROS Project\n\n## Mission\n\n{mission.strip()}\n",
        "memory/NOW.md": _NOW_TEMPLATE,
        "questions/FRONTIER.md": _FRONTIER_TEMPLATE,
        "model/CURRENT.md": _CURRENT_MODEL_TEMPLATE,
    }
    created: list[str] = []
    preserved: list[str] = []
    for relative, content in files.items():
        if _write_new(workspace / relative, content):
            created.append(relative)
        else:
            preserved.append(relative)

    updated: list[str] = []
    ignore_path = workspace / ".gitignore"
    if not ignore_path.exists():
        _write_new(ignore_path, "".join(f"{entry}\n" for entry in _IGNORE_ENTRIES))
        created.append(".gitignore")
    elif _append_missing_ignore_entries(ignore_path):
        updated.append(".gitignore")

    return {
        "root": str(workspace),
        "created": created,
        "updated": updated,
        "preserved": preserved,
    }


def status_workspace(root: str | Path) -> dict[str, object]:
    """Return bounded operational facts without interpreting scientific meaning."""
    workspace = Path(root).expanduser().resolve()
    git, git_root = _git_status(workspace)
    views = {
        name: {"path": relative, "exists": _is_workspace_file(workspace, relative)}
        for name, relative in _VIEWS.items()
    }
    return {
        "root": str(workspace),
        "initialized": bool(
            git_root == workspace
            and views["mission"]["exists"]
            and views["now"]["exists"]
        ),
        "git": git,
        "views": views,
        "runs": _run_summary(workspace),
    }


def boot_packet(
    root: str | Path,
    max_chars: int = DEFAULT_BOOT_MAX_CHARS,
    context: AttentionAuthorityContext | None = None,
) -> dict[str, object]:
    """Return the bounded packet used by every boot renderer."""
    return ResearchAttentionService(root).build(max_chars=max_chars, context=context)


def boot_workspace(
    root: str | Path,
    *,
    max_chars: int = DEFAULT_BOOT_MAX_CHARS,
    context: AttentionAuthorityContext | None = None,
) -> str:
    """Render the exact ResearchAttentionPacket built for this boot."""
    return render_boot_packet(boot_packet(root, max_chars=max_chars, context=context))


def render_boot_packet(packet: dict[str, object]) -> str:
    """Render one already-built packet without deriving another observation."""
    return ResearchAttentionService.render_text(packet)


def _write_new(path: Path, content: str) -> bool:
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"expected an ordinary file but found a symlink: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"expected a file but found a non-file path: {path}")
        return False
    return True


def _ensure_scaffold_directory(root: Path, relative: str) -> None:
    current = root
    for component in Path(relative).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(
                f"workspace scaffold must not contain a symlink: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(
                f"workspace scaffold component must be a directory: {current}"
            )


def _append_missing_ignore_entries(path: Path) -> bool:
    content = path.read_text(encoding="utf-8")
    existing = set(content.splitlines())
    missing = [entry for entry in _IGNORE_ENTRIES if entry not in existing]
    if not missing:
        return False

    prefix = "" if not content or content.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "".join(f"{entry}\n" for entry in missing))
    return True


def _require_git_root(root: Path) -> None:
    if not root.is_dir():
        raise ValueError(f"workspace must be an existing Git repository root: {root}")
    top_level = _git_output(root, "rev-parse", "--show-toplevel")
    if top_level is None or Path(top_level).resolve() != root:
        raise ValueError(f"workspace must be the Git repository root: {root}")


def _git_output(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_status(root: Path) -> tuple[dict[str, Any], Path | None]:
    empty: dict[str, Any] = {
        "is_repository": False,
        "branch": None,
        "head": None,
        "dirty": False,
        "changes": [],
        "changes_truncated": False,
        "worktrees": [],
        "worktrees_truncated": False,
    }
    top_level = _git_output(root, "rev-parse", "--show-toplevel")
    if top_level is None:
        return empty, None

    git_root = Path(top_level).resolve()
    branch = _git_output(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = _git_output(root, "rev-parse", "--verify", "HEAD")
    raw_changes = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    change_lines = raw_changes.splitlines() if raw_changes else []
    raw_worktrees = _git_output(root, "worktree", "list", "--porcelain")
    worktrees = _parse_worktrees(raw_worktrees or "")

    return {
        "is_repository": True,
        "branch": branch,
        "head": head,
        "dirty": bool(change_lines),
        "changes": change_lines[:_MAX_CHANGES],
        "changes_truncated": len(change_lines) > _MAX_CHANGES,
        "worktrees": worktrees[:_MAX_WORKTREES],
        "worktrees_truncated": len(worktrees) > _MAX_WORKTREES,
    }, git_root


def _run_summary(root: Path) -> dict[str, object]:
    """Return a compact run inventory from the deterministic run service."""
    from .runs import RunService

    try:
        raw_runs = RunService(root).list()
        for run in raw_runs:
            if not isinstance(run.get("run_id"), str) or not run["run_id"]:
                raise ValueError("run inventory contains an invalid run_id")
            if not isinstance(run.get("state"), str) or not run["state"]:
                raise ValueError("run inventory contains an invalid state")
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "total": None,
            "counts": {},
            "items": [],
            "truncated": False,
            "operational_error": str(error),
        }

    counts: dict[str, int] = {}
    for run in raw_runs:
        state = str(run["state"])
        counts[state] = counts.get(state, 0) + 1

    indexed = list(enumerate(raw_runs))
    ordered = sorted(
        indexed,
        key=lambda pair: (_run_state_priority(str(pair[1]["state"])), -pair[0]),
    )
    items = [_compact_run(run) for _, run in ordered[:_MAX_RUNS]]
    return {
        "total": len(raw_runs),
        "counts": dict(sorted(counts.items())),
        "items": items,
        "truncated": len(raw_runs) > _MAX_RUNS,
        "operational_error": None,
    }


def _run_state_priority(state: str) -> int:
    if state in _ACTIVE_RUN_STATES:
        return 0
    if state == "lost":
        return 1
    return 2


def _compact_run(run: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for field in _RUN_SUMMARY_FIELDS:
        value = run.get(field)
        if value is None:
            continue
        summary[field] = _clip(value, 240) if isinstance(value, str) else value
    return summary


def _clip(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    marker = "\n\n[truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return content[: limit - len(marker)].rstrip() + marker


def _parse_worktrees(raw: str) -> list[dict[str, object]]:
    worktrees: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in [*raw.splitlines(), ""]:
        if line.startswith("worktree "):
            if current is not None:
                worktrees.append(current)
            current = {
                "path": str(Path(line.removeprefix("worktree ")).resolve()),
                "head": None,
                "branch": None,
                "detached": False,
            }
        elif not line and current is not None:
            worktrees.append(current)
            current = None
        elif current is not None and line.startswith("HEAD "):
            value = line.removeprefix("HEAD ")
            current["head"] = None if set(value) == {"0"} else value
        elif current is not None and line.startswith("branch "):
            ref = line.removeprefix("branch ")
            current["branch"] = ref.removeprefix("refs/heads/")
        elif current is not None and line == "detached":
            current["detached"] = True
    return worktrees


def _is_workspace_file(root: Path, relative: str) -> bool:
    path = root / relative
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return path.is_file()
