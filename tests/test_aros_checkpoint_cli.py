"""Direct cooperative selected-path checkpoint CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from arbor.cli.commands.aros_cmd import aros_app


runner = CliRunner()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(root: Path) -> str:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Checkpoint CLI Test")
    _git(root, "config", "user.email", "checkpoint@example.invalid")
    (root / ".gitignore").write_text("/.aros/\n/.worktree/\n", encoding="utf-8")
    (root / "memory").mkdir()
    (root / "memory/NOW.md").write_text("# NOW\nold\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "memory/NOW.md")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


def test_checkpoint_commits_exact_paths_and_labels_cooperative_boundary(
    tmp_path: Path,
) -> None:
    parent = _repo(tmp_path)
    (tmp_path / "memory/NOW.md").write_text("# NOW\nnew\n", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("preserve\n", encoding="utf-8")

    result = runner.invoke(
        aros_app,
        [
            "checkpoint",
            "--cwd",
            str(tmp_path),
            "--message",
            "Revise current state",
            "--path",
            "memory/NOW.md",
        ],
    )

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["parent"] == parent
    assert output["paths"] == ["memory/NOW.md"]
    assert output["enforcement_class"] == "cooperative"
    assert output["checkpoint_authority"] == "human"
    assert _git(tmp_path, "show", "HEAD:memory/NOW.md") == "# NOW\nnew"
    assert _git(tmp_path, "status", "--short") == "?? unrelated.txt"


def test_checkpoint_help_has_no_removed_schema_or_route_selector() -> None:
    result = runner.invoke(aros_app, ["checkpoint", "--help"])

    assert result.exit_code == 0, result.output
    help_text = result.output.lower()
    assert "cooperative" in help_text
    assert "--message" in help_text
    assert "--path" in help_text
    for removed in ("proposal", "admission", "human-direct", "transition"):
        assert removed not in help_text
