"""Minimal, versioned workspace support for AROS.

The workspace is the durable project memory.  This module deliberately reads
only explicit project views; chat transcripts, IdeaTree state, and provider
memory are not continuity inputs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


DEFAULT_BOOT_MAX_CHARS = 12_000
_MIN_BOOT_MAX_CHARS = 512
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

    for relative in ("memory", ".aros", ".worktree"):
        (workspace / relative).mkdir(exist_ok=True)

    files = {
        "AGENTS.md": _AGENTS_TEMPLATE,
        "AROS.md": f"# AROS Project\n\n## Mission\n\n{mission.strip()}\n",
        "memory/NOW.md": _NOW_TEMPLATE,
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


def boot_workspace(
    root: str | Path,
    *,
    max_chars: int = DEFAULT_BOOT_MAX_CHARS,
) -> str:
    """Build a compact restart context from durable, explicitly allowed views."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < _MIN_BOOT_MAX_CHARS:
        raise ValueError(f"max_chars must be an integer >= {_MIN_BOOT_MAX_CHARS}")

    workspace = Path(root).expanduser().resolve()
    status = status_workspace(workspace)
    if not status["initialized"]:
        raise ValueError(
            f"workspace is not initialized; run `arbor aros init` at the Git root: {workspace}"
        )
    sections: list[tuple[str, str]] = [
        (
            "Mission and constraints — AROS.md",
            _read_workspace_view(workspace, _VIEWS["mission"], max_chars),
        ),
        (
            "Working memory — memory/NOW.md",
            _read_workspace_view(workspace, _VIEWS["now"], max_chars),
        ),
    ]
    if status["views"]["frontier"]["exists"]:  # type: ignore[index]
        sections.append(
            (
                "Live questions — questions/FRONTIER.md",
                _read_workspace_view(workspace, _VIEWS["frontier"], max_chars),
            )
        )
    runs = status["runs"]
    assert isinstance(runs, dict)
    if runs["items"] or runs["operational_error"]:
        sections.append(("Operational runs", _format_runs(runs)))
    sections.append(("Git and workspace status", _format_status(status)))

    return _render_bounded_sections(sections, max_chars)


def _write_new(path: Path, content: str) -> bool:
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        if not path.is_file():
            raise ValueError(f"expected a file but found a non-file path: {path}")
        return False
    return True


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


def _read_workspace_view(root: Path, relative: str, limit: int) -> str:
    if not _is_workspace_file(root, relative):
        return "(missing)"
    path = root / relative
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(limit + 1)
    except OSError as error:
        return f"(unavailable: {error})"
    if not content:
        return "(empty)"
    if len(content) > limit:
        return _clip(content, limit)
    return content.rstrip()


def _format_status(status: dict[str, object]) -> str:
    git = status["git"]
    assert isinstance(git, dict)
    lines = [
        f"Initialized: {'yes' if status['initialized'] else 'no'}",
        f"Repository: {'yes' if git['is_repository'] else 'no'}",
        f"Branch: {git['branch'] or '(detached or unavailable)'}",
        f"HEAD: {git['head'] or '(unborn or unavailable)'}",
        f"Dirty: {'yes' if git['dirty'] else 'no'}",
    ]
    changes = git["changes"]
    assert isinstance(changes, list)
    if changes:
        lines.append("Changes:")
        lines.extend(f"- {change}" for change in changes)
        if git["changes_truncated"]:
            lines.append("- [additional changes truncated]")
    worktrees = git["worktrees"]
    assert isinstance(worktrees, list)
    if worktrees:
        lines.append("Worktrees:")
        for worktree in worktrees:
            assert isinstance(worktree, dict)
            lines.append(
                f"- {worktree['path']} "
                f"[{worktree['branch'] or 'detached'} @ {worktree['head'] or 'unborn'}]"
            )
        if git["worktrees_truncated"]:
            lines.append("- [additional worktrees truncated]")
    return "\n".join(lines)


def _format_runs(runs: dict[str, object]) -> str:
    error = runs["operational_error"]
    if error:
        return f"Operational error: {error}"

    counts = runs["counts"]
    items = runs["items"]
    assert isinstance(counts, dict)
    assert isinstance(items, list)
    count_text = ", ".join(f"{state}={count}" for state, count in counts.items())
    lines = [f"Total: {runs['total']}", f"States: {count_text or '(none)'}"]
    for item in items:
        assert isinstance(item, dict)
        line = f"- {item['run_id']}: {item['state']}"
        if item.get("updated_at"):
            line += f"; updated={item['updated_at']}"
        if item.get("exit_code") is not None:
            line += f"; exit_code={item['exit_code']}"
        if item.get("reason"):
            line += f"; reason={item['reason']}"
        if item.get("final_ref"):
            line += f"; final={item['final_ref']}"
        lines.append(line)
    if runs["truncated"]:
        lines.append("- [additional runs omitted]")
    return "\n".join(lines)


def _render_bounded_sections(sections: list[tuple[str, str]], limit: int) -> str:
    def render(contents: list[str]) -> str:
        parts = ["# AROS Boot"]
        for (title, _), content in zip(sections, contents):
            parts.extend((f"## {title}", content))
        return "\n\n".join(parts) + "\n"

    empty = render([""] * len(sections))
    available = limit - len(empty)
    if available < len(sections):
        raise ValueError("max_chars is too small for the boot section headers")
    per_section = available // len(sections)
    contents = [_clip(content, per_section) for _, content in sections]
    return render(contents)


def _clip(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    marker = "\n\n[truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return content[: limit - len(marker)].rstrip() + marker
