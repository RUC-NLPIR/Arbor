"""Presentation-only native AROS start intake."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .style import console as shared_console


Prompt = Callable[[str, str], str]


@dataclass(frozen=True)
class StartIntake:
    """Exact user input passed from the CLI to native intake mechanics."""

    workspace: Path
    question: str
    materials: tuple[Path, ...]


def collect_start_intake(
    *,
    workspace: Path | None,
    question: str | None,
    materials: Sequence[Path] | None,
    interactive: bool,
    prompt: Prompt | None = None,
) -> StartIntake:
    """Collect only missing values; non-interactive mode never invents them."""
    selected_prompt = prompt or _typer_prompt
    selected_workspace = workspace
    if selected_workspace is None:
        if interactive:
            raw_workspace = selected_prompt("Workspace", str(Path.cwd())).strip()
            if not raw_workspace:
                raise ValueError("workspace must be non-empty")
            selected_workspace = Path(raw_workspace).expanduser()
        else:
            selected_workspace = Path.cwd()

    selected_question = question
    if selected_question is None:
        if not interactive:
            raise ValueError("--question is required for a new AROS workspace")
        selected_question = selected_prompt("Key Research Question", "").strip()
    if not isinstance(selected_question, str) or not selected_question.strip():
        raise ValueError("question must be non-empty")

    selected_materials: tuple[Path, ...]
    if materials is None:
        if interactive:
            raw_materials = selected_prompt(
                "Local materials (comma-separated, blank for none)",
                "",
            ).strip()
            selected_materials = tuple(
                Path(item.strip()).expanduser()
                for item in raw_materials.split(",")
                if item.strip()
            )
        else:
            selected_materials = ()
    else:
        selected_materials = tuple(Path(item).expanduser() for item in materials)

    return StartIntake(
        workspace=selected_workspace,
        question=selected_question.strip(),
        materials=selected_materials,
    )


def render_start_transition(
    intake: StartIntake,
    *,
    authority_class: str,
    max_turns: int,
    allow_shell: bool,
    console: Console | None = None,
) -> None:
    """Render one bounded product transition without reading or mutating state."""
    target = console or shared_console
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column(style="white", overflow="fold")
    table.add_row("Question", intake.question)
    table.add_row("Workspace", str(intake.workspace))
    table.add_row("Materials", str(len(intake.materials)))
    table.add_row("Authority", authority_class)
    table.add_row("Max turns", str(max_turns))
    table.add_row("Shell", "enabled" if allow_shell else "disabled")
    target.print(
        Panel(
            table,
            title="[bold cyan]AROS[/] · Agent-principal Research OS",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def _typer_prompt(label: str, default: str) -> str:
    return str(
        typer.prompt(
            label,
            default=default,
            show_default=bool(default),
        )
    )


__all__ = [
    "StartIntake",
    "collect_start_intake",
    "render_start_transition",
]
