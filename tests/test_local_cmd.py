from __future__ import annotations

from pathlib import Path

import pytest
import typer

from arbor.cli.commands import local_cmd


def _make_skill(root: Path, name: str) -> None:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")


def test_skill_dirs_requires_public_entrypoint(tmp_path: Path) -> None:
    _make_skill(tmp_path, "arbor-agent-tools")
    with pytest.raises(typer.BadParameter, match="missing public entrypoint"):
        local_cmd._skill_dirs(tmp_path)


def test_skill_dirs_discovers_arbor_skills(tmp_path: Path) -> None:
    _make_skill(tmp_path, "arbor-research-agent")
    _make_skill(tmp_path, "arbor-agent-tools")
    (tmp_path / "not-arbor").mkdir()

    out = local_cmd._skill_dirs(tmp_path)

    assert [path.name for path in out] == ["arbor-agent-tools", "arbor-research-agent"]


def test_windows_mount_detection() -> None:
    assert local_cmd._is_windows_mount(Path("/mnt/c/Users/example/Arbor"))
    assert not local_cmd._is_windows_mount(Path("/home/user01/agent-workspace/Arbor"))


def test_build_claude_command_uses_local_skill_entrypoint(tmp_path: Path) -> None:
    skills = tmp_path / "Arbor" / "skills"
    _make_skill(skills, "arbor-research-agent")

    command = local_cmd._build_command(
        agent="claude",
        cwd=tmp_path,
        skills_src=skills,
        task="try a smoke run",
        claude_permission_mode="plan",
    )

    assert command[:5] == ["claude", "--print", "--output-format", "text", "--add-dir"]
    assert str(tmp_path / "Arbor") in command
    assert "--permission-mode" in command
    assert "try a smoke run" in command[-1]
    assert str(skills / "arbor-research-agent" / "SKILL.md") in command[-1]
    assert "do not clone Arbor from GitHub" in command[-1]


def test_build_codex_command_uses_wsl_target_and_add_dir(tmp_path: Path) -> None:
    skills = tmp_path / "Arbor" / "skills"
    _make_skill(skills, "arbor-research-agent")

    command = local_cmd._build_command(
        agent="codex",
        cwd=tmp_path,
        skills_src=skills,
        task="optimize dev score",
    )

    assert command[:4] == ["codex", "exec", "--add-dir", str(tmp_path / "Arbor")]
    assert command[4:6] == ["-C", str(tmp_path)]
    assert "optimize dev score" in command[-1]


def test_select_agent_prefers_claude_when_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(name: str) -> local_cmd.CliProbe:
        return local_cmd.CliProbe(
            name=name,
            path=f"/usr/bin/{name}",
            runnable=name == "claude",
            version="ok" if name == "claude" else None,
            error=None if name == "claude" else "missing",
        )

    monkeypatch.setattr(local_cmd, "_probe_cli", fake_probe)

    assert local_cmd._select_agent("auto") == "claude"


def test_select_agent_rejects_unknown_agent() -> None:
    with pytest.raises(typer.BadParameter, match="--agent must be one of"):
        local_cmd._select_agent("windows")
