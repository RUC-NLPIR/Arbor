from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from arbor.cli.commands.aros_cmd import aros_app

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_pyproject_exposes_direct_aros_script() -> None:
    metadata = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"]["aros"] == "arbor.cli.aros_app:main"


def test_direct_aros_help_is_the_root_app() -> None:
    result = runner.invoke(aros_app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("init", "boot", "status", "start", "run"):
        assert command in result.output
    assert "\naros " not in result.output


def test_direct_entry_reuses_the_single_app() -> None:
    from arbor.cli import aros_app as entry

    assert entry.app is aros_app


def test_legacy_root_mounts_the_same_aros_app() -> None:
    from arbor.cli import app as legacy

    registered = {
        group.name: group.typer_instance
        for group in legacy.app.registered_groups
    }
    assert registered["aros"] is aros_app


def test_legacy_main_warns_when_forwarding_aros(
    monkeypatch,
    capsys,
) -> None:
    from arbor.cli import app as legacy

    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["arbor", "aros", "--help"])
    monkeypatch.setattr(legacy, "app", lambda: calls.append(list(sys.argv)))
    legacy.main()
    captured = capsys.readouterr()

    assert calls == [["arbor", "aros", "--help"]]
    assert "deprecated" in captured.err.lower()
    assert "use aros" in captured.err.lower()


def test_legacy_main_does_not_warn_for_non_aros_commands(
    monkeypatch,
    capsys,
) -> None:
    from arbor.cli import app as legacy

    monkeypatch.setattr(sys, "argv", ["arbor", "report", "--help"])
    monkeypatch.setattr(legacy, "app", lambda: None)
    legacy.main()

    assert capsys.readouterr().err == ""
