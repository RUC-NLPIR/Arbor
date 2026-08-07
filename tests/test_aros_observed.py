"""Session-local observation exposure tracking."""

from __future__ import annotations

import pytest

from arbor.aros.observed import ObservedRefError, ObservedRefs


def test_observed_refs_are_sorted_idempotent_and_clear_only_selected() -> None:
    observed = ObservedRefs()
    task = "tasks/TASK-x/collected.json"
    evaluation = "eval/evaluations/EVAL-x/receipt.json"
    run = "runs/RUN-x/final.json"

    observed.record(task)
    observed.record(evaluation)
    observed.record(run)
    observed.record(task)

    assert observed.snapshot() == (evaluation, run, task)
    observed.clear((evaluation, task))
    assert observed.snapshot() == (run,)


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "/tasks/TASK-x/collected.json",
        "../tasks/TASK-x/collected.json",
        "tasks/TASK-x/../collected.json",
        "tasks\\TASK-x\\collected.json",
        "tasks/TASK-x/collected.json\x00",
        ".aros/tasks/TASK-x/final.json",
        "questions/Q-0001/question.md",
        "tasks/TASK-x/brief.json",
        "runs/RUN-x/manifest.json",
        "eval/suites/evaluator/1/manifest.json",
        "eval/evaluations/EVAL-x/status.json",
    ],
)
def test_observed_refs_reject_nonterminal_or_unsafe_paths(ref: str) -> None:
    observed = ObservedRefs()

    with pytest.raises(ObservedRefError):
        observed.record(ref)

    assert observed.snapshot() == ()


def test_observed_refs_snapshot_is_an_immutable_value() -> None:
    observed = ObservedRefs()
    observed.record("tasks/TASK-x/collected.json")

    snapshot = observed.snapshot()
    observed.clear(snapshot)

    assert snapshot == ("tasks/TASK-x/collected.json",)
    assert observed.snapshot() == ()
