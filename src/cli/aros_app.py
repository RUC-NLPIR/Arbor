"""Direct public entry point for the AROS CLI."""

from __future__ import annotations

from .commands.aros_cmd import aros_app as app


def main() -> None:
    """Run the single native AROS Typer application."""
    app()


if __name__ == "__main__":
    main()
