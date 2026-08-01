"""Native Principal commands exposed under ``arbor aros``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from ...aros.principal import build_principal_agent, run_principal
from ...aros.workspace import (
    DEFAULT_BOOT_MAX_CHARS,
    boot_workspace,
    init_workspace,
    status_workspace,
)
from ...core import AgentConfig, create_provider
from ..user_config import llm_defaults


aros_app = typer.Typer(
    name="aros",
    help="Operate the native Agent-principal research workspace.",
    no_args_is_help=True,
)


def _root(cwd: Path) -> Path:
    return cwd.expanduser().resolve()


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


@aros_app.command("init")
def init_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="Git workspace root."),
    mission: str = typer.Option(
        ...,
        "--mission",
        help="Initial mission written into the new workspace skeleton.",
    ),
) -> None:
    """Create a bootable AROS workspace without inventing research state."""
    try:
        result = init_workspace(_root(cwd), mission)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(result)


@aros_app.command("boot")
def boot_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    max_chars: int = typer.Option(
        DEFAULT_BOOT_MAX_CHARS,
        "--max-chars",
        min=1,
        help="Maximum characters in the derived boot context.",
    ),
) -> None:
    """Render the compact, provider-independent Principal boot context."""
    try:
        context = boot_workspace(_root(cwd), max_chars=max_chars)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    typer.echo(context)


@aros_app.command("status")
def status_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Inspect workspace initialization and operational Git state."""
    try:
        status = status_workspace(_root(cwd))
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)

    if as_json:
        _print_json(status)
        return

    typer.echo(f"workspace: {status.get('root', _root(cwd))}")
    typer.echo(f"initialized: {'yes' if status.get('initialized') else 'no'}")
    git = status.get("git")
    if isinstance(git, dict):
        branch = git.get("branch") or "(detached/unavailable)"
        head = git.get("head") or "unknown"
        dirty = "dirty" if git.get("dirty") else "clean"
        typer.echo(f"git: {branch} @ {head} ({dirty})")
    missing = status.get("missing")
    if missing:
        typer.echo("missing: " + ", ".join(str(item) for item in missing))


@aros_app.command("start")
def start_command(
    instruction: str | None = typer.Argument(
        None,
        help="Current request. Omit to continue from the durable workspace state.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    max_turns: int = typer.Option(100, "--max-turns", min=1),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
    allow_shell: bool = typer.Option(
        False,
        "--allow-shell",
        help="Enable bounded foreground shell commands for this trusted-local session.",
    ),
) -> None:
    """Start a fresh native Principal from workspace state, never transcript replay."""
    root = _root(cwd)
    config_values = dict(llm_defaults())
    if provider is not None:
        config_values["provider"] = provider
    if model is not None:
        config_values["model"] = model

    try:
        config = AgentConfig(
            **config_values,
            cwd=str(root),
            max_turns=max_turns,
            auto_git=False,
        )
        provider_obj = create_provider(config)
        boot_context = boot_workspace(root)
        request = instruction or (
            "Continue the research mission from the current workspace state."
        )
        agent = build_principal_agent(
            provider_obj,
            root,
            boot_context,
            max_turns=max_turns,
            allow_shell=allow_shell,
        )
        asyncio.run(run_principal(agent, request))
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)

__all__ = ["aros_app"]
