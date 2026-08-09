from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from commissioning.cache_campaign import seal as seal_module
from commissioning.cache_campaign.calibration_evidence import CalibrationError
from commissioning.cache_campaign.constraints import validate_calibration
from commissioning.cache_campaign.portfolio import _evaluate_temporal_portfolio
from commissioning.cache_campaign.records import record_sha256
from commissioning.cache_campaign.seal import (
    FrozenInputs,
    ProjectBinding,
    SealError,
    _consume_ledger,
    _git_refs,
    _new_path,
    _project_binding,
    _scan_task,
    _validate_reproduction,
    load_frozen_package,
    run_r3,
)
from scripts import run_aros_cache_r3 as r3_cli
from tests.test_aros_cache_evaluator import portfolio_inputs, write_record


def git(path: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def project_repository(path: Path, *, leak: str | None = None) -> ProjectBinding:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "R3 Test")
    git(path, "config", "user.email", "r3@example.invalid")
    refs = {
        "knowledge/claims/C-0001/claim.md": "claim\n",
        "experiments/confirmation/preregistration.md": "preregistered\n",
        "reviews/RV-0001/report.md": "review\n",
        "reviews/RV-0001/principal-response.md": "response\n",
        "reviews/RV-0001/reproduction.json": json.dumps(
            {"r2_receipt_sha256": "2" * 64}
        ),
    }
    for relative, raw in refs.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw)
    if leak is not None:
        (path / "TaskBrief.md").write_text(leak)
    git(path, "add", ".")
    git(path, "commit", "-qm", "freeze")
    head = git(path, "rev-parse", "HEAD")
    return _project_binding(path, head)


def package(project_path: Path, head: str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "project": str(project_path.resolve()),
        "frozen_commit": head,
        "candidate_commit": "c" * 40,
        "policy": "CandidatePolicy",
        "candidate_diff_sha256": "d" * 64,
        "policy_contract_sha256": "e" * 64,
        "claim_ref": "knowledge/claims/C-0001/claim.md",
        "preregistration_ref": "experiments/confirmation/preregistration.md",
        "review_ref": "reviews/RV-0001/report.md",
        "principal_response_ref": "reviews/RV-0001/principal-response.md",
        "reproduction_ref": "reviews/RV-0001/reproduction.json",
        "r0_receipt_sha256": "0" * 64,
        "r2_receipt_sha256": "2" * 64,
        "calibration_sha256": "a" * 64,
        "r3_commitment_sha256": "3" * 64,
    }
    value.update(changes)
    return value


def test_frozen_package_requires_exact_keys() -> None:
    try:
        load_frozen_package({"schema_version": 1})
    except SealError as error:
        assert "keys" in str(error)
    else:
        raise AssertionError("incomplete frozen package was accepted")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("project", "relative"),
        ("frozen_commit", "A" * 40),
        ("candidate_commit", "0" * 39),
        ("policy", "../CandidatePolicy"),
        ("candidate_diff_sha256", "0" * 63),
        ("claim_ref", "../claim.md"),
    ],
)
def test_frozen_package_requires_exact_types_and_safe_refs(
    tmp_path: Path, field: str, value: object
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    valid = package(project, "f" * 40, **{field: value})
    with pytest.raises(SealError):
        load_frozen_package(valid)


def test_project_requires_frozen_clean_head(tmp_path: Path) -> None:
    project = project_repository(tmp_path / "task")
    assert _project_binding(project.path, project.head) == project
    (project.path / "dirty.txt").write_text("dirty\n")
    with pytest.raises(SealError, match="clean"):
        _project_binding(project.path, project.head)
    (project.path / "dirty.txt").unlink()
    (project.path / "later.txt").write_text("later\n")
    git(project.path, "add", ".")
    git(project.path, "commit", "-qm", "post-freeze")
    with pytest.raises(SealError, match="HEAD"):
        _project_binding(project.path, project.head)


def test_reviewer_and_principal_refs_must_be_regular_frozen_blobs(
    tmp_path: Path,
) -> None:
    project = project_repository(tmp_path / "task")
    value = package(project.path, project.head)
    _git_refs(project, value)
    value["principal_response_ref"] = "reviews/RV-0001/missing.md"
    with pytest.raises(SealError, match="principal_response_ref"):
        _git_refs(project, value)


def test_reproduction_ref_rejects_duplicate_json_keys() -> None:
    digest = "2" * 64
    raw = (
        '{"r2_receipt_sha256":"' + digest + '","r2_receipt_sha256":"' + digest + '"}'
    ).encode()
    with pytest.raises(SealError, match="JSON"):
        _validate_reproduction(raw, digest)


def test_task_tree_and_frozen_git_blobs_reject_private_r3_bytes(
    tmp_path: Path,
) -> None:
    identity = "private-r3-tencent-photo"
    project = project_repository(tmp_path / "task", leak=identity)
    with pytest.raises(SealError, match="leaks"):
        _scan_task(project, b"{}", (identity.encode(),))
    with pytest.raises(SealError, match="package leaks"):
        _scan_task(project, identity.encode(), (identity.encode(),))


def test_new_ledger_and_output_paths_must_stay_outside_task_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "task"
    project.mkdir()
    with pytest.raises(SealError, match="outside"):
        _new_path(project / "ledger.json", project, "ledger")
    external = tmp_path / "host"
    external.mkdir()
    assert _new_path(external / "ledger.json", project, "ledger") == (
        external / "ledger.json"
    )
    (external / "ledger.json").write_text("foreign\n")
    with pytest.raises(SealError, match="already consumed"):
        _new_path(external / "ledger.json", project, "ledger")


def test_ledger_is_exclusive_canonical_and_fsynced_before_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ledger.json"
    calls: list[str] = []
    real_fsync = os.fsync

    def observe(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(seal_module.os, "fsync", observe)
    record = {"schema_version": 1, "state": "consumed", "requested_at_unix_ns": 1}
    consumed, file_hash = _consume_ledger(ledger, record)
    assert calls == ["file", "directory"]
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert consumed["ledger_sha256"] == record_sha256(consumed, "ledger_sha256")
    assert len(file_hash) == 64
    assert json.loads(ledger.read_text()) == consumed
    with pytest.raises(SealError, match="already consumed"):
        _consume_ledger(ledger, dict(record))


def fake_inputs(tmp_path: Path) -> FrozenInputs:
    project = project_repository(tmp_path / "task")
    host = tmp_path / "host"
    host.mkdir()
    binding = SimpleNamespace(sha256="b" * 64, path=host / "bound.json")
    preflight = SimpleNamespace(
        checkout=tmp_path / "checkout",
        checkout_binding=SimpleNamespace(tree="c" * 40),
        source={"receipt_sha256": "1" * 64},
        source_binding=binding,
        r0_binding=binding,
        artifact_bindings={"release_cachesim": binding},
        evaluator_bindings={},
    )
    calibration = SimpleNamespace(
        path=host / "calibration.json",
        calibration_sha256="a" * 64,
        file_sha256="f" * 64,
    )
    return FrozenInputs(
        package=package(project.path, project.head),
        package_binding=binding,
        project=project,
        ref_sha256s={name: "9" * 64 for name in seal_module._REF_FIELDS},
        host_manifest={"traces": [{}]},
        host_binding=binding,
        calibration=calibration,
        preflight=preflight,
        trace_bindings=(binding,),
        contract={},
        contract_binding=binding,
        r3_evaluator_bindings={"seal_sha256": binding},
        ledger=host / "ledger.json",
        output=host / "output",
    )


def run_arguments(tmp_path: Path) -> tuple[object, ...]:
    return (
        tmp_path / "package.json",
        tmp_path / "r3.json",
        tmp_path / "calibration.json",
        "a" * 64,
        tmp_path / "source.json",
        tmp_path / "r0.json",
        tmp_path / "checkout",
        tmp_path / "ledger.json",
        tmp_path / "output",
    )


def test_r3_consumes_ledger_before_failing_launch_and_cannot_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = fake_inputs(tmp_path)
    monkeypatch.setattr(seal_module, "_prevalidate", lambda *args: inputs)
    calls = 0

    def fail_after_consumption(**kwargs: object) -> dict[str, object]:
        nonlocal calls
        del kwargs
        calls += 1
        assert inputs.ledger.exists()
        raise RuntimeError("injected launch failure")

    monkeypatch.setattr(
        seal_module, "_evaluate_temporal_portfolio", fail_after_consumption
    )
    receipt = run_r3(*run_arguments(tmp_path))  # type: ignore[arg-type]
    assert receipt["state"] == "process_failed"
    assert inputs.ledger.exists()
    assert inputs.output.joinpath("receipt.json").exists()
    with pytest.raises(SealError, match="already consumed"):
        run_r3(*run_arguments(tmp_path))  # type: ignore[arg-type]
    assert calls == 1


def test_invalid_transfer_calibration_fails_before_ledger_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_aros_cache_calibration import calibration

    invalid = calibration()
    invalid["transfer_constraints"]["Sieve"].pop("0.01")  # type: ignore[index]
    consumed = False

    def reject_before_consumption(*args: object) -> object:
        del args
        return validate_calibration(invalid)

    def consume(*args: object) -> object:
        nonlocal consumed
        del args
        consumed = True
        raise AssertionError("ledger must not be consumed")

    monkeypatch.setattr(seal_module, "_prevalidate", reject_before_consumption)
    monkeypatch.setattr(seal_module, "_consume_ledger", consume)
    with pytest.raises(CalibrationError, match="transfer"):
        run_r3(*run_arguments(tmp_path))  # type: ignore[arg-type]
    assert consumed is False
    assert not (tmp_path / "ledger.json").exists()


def test_r3_success_requires_every_sealed_cell_and_preserves_frozen_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = fake_inputs(tmp_path)
    before = git(inputs.project.path, "status", "--porcelain=v2")
    head = git(inputs.project.path, "rev-parse", "HEAD")
    monkeypatch.setattr(seal_module, "_prevalidate", lambda *args: inputs)
    monkeypatch.setattr(
        seal_module,
        "_evaluate_temporal_portfolio",
        lambda **kwargs: {"receipt_sha256": "8" * 64, "failures": []},
    )
    monkeypatch.setattr(
        seal_module,
        "_measurement_facts",
        lambda *args: (
            [{"cell_index": index} for index in range(3)],
            [{"cell_index": index, "facts": {}} for index in range(3)],
        ),
    )
    receipt = run_r3(*run_arguments(tmp_path))  # type: ignore[arg-type]
    assert receipt["state"] == "measured"
    assert len(receipt["measurements"]) == 3
    assert len(receipt["constraints"]) == 3
    assert receipt["started_at_unix_ns"] <= receipt["ended_at_unix_ns"]
    assert git(inputs.project.path, "rev-parse", "HEAD") == head
    assert git(inputs.project.path, "status", "--porcelain=v2") == before
    assert not any(key in receipt for key in ("recommendation", "score", "pass"))


def test_temporal_portfolio_runs_every_r3_trace_at_three_exact_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    visible = json.loads(inputs["task_manifest"].read_text())
    private_traces = []
    unseen_ids = []
    for index, trace in enumerate(visible["traces"]):
        trace = dict(trace)
        trace_id = f"unseen-r3-{index}"
        unseen_ids.append(trace_id)
        diagnostics = dict(trace["diagnostics"])
        diagnostics["trace_id"] = trace_id
        diagnostics["diagnostic_sha256"] = record_sha256(
            diagnostics, "diagnostic_sha256"
        )
        trace.update(
            {
                "split": "r3",
                "trace_id": trace_id,
                "diagnostics": diagnostics,
                "diagnostic_sha256": diagnostics["diagnostic_sha256"],
            }
        )
        private_traces.append(trace)
    host = {
        "schema_version": visible["schema_version"],
        "source_commit": visible["source_commit"],
        "cache_fractions": visible["cache_fractions"],
        "traces": private_traces,
    }
    host_manifest = write_record(tmp_path / "private/r3.json", host, "manifest_sha256")
    outer_output = tmp_path / "r3-output"
    outer_output.mkdir()
    output = outer_output / "evidence"
    receipt = _evaluate_temporal_portfolio(
        task_root=inputs["task_root"],
        host_manifest=host_manifest,
        checkout=inputs["checkout"],
        candidate=inputs["candidate"],
        policy=inputs["policy"],
        source_receipt=inputs["source_receipt"],
        r0_receipt=inputs["r0_receipt"],
        output=output,
        run=runner,
    )
    assert receipt["rung"] == "r3"
    assert len(receipt["measurements"]) == len(trace_ids) * 3
    assert {item["cache_fraction"] for item in receipt["selected_cells"]} == {
        "0.01",
        "0.05",
        "0.1",
    }
    assert all(
        type(item["cache_size_bytes"]) is int for item in receipt["selected_cells"]
    )
    assert receipt["failures"] == []
    observed: list[str] = []

    def transfer_compare(
        measurement: dict[str, object], *args: object
    ) -> dict[str, object]:
        del args
        observed.append(str(measurement["trace_id"]))
        return {"throughput": True, "transfer_constraint": {}}

    monkeypatch.setattr(seal_module, "compare_transfer_constraints", transfer_compare)
    facts, constraints = seal_module._measurement_facts(
        outer_output,
        receipt,
        SimpleNamespace(
            preflight=SimpleNamespace(r0={}),
            contract={},
            calibration=SimpleNamespace(
                path=tmp_path / "calibration.json",
                calibration_sha256="a" * 64,
            ),
        ),
    )
    assert len(facts) == len(trace_ids) * 3
    assert len(constraints) == len(trace_ids) * 3
    assert set(observed) == set(unseen_ids)


def test_cli_requires_all_host_only_arguments_and_reports_only_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert r3_cli.main([]) == 2
    monkeypatch.setattr(
        r3_cli,
        "run_r3",
        lambda *args: {
            "state": "measured",
            "receipt_sha256": "a" * 64,
        },
    )
    arguments = [
        "--frozen-package",
        str(tmp_path / "package.json"),
        "--host-r3-manifest",
        str(tmp_path / "r3.json"),
        "--calibration",
        str(tmp_path / "calibration.json"),
        "--calibration-sha256",
        "a" * 64,
        "--source-receipt",
        str(tmp_path / "source.json"),
        "--candidate-r0-receipt",
        str(tmp_path / "r0.json"),
        "--checkout",
        str(tmp_path / "checkout"),
        "--ledger",
        str(tmp_path / "ledger.json"),
        "--output",
        str(tmp_path / "output"),
    ]
    assert r3_cli.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"state", "receipt_path", "receipt_sha256"}
