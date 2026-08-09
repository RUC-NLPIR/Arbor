from __future__ import annotations

import json
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from commissioning.cache_campaign import seal as seal_module
from commissioning.cache_campaign.calibration_evidence import CalibrationError
from commissioning.cache_campaign.constraints import validate_calibration
from commissioning.cache_campaign.portfolio import (
    _evaluate_temporal_portfolio,
    evaluate_portfolio,
)
from commissioning.cache_campaign.records import record_sha256, sha256_file
from commissioning.cache_campaign.seal import (
    FrozenInputs,
    ProjectBinding,
    SealError,
    _authority_id,
    _canonical_authority_paths,
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
            {
                "schema_version": 1,
                "r2_receipt_path": "/retained/r2/receipt.json",
                "r2_receipt_sha256": "2" * 64,
            }
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


def test_reproduction_ref_rejects_hash_only_json() -> None:
    digest = "2" * 64
    with pytest.raises(SealError, match="descriptor"):
        _validate_reproduction(
            json.dumps({"r2_receipt_sha256": digest}).encode(), digest
        )


def test_task_tree_and_frozen_git_blobs_reject_private_r3_bytes(
    tmp_path: Path,
) -> None:
    identity = "private-r3-tencent-photo"
    project = project_repository(tmp_path / "task", leak=identity)
    with pytest.raises(SealError, match="leaks"):
        _scan_task(project, b"{}", (identity.encode(),))
    with pytest.raises(SealError, match="package leaks"):
        _scan_task(project, identity.encode(), (identity.encode(),))


def test_frozen_git_rejects_neutral_blob_equal_to_private_trace(
    tmp_path: Path,
) -> None:
    project = project_repository(tmp_path / "task")
    private = tmp_path / "host/private.oracleGeneral"
    private.parent.mkdir()
    private.write_bytes(b"\x00\x01neutral-private-trace\xff")
    leaked = project.path / "assets/neutral.bin"
    leaked.parent.mkdir()
    leaked.write_bytes(private.read_bytes())
    git(project.path, "add", ".")
    git(project.path, "commit", "-qm", "neutral binary")
    frozen = _project_binding(project.path, git(project.path, "rev-parse", "HEAD"))
    with pytest.raises(SealError, match="leaks"):
        _scan_task(
            frozen,
            b"{}",
            (),
            (seal_module.file_binding(private),),
        )


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


def test_authority_paths_are_fixed_by_package_and_host_manifest(
    tmp_path: Path,
) -> None:
    project = tmp_path / "task"
    project.mkdir()
    value = package(project, "f" * 40)
    host = tmp_path / "host/r3.json"
    host.parent.mkdir()
    authority = _authority_id(value)
    ledger, receipt = _canonical_authority_paths(value, host)
    assert ledger == project.parent / f"r3-{authority}.consumed.json"
    assert receipt == project.parent / f"r3-{authority}.receipt.json"


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


def test_concurrent_consumers_have_exactly_one_ledger_winner(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.json"

    def consume(index: int) -> str:
        try:
            _consume_ledger(
                ledger,
                {
                    "schema_version": 1,
                    "state": "consumed",
                    "requested_at_unix_ns": index,
                },
            )
        except SealError:
            return "rejected"
        return "consumed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (1, 2)))
    assert sorted(results) == ["consumed", "rejected"]


def fake_inputs(tmp_path: Path) -> FrozenInputs:
    project = project_repository(tmp_path / "task")
    host = tmp_path / "host"
    host.mkdir()
    bound_path = host / "bound.json"
    bound_path.write_text("bound\n")
    binding = seal_module.file_binding(bound_path)
    sticky = seal_module._sticky_binding(binding)
    private_root = host / "private-inputs"
    private_root.mkdir()
    snapshot_path = private_root / "r3.json"
    snapshot_path.write_text("snapshot\n")
    snapshot_binding = seal_module.file_binding(snapshot_path)
    private_metadata = private_root.stat()
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
    package_value = package(project.path, project.head)
    authority = _authority_id(package_value)
    return FrozenInputs(
        package=package_value,
        package_binding=binding,
        project=project,
        ref_sha256s={name: "9" * 64 for name in seal_module._REF_FIELDS},
        host_manifest={"traces": [{}]},
        host_binding=binding,
        calibration=calibration,
        reproduction_r2=SimpleNamespace(
            binding=binding,
            receipt={"receipt_sha256": "2" * 64},
        ),
        preflight=preflight,
        trace_bindings=(binding,),
        contract={},
        contract_binding=binding,
        r3_evaluator_bindings={"seal_sha256": binding},
        private_snapshot=SimpleNamespace(
            root=private_root,
            root_identity=(private_metadata.st_dev, private_metadata.st_ino),
            manifest_binding=snapshot_binding,
            trace_bindings=(snapshot_binding,),
            source_manifest=sticky,
            source_traces=(sticky,),
        ),
        authority_id=authority,
        ledger=project.path.parent / f"r3-{authority}.consumed.json",
        final_receipt=project.path.parent / f"r3-{authority}.receipt.json",
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
    assert inputs.final_receipt.exists()
    alternate = list(run_arguments(tmp_path))
    alternate[-2] = tmp_path / "alternate-ledger.json"
    alternate[-1] = tmp_path / "alternate-output"
    with pytest.raises(SealError, match="already consumed"):
        run_r3(*alternate)  # type: ignore[arg-type]
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


def test_candidate_artifacts_may_differ_from_baseline_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_aros_cache_calibration import calibration

    record = calibration()
    record["host_fingerprint"] = {
        "platform": seal_module.platform.platform(),
        "machine": seal_module.platform.machine(),
        "python": seal_module.sys.version,
    }
    record["calibration_sha256"] = record_sha256(
        record, "calibration_sha256"
    )
    evaluator = {
        name: SimpleNamespace(sha256=digest)
        for name, digest in record["evaluator_sha256s"].items()  # type: ignore[union-attr]
    }
    candidate_evaluator = {
        name: record["evaluator_sha256s"][name]  # type: ignore[index]
        for name in seal_module._R0_EVALUATOR_KEYS
    }
    preflight = SimpleNamespace(
        source={"receipt_sha256": record["source_receipt_sha256"]},
        manifest={"source_commit": record["source_commit"]},
        evaluator_bindings=evaluator,
        r0={
            "evaluator": candidate_evaluator,
            "binary_sha256": "candidate-binary-differs",
            "artifact_snapshots": {},
        },
        r0_root=Path("/unused"),
        artifact_bindings={
            "release_cachesim": SimpleNamespace(sha256="candidate-binary")
        },
    )
    bound = SimpleNamespace(record=record)
    reproduction = SimpleNamespace(
        receipt={
            "task_manifest_sha256": record["task_manifest_sha256"],
            "host": record["host_fingerprint"],
        }
    )
    monkeypatch.setattr(
        seal_module,
        "_artifact_binding",
        lambda _root, _artifacts, name: SimpleNamespace(
            sha256=candidate_evaluator[f"{name.removeprefix('evaluator_')}_sha256"]
        ),
    )
    seal_module._validate_calibration(bound, preflight, reproduction)
    candidate_evaluator["evaluate_sha256"] = "0" * 64
    with pytest.raises(SealError, match="evaluator projection"):
        seal_module._validate_calibration(bound, preflight, reproduction)


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


def test_postconsume_source_restore_uses_snapshot_and_fails_sticky_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = fake_inputs(tmp_path)
    monkeypatch.setattr(seal_module, "_prevalidate", lambda *args: inputs)
    source = inputs.private_snapshot.source_manifest.file.path
    original = source.read_bytes()

    def mutate_and_restore(**kwargs: object) -> dict[str, object]:
        assert kwargs["host_manifest"] == inputs.private_snapshot.manifest_binding.path
        assert kwargs["host_manifest"] != inputs.host_binding.path
        source.write_bytes(b"changed\n")
        source.write_bytes(original)
        return {"receipt_sha256": "8" * 64, "failures": []}

    monkeypatch.setattr(
        seal_module, "_evaluate_temporal_portfolio", mutate_and_restore
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
    assert receipt["state"] == "process_failed"
    assert receipt["failures"][-1]["kind"] == "evaluation_failure"
    assert "changed after snapshot" in receipt["failures"][-1]["error"]
    assert inputs.final_receipt.exists()


def test_postconsume_output_failure_still_writes_canonical_final_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = fake_inputs(tmp_path)
    inputs.output.mkdir()
    monkeypatch.setattr(seal_module, "_prevalidate", lambda *args: inputs)
    receipt = run_r3(*run_arguments(tmp_path))  # type: ignore[arg-type]
    assert receipt["state"] == "process_failed"
    assert receipt["final_receipt_path"] == str(inputs.final_receipt)
    assert inputs.ledger.exists()
    assert inputs.final_receipt.exists()


def test_postconsume_receipt_publication_failure_retries_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = fake_inputs(tmp_path)
    inputs.output.mkdir()
    monkeypatch.setattr(seal_module, "_prevalidate", lambda *args: inputs)
    real_write = seal_module._write_final_receipt
    calls = 0

    def flaky_write(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected publication failure")
        return real_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(seal_module, "_write_final_receipt", flaky_write)
    receipt = run_r3(*run_arguments(tmp_path))  # type: ignore[arg-type]
    assert calls == 2
    assert receipt["state"] == "process_failed"
    assert receipt["failures"][-1]["kind"] == "receipt_publication_failure"
    assert inputs.final_receipt.exists()


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


def test_full_bound_r2_reproduction_chain_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    r2_output = tmp_path / "candidate-r2"
    r2_receipt = evaluate_portfolio(
        rung="r2",
        task_root=inputs["task_root"],
        task_manifest=inputs["task_manifest"],
        checkout=inputs["checkout"],
        candidate=inputs["candidate"],
        policy=inputs["policy"],
        source_receipt=inputs["source_receipt"],
        r0_receipt=inputs["r0_receipt"],
        output=r2_output,
        run=runner,
    )
    task_root = inputs["task_root"]
    git(task_root, "init", "-q")
    git(task_root, "config", "user.name", "R3 Reproduction")
    git(task_root, "config", "user.email", "r3-repro@example.invalid")
    git(task_root, "add", ".")
    git(task_root, "commit", "-qm", "frozen task evidence")
    frozen = _project_binding(task_root, git(task_root, "rev-parse", "HEAD"))
    preflight = seal_module._preflight(
        task_root=task_root,
        task_manifest=inputs["task_manifest"],
        checkout=inputs["checkout"],
        candidate=inputs["candidate"],
        policy=inputs["policy"],
        source_receipt=inputs["source_receipt"],
        r0_receipt=inputs["r0_receipt"],
        output=tmp_path / "unused-output",
    )
    os.close(preflight.output_parent.descriptor)
    package_value = {
        "candidate_commit": inputs["candidate"],
        "policy": inputs["policy"],
        "r0_receipt_sha256": preflight.r0["receipt_sha256"],
        "r2_receipt_sha256": r2_receipt["receipt_sha256"],
        "r3_commitment_sha256": json.loads(
            inputs["task_manifest"].read_text()
        )["r3_commitment_sha256"],
    }
    descriptor = {
        "schema_version": 1,
        "r2_receipt_path": str(r2_output / "receipt.json"),
        "r2_receipt_sha256": r2_receipt["receipt_sha256"],
    }
    validated = seal_module._validate_candidate_r2(
        descriptor,
        project=frozen,
        package=package_value,
        preflight=preflight,
    )
    assert validated.receipt["receipt_sha256"] == r2_receipt["receipt_sha256"]
    with pytest.raises(SealError, match="binding mismatch"):
        seal_module._validate_candidate_r2(
            descriptor,
            project=frozen,
            package={**package_value, "policy": "OtherPolicy"},
            preflight=preflight,
        )


def test_real_derived_candidate_prevalidates_with_distinct_baseline_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from commissioning.cache_campaign.evaluate import evaluate_r0
    from tests.test_aros_cache_calibration import (
        CalibrationRun,
        calibration,
        write_bound_calibration,
    )
    from tests.test_aros_cache_evaluator import oracle_trace, portfolio_manifest
    from tests.test_aros_cache_r0 import (
        FakeRun,
        POLICY,
        repository,
        source_receipt,
        write as r0_write,
    )

    checkout, old_base, old_candidate, lock = repository(tmp_path)
    git(checkout, "checkout", "-q", old_base)
    r0_write(
        checkout,
        "libCacheSim/include/libCacheSim.h",
        "/* required phase header */\n",
    )
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "base phase header")
    base = git(checkout, "rev-parse", "HEAD")
    git(checkout, "cherry-pick", "--no-edit", old_candidate)
    candidate = git(checkout, "rev-parse", "HEAD")
    lock["commit"] = base
    lock["tree"] = git(checkout, "rev-parse", f"{base}^{{tree}}")
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    monkeypatch.setattr("commissioning.cache_campaign.portfolio.SOURCE_LOCK", lock)
    (tmp_path / "host").mkdir()
    source = source_receipt(tmp_path / "host/source.json", lock)
    r0_output = tmp_path / "host/candidate-r0"
    r0 = evaluate_r0(
        checkout=checkout,
        base=base,
        candidate=candidate,
        policy=POLICY,
        source_receipt=source,
        output=r0_output,
        run=FakeRun(),
    )
    task_root = tmp_path / "task"
    task_root.mkdir()
    task_manifest, _trace_ids = portfolio_manifest(
        task_root / "manifests/task.json", tmp_path / "public-traces", base
    )
    public_manifest = json.loads(task_manifest.read_text())
    private_path = tmp_path / "host/private/unseen.oracleGeneral"
    working_set, size_bytes = oracle_trace(private_path, 99)
    trace = dict(public_manifest["traces"][0])
    diagnostics = dict(trace["diagnostics"])
    diagnostics.update(
        {
            "trace_id": "unseen-r3-derived",
            "working_set_bytes": working_set,
        }
    )
    diagnostics["diagnostic_sha256"] = record_sha256(
        diagnostics, "diagnostic_sha256"
    )
    trace.update(
        {
            "trace_id": "unseen-r3-derived",
            "split": "r3",
            "organization": "unseen-private-org",
            "application": "unseen-private-app",
            "dataset": "unseen-private-data",
            "provenance_url": "https://private.invalid/unseen-data",
            "license_ref": "private-unseen-license",
            "path": str(private_path),
            "origin_sha256": "f" * 64,
            "working_set_bytes": working_set,
            "sha256": sha256_file(private_path),
            "size_bytes": size_bytes,
            "diagnostic_sha256": diagnostics["diagnostic_sha256"],
            "diagnostics": diagnostics,
        }
    )
    host_manifest = write_record(
        tmp_path / "host/private/r3.json",
        {
            "schema_version": 1,
            "source_commit": base,
            "cache_fractions": [0.01, 0.05, 0.10],
            "traces": [trace],
        },
        "manifest_sha256",
    )
    public_manifest["r3_commitment_sha256"] = json.loads(
        host_manifest.read_text()
    )["manifest_sha256"]
    task_manifest = write_record(
        task_manifest, public_manifest, "manifest_sha256"
    )
    r2_output = tmp_path / "host/candidate-r2"
    r2 = evaluate_portfolio(
        rung="r2",
        task_root=task_root,
        task_manifest=task_manifest,
        checkout=checkout,
        candidate=candidate,
        policy=POLICY,
        source_receipt=source,
        r0_receipt=r0_output / "receipt.json",
        output=r2_output,
        run=CalibrationRun("20.00"),
    )
    calibration_record = calibration()
    calibration_record.update(
        {
            "task_manifest_sha256": public_manifest["manifest_sha256"],
            "source_receipt_sha256": json.loads(source.read_text())[
                "receipt_sha256"
            ],
            "source_commit": base,
            "evaluator_sha256s": r2["evaluator"],
            "host_fingerprint": r2["host"],
        }
    )
    calibration_record["calibration_sha256"] = record_sha256(
        calibration_record, "calibration_sha256"
    )
    calibration_path, calibration_sha = write_bound_calibration(
        tmp_path / "host/calibration.json", calibration_record
    )
    refs = {
        "knowledge/claims/C-0001/claim.md": "claim\n",
        "experiments/confirmation/preregistration.md": "preregistered\n",
        "reviews/RV-0001/report.md": "review\n",
        "reviews/RV-0001/principal-response.md": "response\n",
        "reviews/RV-0001/reproduction.json": json.dumps(
            {
                "schema_version": 1,
                "r2_receipt_path": str(r2_output / "receipt.json"),
                "r2_receipt_sha256": r2["receipt_sha256"],
            }
        ),
    }
    for relative, raw in refs.items():
        path = task_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw)
    git(task_root, "init", "-q")
    git(task_root, "config", "user.name", "Derived Candidate")
    git(task_root, "config", "user.email", "derived@example.invalid")
    git(task_root, "add", ".")
    git(task_root, "commit", "-qm", "freeze derived candidate")
    frozen_commit = git(task_root, "rev-parse", "HEAD")
    package_value = {
        "schema_version": 1,
        "project": str(task_root),
        "frozen_commit": frozen_commit,
        "candidate_commit": candidate,
        "policy": POLICY,
        "candidate_diff_sha256": r0["candidate_diff_sha256"],
        "policy_contract_sha256": r0["contract_sha256"],
        "claim_ref": "knowledge/claims/C-0001/claim.md",
        "preregistration_ref": "experiments/confirmation/preregistration.md",
        "review_ref": "reviews/RV-0001/report.md",
        "principal_response_ref": "reviews/RV-0001/principal-response.md",
        "reproduction_ref": "reviews/RV-0001/reproduction.json",
        "r0_receipt_sha256": r0["receipt_sha256"],
        "r2_receipt_sha256": r2["receipt_sha256"],
        "calibration_sha256": calibration_sha,
        "r3_commitment_sha256": json.loads(host_manifest.read_text())[
            "manifest_sha256"
        ],
    }
    package_path = tmp_path / "host/frozen-package.json"
    package_path.write_text(json.dumps(package_value, sort_keys=True) + "\n")
    ledger, _final = _canonical_authority_paths(package_value, host_manifest)
    inputs = seal_module._prevalidate(
        package_path,
        host_manifest,
        calibration_path,
        calibration_sha,
        source,
        r0_output / "receipt.json",
        checkout,
        ledger,
        tmp_path / "host/r3-output",
    )
    try:
        assert r0["binary_sha256"] != calibration_record["binary_sha256"]
        assert inputs.preflight.r0["candidate_commit"] == candidate
    finally:
        seal_module.cleanup_owned(
            inputs.private_snapshot.root,
            inputs.private_snapshot.root_identity,
        )
    mismatched = {**package_value, "candidate_commit": "0" * 40}
    mismatch_path = tmp_path / "host/mismatched-package.json"
    mismatch_path.write_text(json.dumps(mismatched, sort_keys=True) + "\n")
    mismatch_ledger, _receipt = _canonical_authority_paths(
        mismatched, host_manifest
    )
    with pytest.raises(SealError, match="candidate"):
        seal_module._prevalidate(
            mismatch_path,
            host_manifest,
            calibration_path,
            calibration_sha,
            source,
            r0_output / "receipt.json",
            checkout,
            mismatch_ledger,
            tmp_path / "host/mismatched-output",
        )


def test_cli_requires_all_host_only_arguments_and_reports_only_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert r3_cli.main([]) == 2
    monkeypatch.setattr(
        r3_cli,
        "run_r3",
        lambda *args: {
            "state": "measured",
            "final_receipt_path": str(tmp_path / "final.json"),
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
