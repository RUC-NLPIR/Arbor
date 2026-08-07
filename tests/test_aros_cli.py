"""CLI integration tests for the native AROS Principal path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arbor.aros.intake import initialize_knowledge_bank
from arbor.aros.workspace import init_workspace
from arbor.cli.aros_start import StartIntake
from arbor.cli.commands import aros_cmd


runner = CliRunner()


def test_public_aros_init_command_is_absent() -> None:
    registered = {
        command.name for command in aros_cmd.aros_app.registered_commands
    }

    assert "init" not in registered


def _capture_start_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    captured: dict[str, object] = {}
    fake_agent = object()
    monkeypatch.setattr(
        aros_cmd,
        "status_workspace",
        lambda root: {"initialized": True},
    )
    monkeypatch.setattr(
        aros_cmd,
        "boot_workspace",
        lambda root, *, context: "boot",
    )

    def create(config: object) -> object:
        captured["config"] = config
        return object()

    def build(*args: object, **kwargs: object) -> object:
        return fake_agent

    async def run(agent: object, instruction: str) -> str:
        return "done"

    monkeypatch.setattr(aros_cmd, "create_provider", create)
    monkeypatch.setattr(aros_cmd, "build_principal_agent", build)
    monkeypatch.setattr(aros_cmd, "run_principal", run)
    return captured


def test_start_uses_aros_owned_default_model_triple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_start_config(tmp_path, monkeypatch)

    result = runner.invoke(
        aros_cmd.aros_app,
        ["start", "inspect", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    config = captured["config"]
    assert config.provider == "openai-responses"
    assert config.model == "gpt-5.6-luna"
    assert config.reasoning_effort == "max"


def test_start_accepts_exact_model_triple_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_start_config(tmp_path, monkeypatch)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "start",
            "inspect",
            "--cwd",
            str(tmp_path),
            "--provider",
            "openai-oauth",
            "--model",
            "gpt-custom",
            "--reasoning-effort",
            "high",
        ],
    )

    assert result.exit_code == 0, result.output
    config = captured["config"]
    assert config.provider == "openai-oauth"
    assert config.model == "gpt-custom"
    assert config.reasoning_effort == "high"


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
    init_workspace(tmp_path, "Bound the exact boot packet.")
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


def test_start_initializes_before_context_and_runs_native_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = tmp_path / "requested"
    paper = tmp_path / "paper.md"
    events: list[object] = []
    fake_provider = object()
    fake_agent = object()

    monkeypatch.setattr(
        aros_cmd,
        "status_workspace",
        lambda root: {"initialized": False},
    )

    def collect(**kwargs: object) -> StartIntake:
        events.append(("collect", kwargs))
        return StartIntake(
            workspace=requested,
            question="What mechanism matters?",
            materials=(paper,),
        )

    def initialize(
        workspace: Path,
        question: str,
        materials: tuple[Path, ...],
    ) -> dict[str, object]:
        events.append(("initialize", workspace, question, materials))
        return {"commit": "a" * 40}

    def boot(root: Path, *, context: object) -> str:
        events.append(("boot", root, context))
        return "exact canonical attention"

    def build(provider: object, root: Path, context: str, **kwargs: object) -> object:
        events.append(("build", provider, root, context, kwargs))
        return fake_agent

    async def run(agent: object, request: str) -> str:
        events.append(("run", agent, request))
        return "done"

    monkeypatch.setattr(aros_cmd, "collect_start_intake", collect, raising=False)
    monkeypatch.setattr(
        aros_cmd,
        "initialize_knowledge_bank",
        initialize,
        raising=False,
    )
    monkeypatch.setattr(aros_cmd, "render_start_transition", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(aros_cmd, "boot_workspace", boot)
    monkeypatch.setattr(aros_cmd, "create_provider", lambda config: fake_provider)
    monkeypatch.setattr(aros_cmd, "build_principal_agent", build)
    monkeypatch.setattr(aros_cmd, "run_principal", run)

    result = runner.invoke(
        aros_cmd.aros_app,
        [
            "start",
            "research now",
            "--cwd",
            str(requested),
            "--question",
            "What mechanism matters?",
            "--material",
            str(paper),
            "--allow-checkpoint",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [event[0] for event in events] == [
        "collect",
        "initialize",
        "boot",
        "build",
        "run",
    ]
    assert events[1] == (
        "initialize",
        requested,
        "What mechanism matters?",
        (paper,),
    )
    assert events[2][1] == requested.resolve()
    assert events[2][2].authority["enforcement_class"] == "cooperative"


def test_start_rejects_intake_arguments_for_initialized_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aros_cmd,
        "status_workspace",
        lambda root: {"initialized": True},
    )

    result = runner.invoke(
        aros_cmd.aros_app,
        ["start", "--cwd", str(tmp_path), "--question", "Do not replace?"],
    )

    assert result.exit_code == 2
    assert "already initialized" in result.output


def test_start_does_not_import_or_construct_coordinator() -> None:
    source = Path(aros_cmd.__file__).read_text(encoding="utf-8")
    assert "coordinator" not in source.lower()


def test_direct_aros_status_and_boot_use_native_intake_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    receipt = initialize_knowledge_bank(workspace, "Real CLI smoke?")

    status = runner.invoke(
        aros_cmd.aros_app,
        ["status", "--cwd", str(workspace), "--json"],
    )
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["initialized"] is True

    boot = runner.invoke(
        aros_cmd.aros_app,
        ["boot", "--cwd", str(workspace)],
    )
    assert boot.exit_code == 0, boot.output
    packet = json.loads(boot.output)
    assert packet["schema_version"] == 1
    assert packet["snapshot"]["canonical"] == receipt["commit"]
