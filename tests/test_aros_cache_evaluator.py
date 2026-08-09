from __future__ import annotations

import hashlib
import json
import stat
import struct
import subprocess
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from commissioning.cache_campaign.cachesim import ChildResult
from commissioning.cache_campaign.evaluate import evaluate_portfolio
from commissioning.cache_campaign.records import ContractError, ParetoMeasurement
from commissioning.cache_campaign.records import record_sha256, sha256_file
from scripts import run_aros_cache_eval as eval_cli


ORACLE = struct.Struct("<IQIq")


def measurement(**changes: object) -> ParetoMeasurement:
    values: dict[str, object] = {
        "rung": "r1",
        "split": "dev",
        "trace_id": "dev-a",
        "policy": "Sieve",
        "cache_fraction": Decimal("0.10"),
        "cache_size_bytes": 123,
        "request_count": 160,
        "object_miss_ratio": Decimal("0.25"),
        "byte_miss_ratio": Decimal("0.5"),
        "simulator_throughput_mqps": Decimal("1.25"),
        "cpu_ns_per_request": Decimal("12.5"),
        "metadata_bytes_per_object": Decimal("3"),
        "global_metadata_bytes": 24,
        "metadata_measurement_sha256": "a" * 64,
    }
    values.update(changes)
    return ParetoMeasurement(**values)  # type: ignore[arg-type]


def test_pareto_measurement_is_frozen_and_serializes_canonical_decimals() -> None:
    value = measurement()
    assert value.to_record() == {
        "rung": "r1",
        "split": "dev",
        "trace_id": "dev-a",
        "policy": "Sieve",
        "cache_fraction": "0.1",
        "cache_size_bytes": 123,
        "request_count": 160,
        "object_miss_ratio": "0.25",
        "byte_miss_ratio": "0.5",
        "simulator_throughput_mqps": "1.25",
        "cpu_ns_per_request": "12.5",
        "metadata_bytes_per_object": "3",
        "global_metadata_bytes": 24,
        "metadata_measurement_sha256": "a" * 64,
    }
    assert ParetoMeasurement.from_record(value.to_record()) == value
    with pytest.raises(FrozenInstanceError):
        value.request_count = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("rung", "r0"),
        ("split", "secret"),
        ("trace_id", ""),
        ("policy", ""),
        ("cache_fraction", Decimal("NaN")),
        ("cache_fraction", Decimal("0")),
        ("cache_fraction", Decimal("1.1")),
        ("cache_size_bytes", True),
        ("cache_size_bytes", 0),
        ("request_count", 0),
        ("object_miss_ratio", Decimal("-0.1")),
        ("object_miss_ratio", Decimal("1.1")),
        ("byte_miss_ratio", Decimal("Infinity")),
        ("simulator_throughput_mqps", Decimal("0")),
        ("cpu_ns_per_request", Decimal("0")),
        ("metadata_bytes_per_object", Decimal("-1")),
        ("global_metadata_bytes", -1),
        ("metadata_measurement_sha256", "A" * 64),
    ],
)
def test_pareto_measurement_rejects_invalid_fields(field: str, invalid: object) -> None:
    with pytest.raises(ContractError):
        measurement(**{field: invalid})


def test_pareto_measurement_rejects_non_decimal_metric_types() -> None:
    with pytest.raises(ContractError):
        measurement(object_miss_ratio=0.25)


def test_pareto_measurement_rejects_noncanonical_decimal_strings() -> None:
    value = measurement().to_record()
    value["cache_fraction"] = "0.10"
    with pytest.raises(ContractError, match="canonical"):
        ParetoMeasurement.from_record(value)


def git(path: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=path, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def write_record(path: Path, value: dict[str, object], hash_field: str) -> Path:
    value[hash_field] = record_sha256(value, hash_field)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    return path


def portfolio_checkout(tmp_path: Path) -> tuple[Path, dict[str, object], str, str]:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "Test")
    git(checkout, "config", "user.email", "test@example.invalid")
    git(checkout, "remote", "add", "origin", "https://example.invalid/libCacheSim.git")
    source = checkout / "libCacheSim/cache/eviction/Sieve.c"
    source.parent.mkdir(parents=True)
    source.write_text("/* baseline */\n")
    cache_init = checkout / "libCacheSim/bin/cachesim/cache_init.h"
    cache_init.parent.mkdir(parents=True)
    cache_init.write_text("/* pinned cache init */\n")
    include = checkout / "libCacheSim/include/libCacheSim.h"
    include.parent.mkdir(parents=True)
    include.write_text("/* pinned public header */\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "baseline")
    commit = git(checkout, "rev-parse", "HEAD")
    tree = git(checkout, "rev-parse", "HEAD^{tree}")
    lock: dict[str, object] = {
        "schema_version": 1,
        "repository_url": "https://example.invalid/libCacheSim.git",
        "commit": commit,
        "tree": tree,
        "configure_argv": ["cmake", "configure"],
        "build_argv": ["cmake", "build"],
        "test_argv": ["ctest"],
        "binary": "_build/bin/cachesim",
        "baseline_policies": ["Sieve", "S3FIFO"],
        "comparison_policies": ["Sieve", "S3FIFO"],
    }
    return checkout, lock, commit, tree


def portfolio_source_receipt(
    path: Path, lock: dict[str, object]
) -> Path:
    value: dict[str, object] = {
        "schema_version": 1,
        "repository_url": lock["repository_url"],
        "commit": lock["commit"],
        "tree": lock["tree"],
        "clean": True,
        "commands": [
            {
                "argv": argv,
                "returncode": 0,
                "stdout_sha256": "1" * 64,
                "stderr_sha256": "2" * 64,
            }
            for argv in (
                lock["configure_argv"],
                lock["build_argv"],
                lock["test_argv"],
            )
        ],
        "versions": {"cmake": "cmake version test", "ninja": "1.test"},
        "compilers": {
            "c": {"path": "/usr/bin/cc", "version": "cc test"},
            "cxx": {"path": "/usr/bin/c++", "version": "c++ test"},
        },
        "interpreter": "python test",
        "platform": "test",
        "binary": lock["binary"],
        "binary_sha256": "3" * 64,
    }
    return write_record(path, value, "receipt_sha256")


def artifact_record(root: Path, name: str, raw: bytes) -> dict[str, object]:
    path = root / "artifact_snapshots" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o400)
    metadata = path.stat()
    return {
        "source_path": f"/deleted/{name}",
        "source_identity": {"device": 1, "inode": 1},
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_path": f"artifact_snapshots/{name}",
        "snapshot_identity": {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
        },
        "binding_intact": True,
    }


def portfolio_r0_receipt(
    root: Path,
    source_receipt: Path,
    candidate: str,
    tree: str,
    checkout: Path,
) -> Path:
    empty_diff_sha256 = hashlib.sha256(b"").hexdigest()
    argv_log = root / "argv.log"
    fake_binary = f'''#!/usr/bin/python3
import json
import pathlib
import sys
with pathlib.Path({str(argv_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
output = pathlib.Path(next(item.split("=", 1)[1] for item in sys.argv if item.startswith("--output=")))
line = (f"{{sys.argv[1]}} {{sys.argv[3]}} cache size {{sys.argv[4]}}B, 160 req, "
        "miss ratio 0.0625, byte miss ratio 0.0625, throughput 1.25 MQPS\\n")
with output.open("a", encoding="ascii") as stream:
    stream.write(line)
sys.stdout.write(line)
'''.encode("ascii")
    binary = artifact_record(root, "release_cachesim", fake_binary)
    archive = artifact_record(root, "release_archive", b"fake static archive\n")
    cmake_cache = artifact_record(
        root,
        "release_cmake_cache",
        (
            b"CMAKE_C_COMPILER:FILEPATH=/usr/bin/cc\n"
            b"CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/c++\n"
            b"GLib_INCLUDE_DIRS:PATH=/usr/include/glib-2.0\n"
            b"GLib_LIBRARIES:STRING=glib-2.0\n"
        ),
    )
    metadata_stdout = (
        b"global_metadata_bytes=24\n"
        b"sample=1000 live_bytes=3024 resident_objects=1000\n"
        b"sample=5000 live_bytes=15024 resident_objects=5000\n"
        b"sample=10000 live_bytes=30024 resident_objects=10000\n"
        b"status=ok\n"
    )
    metadata_dir = root / "commands/14-metadata-run"
    metadata_dir.mkdir(parents=True)
    metadata_stdout_path = metadata_dir / "stdout.raw"
    metadata_stderr_path = metadata_dir / "stderr.raw"
    metadata_stdout_path.write_bytes(metadata_stdout)
    metadata_stderr_path.write_bytes(b"")

    def raw_receipt(path: Path) -> dict[str, object]:
        raw = path.read_bytes()
        return {
            "path": str(path.relative_to(root)),
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "binding_intact": True,
        }

    stdout_receipt = raw_receipt(metadata_stdout_path)
    stderr_receipt = raw_receipt(metadata_stderr_path)

    def inventory_item(receipt: dict[str, object]) -> dict[str, object]:
        metadata = (root / str(receipt["path"])).stat()
        identity = {"device": metadata.st_dev, "inode": metadata.st_ino}
        return {
            "path": receipt["path"],
            "identity": identity,
            "size_bytes": receipt["size_bytes"],
            "sha256": receipt["sha256"],
            "present": True,
            "observed_identity": identity,
            "observed_size_bytes": receipt["size_bytes"],
            "observed_sha256": receipt["sha256"],
            "binding_intact": True,
        }
    value: dict[str, object] = {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": "r0",
        "source_receipt_path": str(source_receipt.resolve()),
        "source_receipt_sha256": json.loads(source_receipt.read_text())[
            "receipt_sha256"
        ],
        "source_receipt_file_sha256": sha256_file(source_receipt),
        "repository_url": "https://example.invalid/libCacheSim.git",
        "base_commit": candidate,
        "base_tree": tree,
        "candidate_commit": candidate,
        "candidate_tree": tree,
        "candidate_diff_sha256": empty_diff_sha256,
        "changed_path_sha256": {},
        "policy": "Sieve",
        "policy_source_sha256": hashlib.sha256(
            (checkout / "libCacheSim/cache/eviction/Sieve.c").read_bytes()
        ).hexdigest(),
        "candidate_test_sha256": None,
        "contract_sha256": None,
        "binary": "_build-release/bin/cachesim",
        "binary_sha256": binary["sha256"],
        "binary_post_run_sha256": binary["sha256"],
        "checks": {
            "source_binding": True,
            "evidence_binding": True,
            "build": True,
            "full_tests": True,
            "candidate_test": None,
            "sanitizer": True,
            "deterministic": True,
            "capacity": True,
            "metadata_probe": True,
        },
        "scope": {
            "allowed_paths": True,
            "baseline_unchanged": True,
            "additive_wiring_only": True,
            "contract_bound": None,
            "changed_paths": [],
            "diff_sha256": empty_diff_sha256,
        },
        "declared_metadata": None,
        "measured_metadata": {
            "bytes_per_object": "3",
            "global_bytes": 24,
            "measurement_sha256": stdout_receipt["sha256"],
            "within_budget": None,
        },
        "complexity_audit": "pending_independent_review",
        "synthetic_trace": {},
        "simulations": [],
        "simulator_result": {},
        "capacity_measurement": {},
        "commands": [
            {
                "index": 14,
                "label": "metadata-run",
                "argv": ["/usr/bin/env", "LD_PRELOAD=/retained", "/metadata-probe"],
                "cwd": str(root),
                "timeout_seconds": 120.0,
                "max_output_bytes": 16 * 1024 * 1024,
                "returncode": 0,
                "wall_ns": 1,
                "cpu_ns": 1,
                "stdout": stdout_receipt,
                "stderr": stderr_receipt,
            }
        ],
        "artifact_snapshots": {
            "release_cachesim": binary,
            "release_archive": archive,
            "release_cmake_cache": cmake_cache,
        },
        "probes": {},
        "evaluator": {},
        "host": {"platform": "test", "machine": "test", "python": "test"},
        "timings": {},
        "errors": [],
        "evidence_inventory": [
            inventory_item(stderr_receipt),
            inventory_item(stdout_receipt),
        ],
        "unexpected_stage_entries": [],
    }
    return write_record(root / "receipt.json", value, "receipt_sha256")


def oracle_trace(path: Path, seed: int) -> tuple[int, int]:
    raw = bytearray()
    sizes: dict[int, int] = {}
    for index in range(162):
        object_id = seed * 1_000 + index % 10
        size = 64
        sizes[object_id] = size
        next_access = index + 11 if index < 152 else -1
        raw.extend(ORACLE.pack(index, object_id, size, next_access))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return sum(sizes.values()), len(raw)


def portfolio_manifest(path: Path, trace_root: Path, commit: str) -> tuple[Path, list[str]]:
    traces: list[dict[str, object]] = []
    trace_ids: list[str] = []
    for index, split in enumerate(["dev", "dev", "dev", "dev", "visible"]):
        trace_id = f"{split}-{index}"
        trace_ids.append(trace_id)
        trace_path = trace_root / f"{trace_id}.oracleGeneral"
        working_set, size_bytes = oracle_trace(trace_path, index + 1)
        diagnostics: dict[str, object] = {
            "schema_version": 1,
            "trace_id": trace_id,
            "request_count": 162,
            "unique_object_count": 10,
            "working_set_bytes": working_set,
            "one_hit_object_fraction": {"numerator": 0, "denominator": 10},
            "one_hit_request_fraction": {"numerator": 0, "denominator": 162},
            "reuse_distance": {
                "bin_convention": "fixture",
                "counts": {"3": 152},
                "no_next_count": 10,
            },
        }
        diagnostics["diagnostic_sha256"] = record_sha256(
            diagnostics, "diagnostic_sha256"
        )
        traces.append(
            {
                "trace_id": trace_id,
                "split": split,
                "organization": f"org-{index}",
                "application": f"app-{index}",
                "dataset": f"data-{index}",
                "provenance_url": "https://example.invalid/data",
                "license_ref": "fixture",
                "path": str(trace_path.resolve()),
                "trace_type": "oracleGeneral",
                "origin_sha256": str(index + 1) * 64,
                "start_request": 0,
                "warmup_seconds": 1,
                "max_requests": 162,
                "working_set_bytes": working_set,
                "sha256": sha256_file(trace_path),
                "size_bytes": size_bytes,
                "diagnostic_sha256": diagnostics["diagnostic_sha256"],
                "diagnostics": diagnostics,
            }
        )
    value: dict[str, object] = {
        "schema_version": 1,
        "source_commit": commit,
        "cache_fractions": [0.01, 0.05, 0.10],
        "traces": traces,
        "r3_commitment_sha256": "6" * 64,
    }
    return write_record(path, value, "manifest_sha256"), trace_ids


class PortfolioRun:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.modes: list[int] = []
        self.limits: list[tuple[float, int]] = []

    def __call__(
        self,
        argv: list[str],
        output_dir: Path,
        *,
        cwd: Path | None,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ChildResult:
        del cwd
        self.argv.append(list(argv))
        self.modes.append(stat.S_IMODE(Path(argv[0]).stat().st_mode))
        self.limits.append((timeout_seconds, max_output_bytes))
        output_dir.mkdir(mode=0o700)
        program = Path(argv[0]).name
        if argv[0] == "/usr/bin/cc":
            compiled = Path(argv[argv.index("-o") + 1])
            compiled.write_bytes(b"fake phase probe\n")
            compiled.chmod(0o500)
            stdout = b""
        elif program == "phase-probe":
            lines = []
            for index in range(16):
                misses = 10 if index == 0 else 0
                lines.append(
                    f"phase={index} requests=10 object_misses={misses} "
                    f"request_bytes=640 byte_misses={misses * 64}"
                )
            stdout = ("\n".join(lines) + "\n").encode()
        else:
            request_count = 160
            stdout = (
                f"{argv[1]} Sieve cache size 6B, {request_count} req, "
                "miss ratio 0.0625, byte miss ratio 0.0625, throughput 1.25 MQPS\n"
            ).encode()
        stderr = b""
        if program == "cachesim":
            side_effect = Path(
                next(item for item in argv if item.startswith("--output=")).split(
                    "=", 1
                )[1]
            )
            side_effect.write_bytes(stdout)
        stdout_path = output_dir / "stdout.raw"
        stderr_path = output_dir / "stderr.raw"
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(stderr)
        return ChildResult(
            argv=tuple(argv),
            returncode=0,
            wall_ns=2_000,
            cpu_ns=1_000,
            stdout_path=stdout_path,
            stdout_bytes=len(stdout),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_path=stderr_path,
            stderr_bytes=0,
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        )


def portfolio_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], PortfolioRun, list[str]]:
    checkout, lock, candidate, tree = portfolio_checkout(tmp_path)
    monkeypatch.setattr("commissioning.cache_campaign.evaluate.SOURCE_LOCK", lock)
    monkeypatch.setattr("commissioning.cache_campaign.portfolio.SOURCE_LOCK", lock)
    source = portfolio_source_receipt(tmp_path / "source.json", lock)
    r0 = portfolio_r0_receipt(
        tmp_path / "r0", source, candidate, tree, checkout
    )
    task_root = tmp_path / "task"
    task_root.mkdir()
    manifest, trace_ids = portfolio_manifest(
        task_root / "manifests/task.json", tmp_path / "traces", candidate
    )
    runner = PortfolioRun()
    values: dict[str, object] = {
        "task_root": task_root,
        "task_manifest": manifest,
        "checkout": checkout,
        "candidate": candidate,
        "policy": "Sieve",
        "source_receipt": source,
        "r0_receipt": r0,
        "output": tmp_path / "portfolio-output",
        "run": runner,
    }
    return values, runner, trace_ids


def test_r1_is_first_three_dev_windows_by_three_exact_integer_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    receipt = evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert len(receipt["measurements"]) == 9
    assert [item["trace_id"] for item in receipt["measurements"]] == [
        trace_id
        for trace_id in trace_ids[:3]
        for _fraction in range(3)
    ]
    assert [item["cache_fraction"] for item in receipt["measurements"][:3]] == [
        "0.01",
        "0.05",
        "0.1",
    ]
    assert len(runner.argv) == 9
    for argv in runner.argv:
        assert argv[2:4] == ["oracleGeneral", "Sieve"]
        assert argv[4] in {"6", "32", "64"}
        assert argv[4] not in {"auto", "0.01", "0.05", "0.10"}
        assert argv[5:10] == [
            "--num-thread=1",
            "--num-req=162",
            "--warmup-sec=1",
            "--consider-obj-metadata=true",
            "--print-head-req=false",
        ]
        assert argv[10].startswith("--output=")
    assert runner.modes == [0o500] * 9
    assert runner.limits == [(3600.0, 64 * 1024 * 1024)] * 9
    assert receipt["failures"] == []


def test_r1_uses_exact_decimal_cpu_and_r0_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    receipt = evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    first_path = inputs["output"] / receipt["measurements"][0]["path"]
    retained = json.loads(first_path.read_text())
    assert retained["cpu_ns_per_request"] == "6.25"
    assert retained["metadata_bytes_per_object"] == "3"
    assert retained["global_metadata_bytes"] == 24
    assert retained["metadata_measurement_sha256"] == json.loads(
        inputs["r0_receipt"].read_text()
    )["measured_metadata"]["measurement_sha256"]
    assert retained["request_count"] == 160
    assert retained["simulator_output"]["sha256"] == retained["process"][
        "stdout"
    ]["sha256"]


def test_r1_real_fake_executable_receives_one_exact_cell_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    del inputs["run"]
    receipt = evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert len(receipt["measurements"]) == 9
    logged = [
        json.loads(line)
        for line in (inputs["r0_receipt"].parent / "argv.log").read_text().splitlines()
    ]
    assert len(logged) == 9
    assert all(len(argv) == 10 for argv in logged)
    assert all(argv[1:3] == ["oracleGeneral", "Sieve"] for argv in logged)


def test_portfolio_rejects_r3_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="R3"):
        evaluate_portfolio(rung="r3", **inputs)  # type: ignore[arg-type]
    assert runner.argv == []


def test_r2_covers_all_frozen_traces_and_binds_continuous_phase_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    receipt = evaluate_portfolio(rung="r2", **inputs)  # type: ignore[arg-type]
    assert len(receipt["measurements"]) == len(trace_ids) * 3
    assert [item["trace_id"] for item in receipt["measurements"]] == [
        trace_id for trace_id in trace_ids for _fraction in range(3)
    ]
    assert runner.argv[0][0] == "/usr/bin/cc"
    assert "-std=c11" in runner.argv[0]
    assert len(runner.argv) == 1 + len(trace_ids) * 6
    assert [Path(argv[0]).name for argv in runner.argv[1:]] == [
        name for _cell in range(len(trace_ids) * 3) for name in ("cachesim", "phase-probe")
    ]
    first_path = inputs["output"] / receipt["measurements"][0]["path"]
    measurement = json.loads(first_path.read_text())
    phase = measurement["phase_diagnostic"]
    assert len(phase["bins"]) == 16
    assert [item["object_misses"] for item in phase["bins"]] == [10] + [0] * 15
    assert phase["request_count"] == measurement["request_count"] == 160
    assert phase["request_bytes"] == 10_240
    assert phase["frozen_trace_diagnostic_sha256"] == measurement[
        "trace_diagnostic_sha256"
    ]
    assert measurement["frozen_trace_diagnostic"]["one_hit_object_fraction"] == {
        "denominator": 10,
        "numerator": 0,
    }
    assert measurement["frozen_trace_diagnostic"]["reuse_distance"]["counts"] == {
        "3": 152
    }
    assert len(receipt["frozen_trace_diagnostics"]) == len(trace_ids)
    assert receipt["phase_probe"]["source_sha256"] == sha256_file(
        inputs["output"] / receipt["phase_probe"]["source_path"]
    )
    assert receipt["failures"] == []


def test_cli_routes_r1_with_exact_arguments_and_prints_only_receipt_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_root = tmp_path / "task"
    checkout = tmp_path / "checkout"
    task_root.mkdir()
    checkout.mkdir()
    paths = {}
    for name in ("task_manifest", "source_receipt", "r0_receipt"):
        path = tmp_path / f"{name}.json"
        path.write_text("{}")
        paths[name] = path
    output = tmp_path / "output"
    observed: dict[str, object] = {}

    def fake_portfolio(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        output.mkdir()
        return {
            "receipt_sha256": "a" * 64,
            "measurements": [{"measurement_sha256": "b" * 64}],
            "failures": [],
        }

    monkeypatch.setattr(eval_cli, "evaluate_portfolio", fake_portfolio)
    result = eval_cli.main(
        [
            "--rung",
            "r1",
            "--task-root",
            str(task_root),
            "--task-manifest",
            str(paths["task_manifest"]),
            "--checkout",
            str(checkout),
            "--candidate",
            "b" * 40,
            "--policy",
            "Sieve",
            "--source-receipt",
            str(paths["source_receipt"]),
            "--r0-receipt",
            str(paths["r0_receipt"]),
            "--output",
            str(output),
        ]
    )
    assert result == 0
    assert observed["rung"] == "r1"
    assert observed["task_root"] == task_root.resolve()
    assert json.loads(capsys.readouterr().out) == {
        "failure_count": 0,
        "measurement_count": 1,
        "receipt_path": str(output / "receipt.json"),
        "receipt_sha256": "a" * 64,
        "rung": "r1",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["--rung", "r3"],
        ["--rung", "r1", "--base", "a" * 40],
        ["--rung", "r0", "--task-root", "/tmp/not-allowed"],
    ],
)
def test_cli_rejects_r3_and_cross_rung_arguments(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert eval_cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: invalid command line\n"


def test_trace_mutation_after_child_aborts_before_next_launch_and_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, base_runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)

    class TraceMutatingRun(PortfolioRun):
        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            result = super().__call__(*args, **kwargs)  # type: ignore[arg-type]
            argv = args[0]
            assert isinstance(argv, list)
            if Path(argv[0]).name == "cachesim" and len(self.argv) == 1:
                with Path(argv[1]).open("ab") as stream:
                    stream.write(b"mutation")
            return result

    runner = TraceMutatingRun()
    inputs["run"] = runner
    receipt = evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert len(runner.argv) == 1
    assert receipt["measurements"] == []
    assert len(receipt["failures"]) == 1
    assert receipt["provenance"]["final_binding_intact"] is False
    failure_path = inputs["output"] / receipt["failures"][0]["path"]
    failure = json.loads(failure_path.read_text())
    assert failure["kind"] == "binding_failure"
    assert failure["remaining_cell_indices"] == list(range(1, 9))
    assert (failure_path.parent / "process.json").exists()
    assert base_runner.argv == []


def test_nonzero_process_has_process_and_failure_receipts_but_no_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)

    class NonzeroRun(PortfolioRun):
        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            result = super().__call__(*args, **kwargs)  # type: ignore[arg-type]
            argv = args[0]
            assert isinstance(argv, list)
            if Path(argv[0]).name == "cachesim" and not any(
                Path(item[0]).name == "cachesim" for item in self.argv[:-1]
            ):
                return ChildResult(**{**result.__dict__, "returncode": 7})
            return result

    runner = NonzeroRun()
    inputs["run"] = runner
    receipt = evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert len(receipt["measurements"]) == 8
    assert len(receipt["failures"]) == 1
    cell = inputs["output"] / "measurements/0000"
    assert (cell / "request.json").exists()
    assert (cell / "process.json").exists()
    assert (cell / "failure.json").exists()
    assert not (cell / "measurement.json").exists()


def test_preflight_r0_binary_mutation_publishes_failure_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    snapshot = inputs["r0_receipt"].parent / "artifact_snapshots/release_cachesim"
    snapshot.chmod(0o600)
    snapshot.write_bytes(b"replaced binary\n")
    snapshot.chmod(0o400)
    with pytest.raises(ValueError, match="snapshot binding"):
        evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert runner.argv == []
    root = json.loads((inputs["output"] / "receipt.json").read_text())
    assert root["measurements"] == []
    assert len(root["failures"]) == 1
    assert root["provenance"]["final_binding_intact"] is False


def test_rehashed_r0_metadata_forgery_is_rejected_against_raw_probe_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    r0_path = inputs["r0_receipt"]
    value = json.loads(r0_path.read_text())
    value["measured_metadata"]["bytes_per_object"] = "99"
    value["measured_metadata"]["global_bytes"] = 999
    value["receipt_sha256"] = record_sha256(value, "receipt_sha256")
    r0_path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    with pytest.raises(ValueError, match="metadata.*evidence"):
        evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert runner.argv == []


def test_r2_phase_compile_failure_is_retained_and_launches_no_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)

    class CompileFailureRun(PortfolioRun):
        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            result = super().__call__(*args, **kwargs)  # type: ignore[arg-type]
            argv = args[0]
            assert isinstance(argv, list)
            if argv[0] == "/usr/bin/cc":
                return ChildResult(**{**result.__dict__, "returncode": 9})
            return result

    runner = CompileFailureRun()
    inputs["run"] = runner
    receipt = evaluate_portfolio(rung="r2", **inputs)  # type: ignore[arg-type]
    assert len(runner.argv) == 1
    assert receipt["measurements"] == []
    assert len(receipt["failures"]) == 1
    failure = json.loads(
        (inputs["output"] / receipt["failures"][0]["path"]).read_text()
    )
    assert failure["kind"] == "process_failure"
    assert failure["label"] == "phase-compile"
    assert failure["process_sha256"] == json.loads(
        (inputs["output"] / "phase-compile-process/process.json").read_text()
    )["process_sha256"]


def test_r2_unavailable_phase_compiler_still_retains_process_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)

    class UnavailableCompiler(PortfolioRun):
        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            argv = args[0]
            assert isinstance(argv, list)
            if argv[0] == "/usr/bin/cc":
                self.argv.append(argv)
                raise subprocess.TimeoutExpired(argv, 1)
            return super().__call__(*args, **kwargs)  # type: ignore[arg-type]

    runner = UnavailableCompiler()
    inputs["run"] = runner
    receipt = evaluate_portfolio(rung="r2", **inputs)  # type: ignore[arg-type]
    assert receipt["measurements"] == []
    assert (inputs["output"] / "phase-compile-process/process.json").exists()
    failure = json.loads(
        (inputs["output"] / receipt["failures"][0]["path"]).read_text()
    )
    assert len(failure["process_sha256"]) == 64


def test_r2_unavailable_phase_run_retains_phase_process_without_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)

    class UnavailablePhaseRun(PortfolioRun):
        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            argv = args[0]
            assert isinstance(argv, list)
            if Path(argv[0]).name == "phase-probe":
                self.argv.append(argv)
                raise subprocess.TimeoutExpired(argv, 1)
            return super().__call__(*args, **kwargs)  # type: ignore[arg-type]

    runner = UnavailablePhaseRun()
    inputs["run"] = runner
    receipt = evaluate_portfolio(rung="r2", **inputs)  # type: ignore[arg-type]
    cell = inputs["output"] / "measurements/0000"
    assert (cell / "phase-process/process.json").exists()
    assert (cell / "failure.json").exists()
    assert not (cell / "measurement.json").exists()
    assert receipt["failures"]


def test_manifest_rehashed_but_inconsistent_frozen_diagnostics_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    path = inputs["task_manifest"]
    value = json.loads(path.read_text())
    diagnostic = value["traces"][0]["diagnostics"]
    diagnostic["one_hit_object_fraction"]["numerator"] = 9
    diagnostic["diagnostic_sha256"] = record_sha256(
        diagnostic, "diagnostic_sha256"
    )
    value["traces"][0]["diagnostic_sha256"] = diagnostic["diagnostic_sha256"]
    value["manifest_sha256"] = record_sha256(value, "manifest_sha256")
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    with pytest.raises(ValueError, match="one-hit"):
        evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert runner.argv == []


def test_retained_raw_mutation_aborts_before_following_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)

    class RawMutatingRun(PortfolioRun):
        retained_stdout: list[Path] = []

        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            result = super().__call__(*args, **kwargs)  # type: ignore[arg-type]
            if Path(result.argv[0]).name == "cachesim":
                self.retained_stdout.append(result.stdout_path)
            cachesim_runs = [
                item for item in self.argv if Path(item[0]).name == "cachesim"
            ]
            if len(cachesim_runs) == 2:
                self.retained_stdout[0].write_bytes(b"mutated retained raw\n")
            return result

    runner = RawMutatingRun()
    inputs["run"] = runner
    receipt = evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert len(runner.argv) == 2
    assert len(receipt["measurements"]) == 1
    assert len(receipt["failures"]) == 1
    assert receipt["provenance"]["final_binding_intact"] is False


def test_portfolio_refuses_existing_output_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    output = inputs["output"]
    output.mkdir()
    marker = output / "foreign"
    marker.write_text("preserve\n")
    with pytest.raises(ValueError, match="output.*exist"):
        evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert marker.read_text() == "preserve\n"
    assert runner.argv == []


def test_foreign_output_race_is_preserved_and_stage_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)
    output = inputs["output"]

    class ForeignPublisher(PortfolioRun):
        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            result = super().__call__(*args, **kwargs)  # type: ignore[arg-type]
            if len(self.argv) == 1:
                output.mkdir()
                (output / "foreign").write_text("preserve\n")
            return result

    inputs["run"] = ForeignPublisher()
    with pytest.raises(ValueError, match="publish|replace|exists"):
        evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert (output / "foreign").read_text() == "preserve\n"
    assert not (output / "receipt.json").exists()


def test_foreign_cell_inventory_aborts_before_next_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _runner, _trace_ids = portfolio_inputs(tmp_path, monkeypatch)

    class ForeignCellFile(PortfolioRun):
        def __call__(self, *args: object, **kwargs: object) -> ChildResult:
            result = super().__call__(*args, **kwargs)  # type: ignore[arg-type]
            argv = args[0]
            assert isinstance(argv, list)
            if Path(argv[0]).name == "cachesim":
                result.stdout_path.parent.joinpath("foreign").write_text("foreign\n")
            return result

    runner = ForeignCellFile()
    inputs["run"] = runner
    receipt = evaluate_portfolio(rung="r1", **inputs)  # type: ignore[arg-type]
    assert len(runner.argv) == 1
    assert receipt["measurements"] == []
    assert receipt["provenance"]["final_binding_intact"] is False
    assert len(receipt["failures"]) == 1
