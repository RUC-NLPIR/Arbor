"""Native Principal commands exposed under ``arbor aros``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from typer.core import TyperCommand

from ...aros.principal import build_principal_agent, run_principal
from ...aros.runs import RunService
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
run_app = typer.Typer(
    name="run",
    help="Manage durable background experiments.",
    no_args_is_help=True,
)
aros_app.add_typer(run_app, name="run")


def _root(cwd: Path) -> Path:
    return cwd.expanduser().resolve()


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


class _RequireCommandSeparator(TyperCommand):
    """Keep experiment argv separate from Arbor's own CLI options."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if "--help" not in args and "-h" not in args and "--" not in args:
            ctx.fail("experiment command argv must follow --")
        return super().parse_args(ctx, args)


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


@run_app.command("start", cls=_RequireCommandSeparator)
def run_start_command(
    command: list[str] = typer.Argument(
        ...,
        metavar="-- COMMAND [ARGS]...",
        help="Exact command argv. Place it after -- so options are not parsed by Arbor.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    run_cwd: str = typer.Option(
        ".",
        "--run-cwd",
        help="Command working directory, relative to the workspace root.",
    ),
    timeout_seconds: int = typer.Option(
        3600,
        "--timeout-seconds",
        min=1,
        help="Hard run timeout in seconds.",
    ),
    idempotency_key: str = typer.Option(
        ...,
        "--idempotency-key",
        help="Stable key preventing duplicate process launch.",
    ),
    security_profile: str = typer.Option(
        "isolated-linux",
        "--security-profile",
        help="Run isolation profile; trusted-local must be selected explicitly.",
    ),
    writable_paths: list[str] | None = typer.Option(
        None,
        "--writable-path",
        help="Workspace-relative writable path; repeat for multiple paths.",
    ),
    actor: str = typer.Option("human", "--actor", help="Launch authority."),
    label: str | None = typer.Option(None, "--label", help="Optional display label."),
) -> None:
    """Prepare and launch one durable run."""
    try:
        service = RunService(_root(cwd))
        manifest = service.prepare(
            list(command),
            cwd=run_cwd,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            actor=actor,
            label=label,
            security_profile=security_profile,
            writable_paths=writable_paths or [],
        )
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("prepared run manifest has no run_id")
        status = service.start(run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(status)


@run_app.command("status")
def run_status_command(
    run_id: str = typer.Argument(..., help="Stable run identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Reconcile and inspect one durable run."""
    try:
        status = RunService(_root(cwd)).status(run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(status)


@run_app.command("list")
def run_list_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """List durable runs after reconciling their process state."""
    try:
        runs = RunService(_root(cwd)).list()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(runs)


@run_app.command("tail")
def run_tail_command(
    run_id: str = typer.Argument(..., help="Stable run identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    stream: str = typer.Option(
        "stdout",
        "--stream",
        help="Log stream: stdout or stderr.",
    ),
    max_bytes: int = typer.Option(
        65_536,
        "--max-bytes",
        min=1,
        help="Maximum number of trailing bytes to print.",
    ),
) -> None:
    """Print the current tail of a run log without JSON escaping it."""
    try:
        output = RunService(_root(cwd)).tail(
            run_id,
            stream=stream,
            max_bytes=max_bytes,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    typer.echo(output, nl=False)


@run_app.command("stop")
def run_stop_command(
    run_id: str = typer.Argument(..., help="Stable run identifier."),
    reason: str = typer.Option(..., "--reason", help="Required audit reason."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    actor: str = typer.Option("human", "--actor", help="Stop authority."),
    signal_name: str = typer.Option(
        "TERM",
        "--signal",
        help="Signal name recorded and sent by the run service.",
    ),
) -> None:
    """Stop a durable run and record an attributed receipt."""
    try:
        receipt = RunService(_root(cwd)).stop(
            run_id,
            actor=actor,
            reason=reason,
            signal_name=signal_name,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(receipt)


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
