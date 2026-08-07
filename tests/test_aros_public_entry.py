from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import arbor.cli.commands.aros_cmd as aros_cmd
from arbor.cli.commands.aros_cmd import aros_app

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def _metadata() -> dict[str, object]:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_exposes_direct_aros_script() -> None:
    assert _metadata()["project"]["scripts"]["aros"] == "arbor.cli.aros_app:main"  # type: ignore[index]


def test_all_src_python_packages_are_configured() -> None:
    source_packages = {
        ".".join(("arbor", *init.parent.relative_to(_ROOT / "src").parts))
        for init in (_ROOT / "src").rglob("__init__.py")
    }
    setuptools_config = _metadata()["tool"]["setuptools"]  # type: ignore[index]
    package_dirs = setuptools_config["package-dir"]
    non_src_roots = {
        package
        for package, directory in package_dirs.items()
        if Path(directory).as_posix().rstrip("/") != "src"
    }
    configured_src_packages = {
        package
        for package in setuptools_config["packages"]
        if not any(
            package == root or package.startswith(f"{root}.")
            for root in non_src_roots
        )
    }

    assert configured_src_packages == source_packages


def _configured_wheel_python_files(
    project_root: Path,
    setuptools_config: dict[str, object],
) -> set[str]:
    package_dirs = setuptools_config["package-dir"]
    assert isinstance(package_dirs, dict)
    outputs: set[str] = set()
    for package in setuptools_config["packages"]:  # type: ignore[index]
        roots = [
            root
            for root in package_dirs
            if package == root or package.startswith(f"{root}.")
        ]
        root = max(roots, key=lambda candidate: len(candidate.split(".")))
        suffix = package.removeprefix(root).removeprefix(".").split(".")
        source_dir = project_root / package_dirs[root]
        if suffix != [""]:
            source_dir = source_dir.joinpath(*suffix)
        outputs.update(
            f"{package.replace('.', '/')}/{source.name}"
            for source in source_dir.glob("*.py")
        )
    return outputs


def test_clean_configured_wheel_contents_include_oauth_modules(tmp_path: Path) -> None:
    setuptools_config = _metadata()["tool"]["setuptools"]  # type: ignore[index]
    clean_source = tmp_path / "clean-source"
    shutil.copytree(
        _ROOT / "src",
        clean_source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    expected = {
        f"arbor/core/oauth/{source.name}"
        for source in (clean_source / "src" / "core" / "oauth").glob("*.py")
    }
    actual = _configured_wheel_python_files(clean_source, setuptools_config)
    assert expected <= actual


def test_direct_aros_help_is_the_root_app() -> None:
    result = runner.invoke(aros_app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "boot",
        "status",
        "start",
        "run",
        "task",
        "eval",
        "checkpoint",
    ):
        assert command in result.output
    assert "transition" not in result.output
    assert "rebuild-index" not in result.output
    registered = {command.name for command in aros_app.registered_commands}
    assert "init" not in registered
    assert "\naros " not in result.output


def test_direct_run_help_uses_aros_wording() -> None:
    result = runner.invoke(aros_app, ["run", "start", "--help"])

    assert result.exit_code == 0, result.output
    assert "AROS" in result.output
    assert "Arbor" not in result.output


def test_uninitialized_workspace_error_uses_direct_aros_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbor.aros import workspace

    monkeypatch.setattr(
        workspace,
        "status_workspace",
        lambda root: {"initialized": False},
    )

    with pytest.raises(ValueError) as caught:
        workspace.boot_workspace(tmp_path)

    message = str(caught.value)
    assert "`aros start`" in message
    assert "arbor aros init" not in message


def test_direct_entry_reuses_the_single_app() -> None:
    from arbor.cli import aros_app as entry

    assert entry.app is aros_app


def _capture_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(aros_cmd, "create_provider", lambda config: object())
    monkeypatch.setattr(
        aros_cmd,
        "status_workspace",
        lambda root: {"initialized": True},
    )
    monkeypatch.setattr(
        aros_cmd,
        "boot_workspace",
        lambda root, *, context: "exact boot",
    )

    def build(provider: object, root: Path, boot: str, **kwargs: object) -> object:
        captured.update(root=root, boot=boot, kwargs=kwargs)
        return object()

    async def run(agent: object, request: str) -> str:
        captured.update(agent=agent, request=request)
        return "done"

    monkeypatch.setattr(aros_cmd, "build_principal_agent", build)
    monkeypatch.setattr(aros_cmd, "run_principal", run)
    return captured


def test_start_has_no_checkpoint_authority_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_start(monkeypatch)

    result = runner.invoke(
        aros_app,
        ["start", "inspect", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["allow_checkpoint"] is False
    assert kwargs["attention_context"] is None


def test_start_explicit_checkpoint_mode_injects_host_owned_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_start(monkeypatch)

    result = runner.invoke(
        aros_app,
        [
            "start",
            "inspect",
            "--allow-checkpoint",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["allow_checkpoint"] is True
    context = kwargs["attention_context"]
    assert isinstance(context, aros_cmd.AttentionAuthorityContext)
    assert dict(context.authority) == {
        "state": "available",
        "enforcement_class": "cooperative",
        "issuer": "local-host",
    }
    assert dict(context.remaining_budget) == {
        "state": "not_configured",
        "enforcement_class": "cooperative",
    }


def test_legacy_root_does_not_mount_aros() -> None:
    from arbor.cli import app as legacy

    registered = {
        group.name: group.typer_instance
        for group in legacy.app.registered_groups
    }
    assert "aros" not in registered


def test_legacy_main_contains_no_aros_forwarding_code() -> None:
    from arbor.cli import app as legacy

    source = Path(legacy.__file__).read_text(encoding="utf-8")
    assert "aros_app" not in source
    assert "_warn_aros_forward" not in source
    assert "aros" not in legacy._KNOWN_COMMANDS
