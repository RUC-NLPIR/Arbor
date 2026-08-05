"""CLI integration tests for the native AROS Principal path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from arbor.cli.app import app
from arbor.cli.commands import aros_cmd


runner = CliRunner()


def test_init_calls_workspace_api_and_prints_result(
    tmp_path: Path, monkeypatch,
) -> None:
    calls: list[tuple[Path, str | None]] = []

    def fake_init(root: Path, mission: str) -> dict[str, Any]:
        calls.append((root, mission))
        return {"workspace": str(root), "created": ["AROS.md"]}

    monkeypatch.setattr(aros_cmd, "init_workspace", fake_init)

    result = runner.invoke(
        aros_cmd.aros_app,
        ["init", "--cwd", str(tmp_path), "--mission", "Understand the system"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path.resolve(), "Understand the system")]
    assert json.loads(result.output)["created"] == ["AROS.md"]


def test_init_requires_mission_before_calling_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    called = False

    def fake_init(root: Path, mission: str) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(aros_cmd, "init_workspace", fake_init)

    result = runner.invoke(
        aros_cmd.aros_app,
        ["init", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert called is False


@pytest.mark.parametrize("as_json", [False, True])
def test_boot_text_and_json_render_the_same_single_built_packet(
    tmp_path: Path, monkeypatch,
    as_json: bool,
) -> None:
    calls: list[tuple[Path, int]] = []
    rendered: list[dict[str, object]] = []
    packet = {"schema_version": 1, "snapshot": {"head": "abc"}}
    packet_wire = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def fake_boot_packet(root: Path, max_chars: int = 80_000) -> dict[str, object]:
        calls.append((root, max_chars))
        return packet

    def fake_render(value: dict[str, object]) -> str:
        rendered.append(value)
        return packet_wire

    monkeypatch.setattr(aros_cmd, "boot_packet", fake_boot_packet)
    monkeypatch.setattr(aros_cmd, "render_boot_packet", fake_render)

    argv = ["boot", "--cwd", str(tmp_path), "--max-chars", "1234"]
    if as_json:
        argv.append("--json")

    result = runner.invoke(aros_cmd.aros_app, argv)

    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path.resolve(), 1234)]
    assert result.output == packet_wire + "\n"
    assert json.loads(result.output) == packet
    assert rendered == [packet]


def test_boot_json_wire_is_the_once_built_bounded_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(tmp_path)],
        check=True,
    )
    aros_cmd.init_workspace(tmp_path, "Bound the exact boot packet.")
    built: list[dict[str, object]] = []
    real_boot_packet = aros_cmd.boot_packet

    def recording_boot_packet(root: Path, max_chars: int) -> dict[str, object]:
        packet = real_boot_packet(root, max_chars=max_chars)
        built.append(packet)
        return packet

    monkeypatch.setattr(aros_cmd, "boot_packet", recording_boot_packet)

    result = runner.invoke(
        aros_cmd.aros_app,
        ["boot", "--json", "--max-chars", "512", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert len(built) == 1
    assert json.loads(result.output) == built[0]
    wire = result.output.rstrip("\n")
    assert len(wire) <= 512
    assert wire == aros_cmd.render_boot_packet(built[0])


def test_status_json_forwards_workspace_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    snapshot = {
        "workspace": str(tmp_path.resolve()),
        "initialized": True,
        "git": {"branch": "main", "dirty": False},
    }
    monkeypatch.setattr(aros_cmd, "status_workspace", lambda root: snapshot)

    result = runner.invoke(
        aros_cmd.aros_app,
        ["status", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == snapshot


def test_start_uses_user_llm_defaults_with_cli_overrides(
    tmp_path: Path, monkeypatch,
) -> None:
    seen: dict[str, Any] = {}
    fake_agent = object()
    fake_provider = object()

    monkeypatch.setattr(
        aros_cmd,
        "llm_defaults",
        lambda: {
            "provider": "anthropic",
            "model": "user-model",
            "base_url": "https://user.invalid/v1",
        },
    )
    monkeypatch.setattr(
        aros_cmd,
        "boot_workspace",
        lambda root: "# Boot\nmission from workspace",
    )

    def fake_create_provider(config):
        seen["config"] = config
        return fake_provider

    def fake_build(provider, root, boot_context, *, max_turns, allow_shell):
        seen["build"] = {
            "provider": provider,
            "root": root,
            "boot_context": boot_context,
            "max_turns": max_turns,
            "allow_shell": allow_shell,
        }
        return fake_agent

    async def fake_run(agent, instruction):
        seen["run"] = (agent, instruction)
        return "principal finished"

    monkeypatch.setattr(aros_cmd, "create_provider", fake_create_provider)
    monkeypatch.setattr(aros_cmd, "build_principal_agent", fake_build)
    monkeypatch.setattr(aros_cmd, "run_principal", fake_run)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "start",
            "inspect the current evidence",
            "--cwd",
            str(tmp_path),
            "--provider",
            "openai-responses",
            "--model",
            "cli-model",
            "--max-turns",
            "7",
            "--allow-shell",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert seen["config"].provider == "openai-responses"
    assert seen["config"].model == "cli-model"
    assert seen["config"].base_url == "https://user.invalid/v1"
    assert seen["config"].cwd == str(tmp_path.resolve())
    assert seen["config"].auto_git is False
    assert seen["build"] == {
        "provider": fake_provider,
        "root": tmp_path.resolve(),
        "boot_context": "# Boot\nmission from workspace",
        "max_turns": 7,
        "allow_shell": True,
    }
    assert seen["run"] == (fake_agent, "inspect the current evidence")


def test_start_does_not_import_or_construct_coordinator() -> None:
    source = Path(aros_cmd.__file__).read_text(encoding="utf-8")
    assert "coordinator" not in source.lower()


def test_top_level_aros_init_status_and_boot_use_real_workspace(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    init = runner.invoke(
        app,
        ["aros", "init", "--cwd", str(tmp_path), "--mission", "Real CLI smoke"],
    )
    assert init.exit_code == 0, init.output
    assert (tmp_path / "AROS.md").exists()

    status = runner.invoke(
        app,
        ["aros", "status", "--cwd", str(tmp_path), "--json"],
    )
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["initialized"] is True

    boot = runner.invoke(app, ["aros", "boot", "--cwd", str(tmp_path)])
    assert boot.exit_code == 0, boot.output
    packet = json.loads(boot.output)
    assert packet["schema_version"] == 1
    assert packet["snapshot"]["canonical"] is None
    assert "canonical_head_unavailable" in packet["warnings"]
