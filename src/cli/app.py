"""Top-level Typer app for the arbor CLI."""

from __future__ import annotations

import sys
from difflib import get_close_matches

import typer

from .._app import APP_NAME, TAGLINE, TAGLINE_SUB
from .commands.run import run_command
from .commands.report_cmd import report_command
from .commands.export_cmd import export_command
from .commands.replay_cmd import replay_command
from .commands.config_cmd import config_app
from .commands.login_cmd import login_app
from .commands.doctor_cmd import doctor_command
from .commands.setup_cmd import setup_command, quickstart_command
from .commands.install_cmd import install_command, uninstall_command
from .commands.mcp_cmd import mcp_command
from .commands.web_cmd import web_command
from .commands.idea_check_cmd import idea_check_command
from .commands.benchmark_cmd import benchmark_app


# We don't use a Typer.callback() default because that would shadow flag
# handling for `arbor --help`. Instead, we detect the no-subcommand case
# in main() and rewrite argv to insert "run" before delegating.

app = typer.Typer(
    name=APP_NAME,
    help=(
        f"{APP_NAME} — {TAGLINE}\n\n"
        f"{TAGLINE_SUB}\n\n"
        f"Tip: run `{APP_NAME}` (no subcommand) inside your project to start "
        f"an interactive session — equivalent to `{APP_NAME} run`."
    ),
    no_args_is_help=False,
    add_completion=False,
)

app.command("run")(run_command)
app.command("report")(report_command)
app.command("export")(export_command)
app.command("replay")(replay_command)
app.command("doctor")(doctor_command)
app.command("setup")(setup_command)
app.command("quickstart")(quickstart_command)
app.command("install")(install_command)
app.command("uninstall")(uninstall_command)
app.command("mcp")(mcp_command)
app.command("web")(web_command)
app.command("idea-check")(idea_check_command)
app.add_typer(config_app, name="config")
app.add_typer(login_app, name="login")
app.add_typer(benchmark_app, name="benchmark")


@app.command("version")
def version_command() -> None:
    """Print the installed version."""
    from importlib.metadata import version as _v

    # The installed distribution is "arbor-agent"; APP_NAME ("arbor") is the
    # command/brand, not the package name, so look up the dist explicitly (with
    # a fallback in case it's ever renamed back).
    ver = "unknown"
    for dist in ("arbor-agent", APP_NAME):
        try:
            ver = _v(dist)
            break
        except Exception:
            continue
    typer.echo(f"{APP_NAME} {ver}")


_KNOWN_COMMANDS = {
    "run", "report", "export", "replay", "config", "version", "doctor", "setup",
    "quickstart", "login", "install", "uninstall", "mcp", "web", "idea-check",
    "benchmark",
}
_ROOT_FLAGS = {"--help", "-h"}
_VERSION_FLAGS = {"--version", "-V"}


def main() -> None:
    """Console-script entry point.

    If invoked with no subcommand (e.g. `arbor` or `arbor --cwd .`),
    default to `run`. The only flags that stay at root level are --help / -h.
    """
    # Some terminals (notably macOS Terminal.app with "Set locale env vars on
    # startup" off) hand Python a non-UTF-8 stdout, and any glyph or CJK text
    # we print then raises UnicodeEncodeError and crashes. Force UTF-8 with
    # replacement so the worst case is a "?" rather than a dead process.
    for _stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(_stream, "encoding", None) or "").lower()
            if hasattr(_stream, "reconfigure") and enc not in ("utf-8", "utf8"):
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    argv = sys.argv[1:]
    first = argv[0] if argv else None
    if first in _VERSION_FLAGS:
        sys.argv = [sys.argv[0], "version", *argv[1:]]
        app()
        return
    needs_default = (
        not argv
        or (first not in _KNOWN_COMMANDS and first not in _ROOT_FLAGS)
    )
    if first and first not in _KNOWN_COMMANDS and first not in _ROOT_FLAGS and not first.startswith("-"):
        match = get_close_matches(first, sorted(_KNOWN_COMMANDS), n=1, cutoff=0.74)
        if match:
            typer.secho(
                f"error: unknown command {first!r}. Did you mean {match[0]!r}?",
                fg=typer.colors.RED,
                err=True,
            )
            sys.exit(2)
    if needs_default:
        sys.argv = [sys.argv[0], "run", *argv]
    app()


if __name__ == "__main__":
    main()
