"""Native AROS start presentation behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from arbor.cli.aros_start import (
    StartIntake,
    collect_start_intake,
    render_start_transition,
)


def test_noninteractive_start_requires_question_without_prompting(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []

    with pytest.raises(ValueError, match="--question is required"):
        collect_start_intake(
            workspace=tmp_path,
            question=None,
            materials=None,
            interactive=False,
            prompt=lambda label, default: prompts.append(label) or default,
        )

    assert prompts == []


def test_supplied_start_values_never_prompt(tmp_path: Path) -> None:
    paper = tmp_path / "paper.md"
    prompts: list[str] = []

    result = collect_start_intake(
        workspace=tmp_path / "topic_KB",
        question="What mechanism matters?",
        materials=[paper],
        interactive=True,
        prompt=lambda label, default: prompts.append(label) or default,
    )

    assert result == StartIntake(
        workspace=tmp_path / "topic_KB",
        question="What mechanism matters?",
        materials=(paper,),
    )
    assert prompts == []


def test_interactive_start_asks_workspace_question_then_materials(
    tmp_path: Path,
) -> None:
    answers = iter(
        [
            str(tmp_path / "topic_KB"),
            "Why does the intervention work?",
            f"{tmp_path / 'a.md'}, {tmp_path / 'b.pdf'}",
        ]
    )
    prompts: list[tuple[str, str]] = []

    def prompt(label: str, default: str) -> str:
        prompts.append((label, default))
        return next(answers)

    result = collect_start_intake(
        workspace=None,
        question=None,
        materials=None,
        interactive=True,
        prompt=prompt,
    )

    assert [label for label, _ in prompts] == [
        "Workspace",
        "Key Research Question",
        "Local materials (comma-separated, blank for none)",
    ]
    assert result == StartIntake(
        workspace=tmp_path / "topic_KB",
        question="Why does the intervention work?",
        materials=(tmp_path / "a.md", tmp_path / "b.pdf"),
    )


def test_start_intake_rejects_empty_question() -> None:
    with pytest.raises(ValueError, match="question"):
        collect_start_intake(
            workspace=Path("."),
            question="",
            materials=[],
            interactive=False,
        )


def test_start_intake_rejects_empty_prompted_workspace() -> None:
    with pytest.raises(ValueError, match="workspace"):
        collect_start_intake(
            workspace=None,
            question="What matters?",
            materials=[],
            interactive=True,
            prompt=lambda label, default: "",
        )


def test_render_start_transition_shows_product_and_authority_boundaries(
    tmp_path: Path,
) -> None:
    console = Console(record=True, width=100)
    intake = StartIntake(
        workspace=tmp_path,
        question="What mechanism matters?",
        materials=(tmp_path / "paper.md",),
    )

    render_start_transition(
        intake,
        authority_class="cooperative",
        max_turns=37,
        allow_shell=False,
        console=console,
    )

    rendered = console.export_text()
    assert "AROS" in rendered
    assert "What mechanism matters?" in rendered
    assert str(tmp_path) in rendered
    assert "cooperative" in rendered
    assert "37" in rendered
    assert "disabled" in rendered
