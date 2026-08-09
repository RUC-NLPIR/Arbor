from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_aros_cache_substrate.py"
POLICIES = ("LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize")
REFERENCES = ("Sieve", "S3FIFO")
FRACTIONS = ("0.01", "0.05", "0.1")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def record_sha256(value: dict[str, object], field: str) -> str:
    projected = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(canonical_bytes(projected)).hexdigest()


def write_record(path: Path, value: dict[str, object], field: str) -> Path:
    value[field] = record_sha256(value, field)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    return path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repository: Path, *arguments: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=True,
    )
    return result.stdout if binary else result.stdout.decode().strip()


def load_verifier() -> object:
    specification = importlib.util.spec_from_file_location(
        "verify_aros_cache_substrate", SCRIPT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def inventory(root: Path) -> list[dict[str, object]]:
    result = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "receipt.json":
            continue
        metadata = path.stat()
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "identity": {"device": metadata.st_dev, "inode": metadata.st_ino},
                "mode": metadata.st_mode & 0o777,
                "size_bytes": metadata.st_size,
                "sha256": sha256_file(path),
            }
        )
    return result


def r0_inventory(root: Path) -> list[dict[str, object]]:
    result = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "receipt.json":
            continue
        metadata = path.stat()
        digest = sha256_file(path)
        identity = {"device": metadata.st_dev, "inode": metadata.st_ino}
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "identity": identity,
                "size_bytes": metadata.st_size,
                "sha256": digest,
                "present": True,
                "observed_identity": identity,
                "observed_size_bytes": metadata.st_size,
                "observed_sha256": digest,
                "binding_intact": True,
            }
        )
    return result


def diagnostic(trace_id: str, request_count: int, working_set: int) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "trace_id": trace_id,
        "request_count": request_count,
        "unique_object_count": request_count,
        "working_set_bytes": working_set,
        "one_hit_object_fraction": {
            "numerator": request_count,
            "denominator": request_count,
        },
        "one_hit_request_fraction": {
            "numerator": request_count,
            "denominator": request_count,
        },
        "reuse_distance": {
            "bin_convention": (
                "1-based next_access_vtime distance d; "
                "bin k counts 2^k <= d < 2^(k+1)"
            ),
            "counts": {},
            "no_next_count": request_count,
        },
    }
    value["diagnostic_sha256"] = record_sha256(value, "diagnostic_sha256")
    return value


def trace_record(
    path: Path,
    *,
    trace_id: str,
    split: str,
    organization: str,
    application: str,
    origin: str,
) -> dict[str, object]:
    request_count = 34
    raw = bytearray()
    for index in range(request_count):
        raw.extend((index).to_bytes(4, "little"))
        raw.extend((index + int(origin[0], 16) * 1000).to_bytes(8, "little"))
        raw.extend((100).to_bytes(4, "little"))
        raw.extend((-1).to_bytes(8, "little", signed=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    facts = diagnostic(trace_id, request_count, request_count * 100)
    return {
        "trace_id": trace_id,
        "split": split,
        "organization": organization,
        "application": application,
        "dataset": f"dataset-{trace_id}",
        "provenance_url": "https://example.invalid/cache-data",
        "license_ref": "fixture-license",
        "path": str(path.resolve()),
        "trace_type": "oracleGeneral",
        "origin_sha256": origin * 64,
        "start_request": 0,
        "warmup_seconds": 1,
        "max_requests": request_count,
        "working_set_bytes": request_count * 100,
        "sha256": sha256_file(path),
        "size_bytes": len(raw),
        "diagnostic_sha256": facts["diagnostic_sha256"],
        "diagnostics": facts,
    }


def source_repository(root: Path) -> tuple[Path, str, str, str, str]:
    checkout = root / "libCacheSim"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "Verifier Fixture")
    git(checkout, "config", "user.email", "fixture@example.invalid")
    git(
        checkout,
        "remote",
        "add",
        "origin",
        "https://github.com/1a1a11a/libCacheSim.git",
    )
    (checkout / ".gitignore").write_text("_build/\n")
    for policy in POLICIES:
        source = checkout / f"libCacheSim/cache/eviction/{policy}.c"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"/* {policy} */\n")
    wiring = {
        "libCacheSim/include/libCacheSim/evictionAlgo.h": "/* algorithms */\n",
        "libCacheSim/cache/CMakeLists.txt": "# policies\n",
        "libCacheSim/bin/cachesim/cache_init.h": "/* registry */\n",
        "test/CMakeLists.txt": "# tests\n",
    }
    for relative, raw in wiring.items():
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw)
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "pinned source")
    base = str(git(checkout, "rev-parse", "HEAD"))
    base_tree = str(git(checkout, "rev-parse", "HEAD^{tree}"))
    binary = checkout / "_build/bin/cachesim"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    candidate_files = {
        "libCacheSim/cache/eviction/CandidatePolicy.c": "/* candidate */\n",
        "test/test_CandidatePolicy.c": "/* candidate test */\n",
        "commissioning/cache_policy_contract.json": json.dumps(
            {
                "schema_version": 1,
                "policy": "CandidatePolicy",
                "reference_policy": "Sieve",
                "policy_source": "libCacheSim/cache/eviction/CandidatePolicy.c",
                "object_metadata_bytes": 3,
                "global_metadata_bytes": 24,
                "global_metadata_evidence": [
                    {
                        "source": "libCacheSim/cache/eviction/CandidatePolicy.c",
                        "line": 1,
                        "expression": "sizeof(candidate_state)",
                    }
                ],
                "update_complexity": "amortized O(1)",
            },
            sort_keys=True,
        )
        + "\n",
    }
    additions = {
        "libCacheSim/include/libCacheSim/evictionAlgo.h": (
            "cache_t *CandidatePolicy_init(const common_cache_params_t, const char *);\n"
        ),
        "libCacheSim/cache/CMakeLists.txt": "eviction/CandidatePolicy.c\n",
        "libCacheSim/bin/cachesim/cache_init.h": (
            '{"CandidatePolicy", CandidatePolicy_init},\n'
        ),
        "test/CMakeLists.txt": (
            "add_test_executable(test_CandidatePolicy test_CandidatePolicy.c)\n"
        ),
    }
    for relative, raw in candidate_files.items():
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw)
    for relative, raw in additions.items():
        path = checkout / relative
        path.write_text(path.read_text() + raw)
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "candidate")
    candidate = str(git(checkout, "rev-parse", "HEAD"))
    candidate_tree = str(git(checkout, "rev-parse", "HEAD^{tree}"))
    return checkout, base, base_tree, candidate, candidate_tree


def source_receipt(
    path: Path, checkout: Path, base: str, base_tree: str
) -> Path:
    zero = hashlib.sha256(b"").hexdigest()
    value: dict[str, object] = {
        "schema_version": 1,
        "repository_url": "https://github.com/1a1a11a/libCacheSim.git",
        "commit": base,
        "tree": base_tree,
        "clean": True,
        "commands": [
            {
                "argv": argv,
                "returncode": 0,
                "stdout_sha256": zero,
                "stderr_sha256": zero,
            }
            for argv in (
                [
                    "cmake", "-S", ".", "-B", "_build", "-G", "Ninja",
                    "-DCMAKE_BUILD_TYPE=Release", "-DENABLE_TESTS=ON",
                ],
                ["cmake", "--build", "_build", "-j", "8"],
                ["ctest", "--test-dir", "_build", "--output-on-failure"],
            )
        ],
        "versions": {"cmake": "cmake fixture", "ninja": "ninja fixture"},
        "compilers": {
            "c": {"path": "/usr/bin/cc", "version": "cc fixture"},
            "cxx": {"path": "/usr/bin/c++", "version": "c++ fixture"},
        },
        "interpreter": "Python fixture",
        "platform": "fixture-linux",
        "binary": "_build/bin/cachesim",
        "binary_sha256": sha256_file(checkout / "_build/bin/cachesim"),
    }
    return write_record(path, value, "receipt_sha256")


def artifact_record(root: Path, name: str, raw: bytes) -> dict[str, object]:
    source = root / "sources" / name
    snapshot = root / "artifact_snapshots" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(raw)
    snapshot.write_bytes(raw)
    snapshot.chmod(0o400)
    source_stat = source.stat()
    snapshot_stat = snapshot.stat()
    return {
        "source_path": str(source.resolve()),
        "source_identity": {
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
        },
        "size_bytes": len(raw),
        "sha256": sha256_file(snapshot),
        "snapshot_path": snapshot.relative_to(root).as_posix(),
        "snapshot_identity": {
            "device": snapshot_stat.st_dev,
            "inode": snapshot_stat.st_ino,
        },
        "binding_intact": True,
    }


def r0_receipt(
    root: Path,
    *,
    source: Path,
    checkout: Path,
    base: str,
    base_tree: str,
    candidate: str,
    candidate_tree: str,
    policy: str,
) -> Path:
    output = root / f"r0-{policy}"
    command_root = output / "commands/00-metadata-run"
    command_root.mkdir(parents=True)
    stdout = command_root / "stdout.raw"
    stderr = command_root / "stderr.raw"
    stdout.write_bytes(b"metadata fixture\n")
    stderr.write_bytes(b"")
    command: dict[str, object] = {
        "index": 0,
        "label": "metadata-run",
        "argv": ["metadata-probe", policy],
        "cwd": str(output),
        "timeout_seconds": 10,
        "max_output_bytes": 1024,
        "returncode": 0,
        "wall_ns": 10,
        "cpu_ns": 5,
        "stdout": {
            "path": stdout.relative_to(output).as_posix(),
            "size_bytes": stdout.stat().st_size,
            "sha256": sha256_file(stdout),
            "binding_intact": True,
        },
        "stderr": {
            "path": stderr.relative_to(output).as_posix(),
            "size_bytes": 0,
            "sha256": sha256_file(stderr),
            "binding_intact": True,
        },
    }
    command["command_sha256"] = record_sha256(command, "command_sha256")
    artifacts = {
        "release_cachesim": artifact_record(output, "release_cachesim", b"binary"),
        "release_archive": artifact_record(output, "release_archive", b"archive"),
        "release_cmake_cache": artifact_record(output, "release_cmake_cache", b"cache"),
    }
    probe_files: dict[str, dict[str, object]] = {}
    for name, raw in {
        "fixed_time": b"fixed-time",
        "capacity": b"capacity",
        "metadata": b"metadata",
        "interposer": b"interposer",
    }.items():
        source_path = output / f"probes/{name}.c"
        binary_path = output / f"probes/{name}.bin"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(raw + b"-source")
        binary_path.write_bytes(raw + b"-binary")
        probe_files[name] = {
            "source_path": source_path.relative_to(output).as_posix(),
            "source_sha256": sha256_file(source_path),
            "binary": {
                "path": binary_path.relative_to(output).as_posix(),
                "size_bytes": binary_path.stat().st_size,
                "sha256": sha256_file(binary_path),
                "binding_intact": True,
            },
        }
    probes = {
        "fixed_time": {
            **probe_files["fixed_time"],
            "environment": "LD_PRELOAD=fixed-time",
        },
        "release_cmake_cache_sha256": artifacts["release_cmake_cache"]["sha256"],
        "include_flags": ["-Iinclude"],
        "link_flags": ["-lm"],
        "capacity": probe_files["capacity"],
        "metadata": {
            **probe_files["metadata"],
            "interposer_source_path": probe_files["interposer"]["source_path"],
            "interposer_source_sha256": probe_files["interposer"]["source_sha256"],
            "interposer_binary": probe_files["interposer"]["binary"],
            "accounting_scope": "process_wide_ld_preload",
        },
    }
    empty_diff = hashlib.sha256(
        git(checkout, "diff", "--binary", "--full-index", "--no-renames", f"{base}..{candidate}", binary=True)
    ).hexdigest()
    changed = str(git(checkout, "diff", "--name-only", f"{base}..{candidate}"))
    changed_paths = sorted(item for item in changed.splitlines() if item)
    baseline = candidate == base
    policy_source = checkout / f"libCacheSim/cache/eviction/{policy}.c"
    contract = checkout / "commissioning/cache_policy_contract.json"
    candidate_test = checkout / f"test/test_{policy}.c"
    hashes = {
        item: sha256_file(checkout / item)
        for item in changed_paths
    }
    evaluator = {
        key: hashlib.sha256(key.encode()).hexdigest()
        for key in (
            "evaluate_sha256", "scope_sha256", "evidence_sha256",
            "r0_probes_sha256", "cachesim_sha256", "linux_subreaper_sha256",
        )
    }
    value: dict[str, object] = {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": "r0",
        "source_receipt_path": str(source.resolve()),
        "source_receipt_sha256": json.loads(source.read_text())["receipt_sha256"],
        "source_receipt_file_sha256": sha256_file(source),
        "repository_url": "https://github.com/1a1a11a/libCacheSim.git",
        "base_commit": base,
        "base_tree": base_tree,
        "candidate_commit": candidate,
        "candidate_tree": candidate_tree,
        "candidate_diff_sha256": empty_diff,
        "changed_path_sha256": hashes,
        "policy": policy,
        "policy_source_sha256": sha256_file(policy_source),
        "candidate_test_sha256": None if baseline else sha256_file(candidate_test),
        "contract_sha256": None if baseline else sha256_file(contract),
        "binary": "_build-release/bin/cachesim",
        "binary_sha256": artifacts["release_cachesim"]["sha256"],
        "binary_post_run_sha256": artifacts["release_cachesim"]["sha256"],
        "checks": {
            "source_binding": True,
            "evidence_binding": True,
            "build": True,
            "full_tests": True,
            "candidate_test": None if baseline else True,
            "sanitizer": True,
            "deterministic": True,
            "capacity": True,
            "metadata_probe": True,
        },
        "scope": {
            "allowed_paths": True,
            "baseline_unchanged": True,
            "additive_wiring_only": True,
            "contract_bound": None if baseline else True,
            "changed_paths": changed_paths,
            "diff_sha256": empty_diff,
        },
        "declared_metadata": None if baseline else json.loads(contract.read_text()),
        "measured_metadata": {
            "bytes_per_object": "3",
            "global_bytes": 24,
            "measurement_sha256": sha256_file(stdout),
            "within_budget": None,
        },
        "complexity_audit": "pending_independent_review",
        "synthetic_trace": {
            "generator": "fixture",
            "path": "synthetic.oracleGeneral",
            "sha256": hashlib.sha256(b"synthetic").hexdigest(),
            "cache_fraction": "0.1",
            "cache_size_bytes": 100,
        },
        "simulations": [
            {
                "request_count": 32,
                "object_miss_ratio": "0.5",
                "byte_miss_ratio": "0.5",
                "simulator_throughput_mqps": "20.0",
            }
        ] * 2,
        "simulator_result": {
            "path": command["stdout"]["path"],
            "size_bytes": command["stdout"]["size_bytes"],
            "sha256": command["stdout"]["sha256"],
        },
        "capacity_measurement": {
            "cache_size_bytes": 100,
            "maximum_occupied_bytes": 100,
            "capacity_conserved": True,
        },
        "commands": [command],
        "artifact_snapshots": artifacts,
        "probes": probes,
        "evaluator": evaluator,
        "host": {"platform": "fixture", "machine": "x86_64", "python": "3.10"},
        "timings": {"total_wall_ns": 10, "command_wall_ns": 10, "command_cpu_ns": 5},
        "errors": [],
        "evidence_inventory": [],
        "unexpected_stage_entries": [],
    }
    value["evidence_inventory"] = r0_inventory(output)
    return write_record(output / "receipt.json", value, "receipt_sha256")


def raw_result(trace: Path, policy: str, throughput: str) -> bytes:
    return (
        f"{trace} {policy} cache size  1.00KiB, 32 req, miss ratio 0.5000, "
        f"byte miss ratio 0.5000, throughput {throughput}.00 MQPS\n"
    ).encode()


def process_record(
    root: Path, directory: Path, argv: list[str], stdout_raw: bytes, *, label: str
) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    stdout = directory / "stdout.raw"
    stderr = directory / "stderr.raw"
    stdout.write_bytes(stdout_raw)
    stderr.write_bytes(b"")
    value: dict[str, object] = {
        "label": label,
        "argv": argv,
        "timeout_seconds": 3600,
        "max_output_bytes": 1024 * 1024,
        "returncode": 0,
        "wall_ns": 6400,
        "cpu_ns": 3200,
        "stdout": {
            "path": stdout.relative_to(root).as_posix(),
            "size_bytes": stdout.stat().st_size,
            "sha256": sha256_file(stdout),
            "identity": {"device": stdout.stat().st_dev, "inode": stdout.stat().st_ino},
        },
        "stderr": {
            "path": stderr.relative_to(root).as_posix(),
            "size_bytes": 0,
            "sha256": sha256_file(stderr),
            "identity": {"device": stderr.stat().st_dev, "inode": stderr.stat().st_ino},
        },
    }
    value["process_sha256"] = record_sha256(value, "process_sha256")
    return value


def phase_record(
    root: Path,
    cell: Path,
    trace: dict[str, object],
    policy: str,
    fraction: str,
    cache_size: int,
) -> dict[str, object]:
    bins = [
        {
            "index": index,
            "requests": 2,
            "object_misses": 1,
            "request_bytes": 200,
            "byte_misses": 100,
        }
        for index in range(16)
    ]
    phase_raw = "".join(
        f"phase={item['index']} requests=2 object_misses=1 "
        "request_bytes=200 byte_misses=100\n"
        for item in bins
    )
    phase_raw += (
        "total requests=32 object_misses=16 object_miss_ratio=0.5000 "
        "request_bytes=3200 byte_misses=1600 byte_miss_ratio=0.5000\n"
    )
    process = process_record(
        root,
        cell / "phase-process",
        ["phase-probe", str(trace["path"]), policy],
        phase_raw.encode(),
        label="phase-probe",
    )
    write_record(cell / "phase-process/process.json", process, "process_sha256")
    value: dict[str, object] = {
        "schema_version": 1,
        "trace_id": trace["trace_id"],
        "trace_sha256": trace["sha256"],
        "frozen_trace_diagnostic_sha256": trace["diagnostic_sha256"],
        "policy": policy,
        "cache_fraction": fraction,
        "cache_size_bytes": cache_size,
        "request_count": 32,
        "object_misses": 16,
        "request_bytes": 3200,
        "byte_misses": 1600,
        "bins": bins,
        "process": process,
    }
    return write_record(cell / "phase.json", value, "phase_sha256") and value


def portfolio_receipt(
    root: Path,
    *,
    rung: str,
    task_root: Path,
    manifest_path: Path,
    manifest: dict[str, object],
    source: Path,
    r0_path: Path,
    checkout: Path,
    candidate: str,
    candidate_tree: str,
    policy: str,
    throughput: str,
) -> Path:
    root.mkdir(parents=True)
    r0 = json.loads(r0_path.read_text())
    selected = list(manifest["traces"])
    if rung == "r1":
        selected = [item for item in selected if item["split"] == "dev"][:3]
    evaluator = {
        key: hashlib.sha256(key.encode()).hexdigest()
        for key in (
            "evaluate_sha256", "scope_sha256", "evidence_sha256",
            "r0_probes_sha256", "cachesim_sha256", "linux_subreaper_sha256",
            "portfolio_sha256", "portfolio_evidence_sha256", "oracle_sha256",
            "records_sha256", "diagnostics_sha256", "source_lock_sha256",
            "run_aros_cache_eval_sha256",
        )
    }
    scientific = {
        "fixed_time_interposer": hashlib.sha256(b"fixed").hexdigest(),
        "release_archive": hashlib.sha256(b"archive").hexdigest(),
        "release_cmake_cache": hashlib.sha256(b"cache").hexdigest(),
        "header:libCacheSim/include/libCacheSim.h": hashlib.sha256(b"header").hexdigest(),
        "header:libCacheSim/bin/cachesim/cache_init.h": hashlib.sha256(b"init").hexdigest(),
    }
    measurements = []
    selected_cells = []
    for trace in selected:
        for fraction in FRACTIONS:
            index = len(selected_cells)
            cache_size = int(int(trace["working_set_bytes"]) * float(fraction))
            cell_summary = {
                "index": index,
                "trace_id": trace["trace_id"],
                "split": trace["split"],
                "cache_fraction": fraction,
                "cache_size_bytes": cache_size,
            }
            selected_cells.append(cell_summary)
            cell = root / f"measurements/{index:04d}"
            argv = ["cachesim", str(trace["path"]), "oracleGeneral", policy, str(cache_size)]
            raw = raw_result(Path(str(trace["path"])), policy, throughput)
            process = process_record(root, cell, argv, raw, label="cachesim")
            write_record(cell / "process.json", process, "process_sha256")
            request: dict[str, object] = {
                "schema_version": 1,
                "rung": rung,
                "cell_index": index,
                "trace_id": trace["trace_id"],
                "split": trace["split"],
                "trace_sha256": trace["sha256"],
                "trace_diagnostic_sha256": trace["diagnostic_sha256"],
                "policy": policy,
                "cache_fraction": fraction,
                "cache_size_bytes": cache_size,
                "expected_measured_requests": 32,
                "expected_measured_request_bytes": 3200,
                "argv": argv,
            }
            write_record(cell / "request.json", request, "request_sha256")
            simulator = cell / "simulator.cachesim"
            simulator.write_bytes(raw)
            measurement: dict[str, object] = {
                "schema_version": 1,
                "receipt_version": 1,
                "rung": rung,
                "split": trace["split"],
                "trace_id": trace["trace_id"],
                "policy": policy,
                "cache_fraction": fraction,
                "cache_size_bytes": cache_size,
                "request_count": 32,
                "object_miss_ratio": "0.5",
                "byte_miss_ratio": "0.5",
                "simulator_throughput_mqps": throughput,
                "cpu_ns_per_request": "100",
                "metadata_bytes_per_object": "3",
                "global_metadata_bytes": 24,
                "metadata_measurement_sha256": r0["measured_metadata"]["measurement_sha256"],
                "trace_sha256": trace["sha256"],
                "trace_diagnostic_sha256": trace["diagnostic_sha256"],
                "source_receipt_sha256": json.loads(source.read_text())["receipt_sha256"],
                "r0_receipt_sha256": r0["receipt_sha256"],
                "candidate_commit": candidate,
                "candidate_tree": candidate_tree,
                "binary_sha256": r0["binary_sha256"],
                "evaluator": evaluator,
                "evaluator_snapshots": evaluator,
                "scientific_inputs": scientific,
                "argv": argv,
                "process": process,
                "simulator_output": {
                    "requested_path": f"simulator-side-effects/{index:04d}.cachesim",
                    "path": simulator.relative_to(root).as_posix(),
                    "identity": {
                        "device": simulator.stat().st_dev,
                        "inode": simulator.stat().st_ino,
                    },
                    "size_bytes": simulator.stat().st_size,
                    "sha256": sha256_file(simulator),
                },
            }
            if rung in {"r2", "r3"}:
                phase = phase_record(root, cell, trace, policy, fraction, cache_size)
                measurement["phase_diagnostic"] = phase
                measurement["frozen_trace_diagnostic"] = trace["diagnostics"]
            write_record(cell / "measurement.json", measurement, "measurement_sha256")
            measurements.append(
                {
                    **cell_summary,
                    "path": f"measurements/{index:04d}/measurement.json",
                    "measurement_sha256": measurement["measurement_sha256"],
                }
            )
    execution = root / "execution/cachesim"
    execution.parent.mkdir(parents=True)
    execution.write_bytes(b"binary")
    trace_snapshots = []
    for index, trace in enumerate(selected):
        source_path = Path(trace["path"])
        snapshot = root / f"trace-snapshots/{index:04d}.oracleGeneral"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(source_path.read_bytes())
        snapshot.chmod(0o400)
        audit = write_record(
            root / f"oracle-audits/{index:04d}/diagnostic.json",
            dict(trace["diagnostics"]),
            "diagnostic_sha256",
        )
        source_stat = source_path.stat()
        snapshot_stat = snapshot.stat()
        audit_stat = audit.stat()
        trace_snapshots.append(
            {
                "trace_id": trace["trace_id"],
                "source_path": trace["path"],
                "source_identity": {
                    "device": source_stat.st_dev,
                    "inode": source_stat.st_ino,
                },
                "source_size_bytes": trace["size_bytes"],
                "source_sha256": trace["sha256"],
                "snapshot_path": snapshot.relative_to(root).as_posix(),
                "snapshot_identity": {
                    "device": snapshot_stat.st_dev,
                    "inode": snapshot_stat.st_ino,
                },
                "snapshot_size_bytes": trace["size_bytes"],
                "snapshot_sha256": trace["sha256"],
                "audit_path": audit.relative_to(root).as_posix(),
                "audit_identity": {
                    "device": audit_stat.st_dev,
                    "inode": audit_stat.st_ino,
                },
                "audit_sha256": trace["diagnostic_sha256"],
            }
        )
    evaluator_snapshots = {}
    for key, digest in evaluator.items():
        path = root / f"evaluator/{key}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.encode())
        path.chmod(0o400)
        metadata = path.stat()
        evaluator_snapshots[key] = {
            "path": path.relative_to(root).as_posix(),
            "identity": {"device": metadata.st_dev, "inode": metadata.st_ino},
            "size_bytes": metadata.st_size,
            "sha256": digest,
        }
    scientific_paths = {
        "fixed_time_interposer": ("inputs/fixed", b"fixed"),
        "release_archive": ("inputs/archive", b"archive"),
        "release_cmake_cache": ("inputs/cache", b"cache"),
        "libCacheSim/include/libCacheSim.h": ("inputs/header", b"header"),
        "libCacheSim/bin/cachesim/cache_init.h": ("inputs/init", b"init"),
    }
    for relative, raw in scientific_paths.values():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o400)
    if rung != "r1":
        phase_source = root / "apparatus/phase.c"
        phase_binary = root / "apparatus/phase"
        phase_source.parent.mkdir(parents=True, exist_ok=True)
        phase_source.write_bytes(b"phase")
        phase_binary.write_bytes(b"phase-bin")
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": rung,
        "task_root": str(task_root.resolve()),
        "task_manifest_path": str(manifest_path.resolve()),
        "task_manifest_sha256": manifest["manifest_sha256"],
        "task_manifest_file_sha256": sha256_file(manifest_path),
        "source_receipt_sha256": json.loads(source.read_text())["receipt_sha256"],
        "source_receipt_file_sha256": sha256_file(source),
        "r0_receipt_sha256": r0["receipt_sha256"],
        "r0_receipt_file_sha256": sha256_file(r0_path),
        "candidate_commit": candidate,
        "candidate_tree": candidate_tree,
        "policy": policy,
        "policy_source_sha256": r0["policy_source_sha256"],
        "binary_snapshot_sha256": r0["binary_sha256"],
        "r0_artifact_snapshots": r0["artifact_snapshots"],
        "execution_copy": {
            "path": execution.relative_to(root).as_posix(),
            "identity": {"device": execution.stat().st_dev, "inode": execution.stat().st_ino},
            "mode": execution.stat().st_mode & 0o777,
            "size_bytes": execution.stat().st_size,
            "sha256": sha256_file(execution),
        },
        "phase_probe": None if rung == "r1" else {
            "source_path": "apparatus/phase.c",
            "source_sha256": hashlib.sha256(b"phase").hexdigest(),
            "binary_path": "apparatus/phase",
            "binary_sha256": hashlib.sha256(b"phase-bin").hexdigest(),
            "compile_process_sha256": hashlib.sha256(b"compile").hexdigest(),
            "release_archive_sha256": r0["artifact_snapshots"]["release_archive"]["sha256"],
            "release_cmake_cache_sha256": r0["artifact_snapshots"]["release_cmake_cache"]["sha256"],
            "compiler_path": "/usr/bin/cc",
            "compiler_resolved_path": "/usr/bin/cc",
            "compiler_sha256": hashlib.sha256(b"cc").hexdigest(),
            "include_flags": ["-Iinclude"],
            "link_flags": ["-lm"],
        },
        "frozen_trace_diagnostics": [] if rung == "r1" else [
            {
                "trace_id": trace["trace_id"],
                "diagnostic_sha256": trace["diagnostic_sha256"],
                "diagnostics": trace["diagnostics"],
            }
            for trace in selected
        ],
        "trace_snapshots": trace_snapshots,
        "evaluator": evaluator,
        "evaluator_snapshots": evaluator_snapshots,
        "scientific_inputs": {
            "fixed_time_interposer": {"path": "inputs/fixed", "sha256": scientific["fixed_time_interposer"]},
            "release_archive": {"path": "inputs/archive", "sha256": scientific["release_archive"]},
            "release_cmake_cache": {"path": "inputs/cache", "sha256": scientific["release_cmake_cache"]},
            "headers": {
                "libCacheSim/include/libCacheSim.h": {"path": "inputs/header", "sha256": scientific["header:libCacheSim/include/libCacheSim.h"]},
                "libCacheSim/bin/cachesim/cache_init.h": {"path": "inputs/init", "sha256": scientific["header:libCacheSim/bin/cachesim/cache_init.h"]},
            },
        },
        "host": {"platform": "fixture", "machine": "x86_64", "python": "3.10"},
        "selected_cells": selected_cells,
        "measurements": measurements,
        "measurement_hashes": [item["measurement_sha256"] for item in measurements],
        "failures": [],
        "failure_hashes": [],
        "provenance": {"final_binding_intact": True},
        "evidence_inventory": [],
        "timings": {"total_wall_ns": 100},
    }
    receipt["evidence_inventory"] = inventory(root)
    return write_record(root / "receipt.json", receipt, "receipt_sha256")


def calibration_record(
    path: Path,
    *,
    manifest: dict[str, object],
    source: Path,
    r0s: dict[str, Path],
    r2s: list[Path],
) -> Path:
    loaded_r0 = {policy: json.loads(receipt.read_text()) for policy, receipt in r0s.items()}
    loaded_r2 = [json.loads(receipt.read_text()) for receipt in r2s]
    traces = manifest["traces"]
    references: dict[str, object] = {}
    comparisons: dict[str, object] = {}
    for policy in POLICIES:
        policy_receipts = [item for item in loaded_r2 if item["policy"] == policy]
        policy_comparisons: dict[str, object] = {}
        reference: dict[str, object] | None = None
        if policy in REFERENCES:
            r0 = loaded_r0[policy]
            command = r0["commands"][0]
            probe = r0["probes"]["metadata"]
            reference = {
                "metadata": {
                    "bytes_per_object": "3",
                    "global_bytes": 24,
                    "measurement_sha256": r0["measured_metadata"]["measurement_sha256"],
                    "probe_evidence": {
                        "r0_receipt_sha256": r0["receipt_sha256"],
                        "metadata_command_sha256": command["command_sha256"],
                        "stdout_sha256": command["stdout"]["sha256"],
                        "metadata_measurement_sha256": r0["measured_metadata"]["measurement_sha256"],
                        "metadata_probe_source_sha256": probe["source_sha256"],
                        "metadata_probe_binary_sha256": probe["binary"]["sha256"],
                        "metadata_interposer_source_sha256": probe["interposer_source_sha256"],
                        "metadata_interposer_binary_sha256": probe["interposer_binary"]["sha256"],
                    },
                    "independent_audit": "pending_independent_review",
                }
            }
        for trace in traces:
            comparison_cells: dict[str, object] = {}
            reference_cells: dict[str, object] = {}
            for fraction in FRACTIONS:
                summaries = []
                for receipt in policy_receipts:
                    summary = next(
                        item for item in receipt["measurements"]
                        if item["trace_id"] == trace["trace_id"]
                        and item["cache_fraction"] == fraction
                    )
                    measurement = json.loads(
                        (Path(receipt["task_manifest_path"]).parents[2] / "unused").read_text()
                    ) if False else json.loads(
                        (Path(r2s[loaded_r2.index(receipt)]).parent / summary["path"]).read_text()
                    )
                    summaries.append((receipt, measurement))
                hashes = sorted(item[1]["measurement_sha256"] for item in summaries)
                receipt_hashes = sorted(item[0]["receipt_sha256"] for item in summaries)
                phases = sorted(
                    [
                        {
                            "phase_sha256": item[1]["phase_diagnostic"]["phase_sha256"],
                            "request_count": 32,
                            "object_misses": 16,
                            "request_bytes": 3200,
                            "byte_misses": 1600,
                            "bins": item[1]["phase_diagnostic"]["bins"],
                        }
                        for item in summaries
                    ],
                    key=lambda item: item["phase_sha256"],
                )
                comparison_cells[fraction] = {
                    "repetitions": len(summaries),
                    "input_receipt_sha256s": receipt_hashes,
                    "measurement_sha256s": hashes,
                    "object_miss_ratio_values": ["0.5"] * len(summaries),
                    "byte_miss_ratio_values": ["0.5"] * len(summaries),
                    "phase_values": phases,
                }
                if reference is not None:
                    values = sorted(
                        (item[1]["simulator_throughput_mqps"] for item in summaries),
                        key=int,
                    )
                    median = values[2]
                    reference_cells[fraction] = {
                        "repetitions": 5,
                        "input_receipt_sha256s": receipt_hashes,
                        "measurement_sha256s": hashes,
                        "object_miss_ratio_values": ["0.5"] * 5,
                        "byte_miss_ratio_values": ["0.5"] * 5,
                        "simulator_throughput_mqps_values": values,
                        "cpu_ns_per_request_values": ["100"] * 5,
                        "throughput_median_mqps": median,
                        "throughput_floor_mqps": str(int(median) * 9 // 10),
                    }
            policy_comparisons[trace["trace_id"]] = comparison_cells
            if reference is not None:
                reference[trace["trace_id"]] = reference_cells
        comparisons[policy] = policy_comparisons
        if reference is not None:
            references[policy] = reference
    transfer: dict[str, object] = {}
    for policy in REFERENCES:
        projection: dict[str, object] = {"metadata": references[policy]["metadata"]}
        for fraction in FRACTIONS:
            cells = []
            for trace in sorted(traces, key=lambda item: item["trace_id"]):
                cell = references[policy][trace["trace_id"]][fraction]
                cells.append(
                    {
                        "trace_id": trace["trace_id"],
                        "reference_cell_sha256": hashlib.sha256(canonical_bytes(cell)).hexdigest(),
                        "input_receipt_sha256s": cell["input_receipt_sha256s"],
                        "measurement_sha256s": cell["measurement_sha256s"],
                        "throughput_median_mqps": cell["throughput_median_mqps"],
                    }
                )
            minimum = min(int(item["throughput_median_mqps"]) for item in cells)
            projection[fraction] = {
                "derivation": "0.90 * minimum(source throughput_median_mqps)",
                "source_cells": cells,
                "minimum_throughput_median_mqps": str(minimum),
                "throughput_floor_mqps": str(minimum * 9 // 10),
            }
        transfer[policy] = projection
    first = loaded_r2[0]
    value: dict[str, object] = {
        "schema_version": 1,
        "task_manifest_sha256": manifest["manifest_sha256"],
        "source_receipt_sha256": json.loads(source.read_text())["receipt_sha256"],
        "source_commit": manifest["source_commit"],
        "binary_sha256": first["binary_snapshot_sha256"],
        "evaluator_sha256s": first["evaluator"],
        "scientific_input_sha256s": {
            "fixed_time_interposer": first["scientific_inputs"]["fixed_time_interposer"]["sha256"],
            "release_archive": first["scientific_inputs"]["release_archive"]["sha256"],
            "release_cmake_cache": first["scientific_inputs"]["release_cmake_cache"]["sha256"],
            **{
                f"header:{name}": item["sha256"]
                for name, item in first["scientific_inputs"]["headers"].items()
            },
        },
        "host_fingerprint": first["host"],
        "repetitions": 5,
        "cache_fractions": list(FRACTIONS),
        "references": references,
        "transfer_constraints": transfer,
        "comparisons": comparisons,
        "r0_receipt_sha256s": {
            policy: loaded_r0[policy]["receipt_sha256"] for policy in POLICIES
        },
        "input_receipt_sha256s": sorted(item["receipt_sha256"] for item in loaded_r2),
    }
    write_record(path, value, "calibration_sha256")
    path.chmod(0o400)
    return path


def refresh_record(path: Path, field: str) -> dict[str, object]:
    value = json.loads(path.read_text())
    write_record(path, value, field)
    return value


class RetainedFixture:
    def __init__(self, root: Path, module: object) -> None:
        self.root = root
        self.module = module
        self.index = root / "retained/index.json"
        self.paths: dict[str, Path] = {}


def valid_retained_substrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RetainedFixture:
    module = load_verifier()
    fixture = RetainedFixture(tmp_path, module)
    checkout, base, base_tree, candidate, candidate_tree = source_repository(tmp_path)
    monkeypatch.setattr(module, "SOURCE_COMMIT", base)
    monkeypatch.setattr(module, "SOURCE_TREE", base_tree)
    authority_root = tmp_path / "uid-authority"
    authority_root.mkdir(mode=0o700)
    monkeypatch.setattr(module, "_host_authority_root", lambda: authority_root)
    source = source_receipt(tmp_path / "host/source.json", checkout, base, base_tree)

    data = tmp_path / "data"
    public = [
        trace_record(data / "dev-a.bin", trace_id="dev-a", split="dev", organization="Meta", application="kv", origin="1"),
        trace_record(data / "dev-b.bin", trace_id="dev-b", split="dev", organization="Twitter", application="kv", origin="2"),
        trace_record(data / "dev-c.bin", trace_id="dev-c", split="dev", organization="Meta", application="cdn", origin="3"),
        trace_record(data / "visible-a.bin", trace_id="visible-a", split="visible", organization="Twitter", application="search", origin="4"),
    ]
    private = trace_record(
        tmp_path / "host/private/r3.bin",
        trace_id="private-r3",
        split="r3",
        organization="Tencent",
        application="photo",
        origin="5",
    )
    host_manifest_value: dict[str, object] = {
        "schema_version": 1,
        "source_commit": base,
        "cache_fractions": [0.01, 0.05, 0.10],
        "traces": [private],
    }
    host_manifest = write_record(
        tmp_path / "host/sealed/r3.json", host_manifest_value, "manifest_sha256"
    )
    task_root = tmp_path / "task"
    task_root.mkdir()
    task_manifest_value: dict[str, object] = {
        "schema_version": 1,
        "source_commit": base,
        "cache_fractions": [0.01, 0.05, 0.10],
        "traces": public,
        "r3_commitment_sha256": host_manifest_value["manifest_sha256"],
    }
    task_manifest = write_record(
        task_root / "manifests/task.json", task_manifest_value, "manifest_sha256"
    )
    r0s = {
        policy: r0_receipt(
            tmp_path / "host",
            source=source,
            checkout=checkout,
            base=base,
            base_tree=base_tree,
            candidate=base,
            candidate_tree=base_tree,
            policy=policy,
        )
        for policy in POLICIES
    }
    candidate_r0 = r0_receipt(
        tmp_path / "host/candidate",
        source=source,
        checkout=checkout,
        base=base,
        base_tree=base_tree,
        candidate=candidate,
        candidate_tree=candidate_tree,
        policy="CandidatePolicy",
    )
    r1 = portfolio_receipt(
        tmp_path / "host/r1-sieve",
        rung="r1",
        task_root=task_root,
        manifest_path=task_manifest,
        manifest=task_manifest_value,
        source=source,
        r0_path=r0s["Sieve"],
        checkout=checkout,
        candidate=base,
        candidate_tree=base_tree,
        policy="Sieve",
        throughput="20",
    )
    r2s = []
    for policy in POLICIES:
        throughputs = ("18", "19", "20", "21", "22") if policy in REFERENCES else ("20",)
        for repetition, throughput in enumerate(throughputs):
            r2s.append(
                portfolio_receipt(
                    tmp_path / f"host/r2-{policy}-{repetition}",
                    rung="r2",
                    task_root=task_root,
                    manifest_path=task_manifest,
                    manifest=task_manifest_value,
                    source=source,
                    r0_path=r0s[policy],
                    checkout=checkout,
                    candidate=base,
                    candidate_tree=base_tree,
                    policy=policy,
                    throughput=throughput,
                )
            )
    calibration = calibration_record(
        tmp_path / "host/calibration.json",
        manifest=task_manifest_value,
        source=source,
        r0s=r0s,
        r2s=r2s,
    )
    calibration_value = json.loads(calibration.read_text())
    candidate_r2 = portfolio_receipt(
        tmp_path / "host/candidate-r2",
        rung="r2",
        task_root=task_root,
        manifest_path=task_manifest,
        manifest=task_manifest_value,
        source=source,
        r0_path=candidate_r0,
        checkout=checkout,
        candidate=candidate,
        candidate_tree=candidate_tree,
        policy="CandidatePolicy",
        throughput="20",
    )
    candidate_r2_value = json.loads(candidate_r2.read_text())
    refs = {
        "knowledge/claims/C-0001/claim.md": "scoped claim\n",
        "experiments/confirmation/preregistration.md": "preregistered\n",
        "reviews/RV-0001/report.md": "review\n",
        "reviews/RV-0001/principal-response.md": "response\n",
        "reviews/RV-0001/reproduction.json": json.dumps(
            {
                "schema_version": 1,
                "r2_receipt_path": str(candidate_r2.resolve()),
                "r2_receipt_sha256": candidate_r2_value["receipt_sha256"],
            },
            sort_keys=True,
        )
        + "\n",
    }
    for relative, raw in refs.items():
        path = task_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw)
    git(task_root, "init", "-q")
    git(task_root, "config", "user.name", "Frozen Fixture")
    git(task_root, "config", "user.email", "frozen@example.invalid")
    git(task_root, "add", ".")
    git(task_root, "commit", "-qm", "frozen package")
    frozen_commit = str(git(task_root, "rev-parse", "HEAD"))
    candidate_diff = hashlib.sha256(
        git(checkout, "diff", "--binary", "--full-index", "--no-renames", f"{base}..{candidate}", binary=True)
    ).hexdigest()
    candidate_r0_value = json.loads(candidate_r0.read_text())
    package: dict[str, object] = {
        "schema_version": 1,
        "project": str(task_root.resolve()),
        "frozen_commit": frozen_commit,
        "candidate_commit": candidate,
        "policy": "CandidatePolicy",
        "candidate_diff_sha256": candidate_diff,
        "policy_contract_sha256": candidate_r0_value["contract_sha256"],
        "claim_ref": "knowledge/claims/C-0001/claim.md",
        "preregistration_ref": "experiments/confirmation/preregistration.md",
        "review_ref": "reviews/RV-0001/report.md",
        "principal_response_ref": "reviews/RV-0001/principal-response.md",
        "reproduction_ref": "reviews/RV-0001/reproduction.json",
        "r0_receipt_sha256": candidate_r0_value["receipt_sha256"],
        "r2_receipt_sha256": candidate_r2_value["receipt_sha256"],
        "calibration_sha256": calibration_value["calibration_sha256"],
        "r3_commitment_sha256": host_manifest_value["manifest_sha256"],
    }
    package_path = tmp_path / "host/frozen-package.json"
    package_path.write_text(json.dumps(package, sort_keys=True, indent=2) + "\n")
    authority_id = hashlib.sha256(
        (
            frozen_commit
            + str(package["r3_commitment_sha256"])
            + candidate
            + "/CandidatePolicy"
        ).encode()
    ).hexdigest()
    ledger_path = authority_root / f"r3-{authority_id}.consumed.json"
    final_path = authority_root / f"r3-{authority_id}.receipt.json"
    ref_hashes = {
        field: hashlib.sha256((task_root / str(package[field])).read_bytes()).hexdigest()
        for field in (
            "claim_ref", "preregistration_ref", "review_ref",
            "principal_response_ref", "reproduction_ref",
        )
    }
    ledger: dict[str, object] = {
        "schema_version": 1,
        "state": "consumed",
        "authority_id": authority_id,
        "final_receipt_path": str(final_path),
        "requested_at_unix_ns": 2_000_000_000_000_000_000,
        "frozen_package_file_sha256": sha256_file(package_path),
        "frozen_commit": frozen_commit,
        "frozen_tree": str(git(task_root, "rev-parse", "HEAD^{tree}")),
        "candidate_commit": candidate,
        "candidate_tree": candidate_tree,
        "policy": "CandidatePolicy",
        "candidate_diff_sha256": candidate_diff,
        "policy_contract_sha256": candidate_r0_value["contract_sha256"],
        "git_ref_sha256s": ref_hashes,
        "host_r3_manifest_sha256": sha256_file(host_manifest),
        "r3_commitment_sha256": host_manifest_value["manifest_sha256"],
        "calibration_sha256": calibration_value["calibration_sha256"],
        "calibration_file_sha256": sha256_file(calibration),
        "source_receipt_sha256": json.loads(source.read_text())["receipt_sha256"],
        "source_receipt_file_sha256": sha256_file(source),
        "candidate_r0_receipt_sha256": candidate_r0_value["receipt_sha256"],
        "candidate_r0_file_sha256": sha256_file(candidate_r0),
        "r2_receipt_sha256": candidate_r2_value["receipt_sha256"],
        "r2_receipt_file_sha256": sha256_file(candidate_r2),
        "binary_sha256": candidate_r0_value["binary_sha256"],
        "r3_evaluator_sha256s": {"seal_sha256": hashlib.sha256(b"seal").hexdigest()},
        "portfolio_evaluator_sha256s": candidate_r2_value["evaluator"],
        "trace_sha256s": [private["sha256"]],
        "private_snapshot": {
            "root": str((tmp_path / "host/private-snapshot").resolve()),
            "manifest_sha256": sha256_file(host_manifest),
            "trace_sha256s": [private["sha256"]],
            "source_receipt_sha256": sha256_file(source),
            "r0_receipt_sha256": sha256_file(candidate_r0),
            "r0_artifact_sha256s": {
                name: item["sha256"] for name, item in candidate_r0_value["artifact_snapshots"].items()
            },
            "r0_evidence_sha256s": {},
        },
    }
    write_record(ledger_path, ledger, "ledger_sha256")
    ledger_path.chmod(0o600)
    os.utime(ledger_path, ns=(1, 1))
    r3_portfolio = portfolio_receipt(
        tmp_path / "host/r3-result",
        rung="r3",
        task_root=task_root,
        manifest_path=host_manifest,
        manifest=host_manifest_value,
        source=source,
        r0_path=candidate_r0,
        checkout=checkout,
        candidate=candidate,
        candidate_tree=candidate_tree,
        policy="CandidatePolicy",
        throughput="20",
    )
    r3_portfolio_value = json.loads(r3_portfolio.read_text())
    r3_measurements = []
    constraints = []
    for summary in r3_portfolio_value["measurements"]:
        measurement = json.loads((r3_portfolio.parent / summary["path"]).read_text())
        pareto_keys = (
            "rung", "split", "trace_id", "policy", "cache_fraction",
            "cache_size_bytes", "request_count", "object_miss_ratio",
            "byte_miss_ratio", "simulator_throughput_mqps",
            "cpu_ns_per_request", "metadata_bytes_per_object",
            "global_metadata_bytes", "metadata_measurement_sha256",
        )
        r3_measurements.append(
            {
                "cell_index": summary["index"],
                "path": summary["path"],
                "measurement_sha256": summary["measurement_sha256"],
                "pareto": {key: measurement[key] for key in pareto_keys},
            }
        )
        constraints.append(
            {
                "cell_index": summary["index"],
                "measurement_sha256": summary["measurement_sha256"],
                "facts": {
                    "throughput": True,
                    "metadata_independent_audit": "pending_independent_review",
                },
            }
        )
    ledger_value = json.loads(ledger_path.read_text())
    final: dict[str, object] = {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": "r3",
        "state": "measured",
        "authority_id": authority_id,
        "final_receipt_path": str(final_path),
        "frozen_commit": frozen_commit,
        "candidate_commit": candidate,
        "candidate_tree": candidate_tree,
        "policy": "CandidatePolicy",
        "output_path": str(r3_portfolio.parent),
        "ledger_path": str(ledger_path),
        "ledger_sha256": ledger_value["ledger_sha256"],
        "ledger_intended_sha256": sha256_file(ledger_path),
        "ledger_file_sha256": sha256_file(ledger_path),
        "ledger_size_bytes": ledger_path.stat().st_size,
        "r3_commitment_sha256": host_manifest_value["manifest_sha256"],
        "host_r3_manifest_sha256": sha256_file(host_manifest),
        "calibration_sha256": calibration_value["calibration_sha256"],
        "r3_evaluator_sha256s": ledger["r3_evaluator_sha256s"],
        "source_receipt_sha256": json.loads(source.read_text())["receipt_sha256"],
        "r0_receipt_sha256": candidate_r0_value["receipt_sha256"],
        "r2_receipt_sha256": candidate_r2_value["receipt_sha256"],
        "started_at_unix_ns": ledger["requested_at_unix_ns"] + 1,
        "ended_at_unix_ns": ledger["requested_at_unix_ns"] + 2,
        "portfolio_receipt_path": "receipt.json",
        "portfolio_receipt_sha256": r3_portfolio_value["receipt_sha256"],
        "measurements": r3_measurements,
        "failures": [],
        "constraints": constraints,
    }
    write_record(final_path, final, "receipt_sha256")
    index: dict[str, object] = {
        "schema_version": 1,
        "checkout": str(checkout.resolve()),
        "task_root": str(task_root.resolve()),
        "source_receipt": str(source.resolve()),
        "task_manifest": str(task_manifest.resolve()),
        "host_r3_manifest": str(host_manifest.resolve()),
        "r0_receipts": [str(r0s[policy].resolve()) for policy in POLICIES],
        "r1_receipts": [str(r1.resolve())],
        "r2_receipts": [str(item.resolve()) for item in r2s],
        "calibration": str(calibration.resolve()),
        "calibration_sha256": calibration_value["calibration_sha256"],
        "r3": {
            "frozen_package": str(package_path.resolve()),
            "candidate_r0_receipt": str(candidate_r0.resolve()),
            "candidate_r2_receipt": str(candidate_r2.resolve()),
            "ledger": str(ledger_path.resolve()),
            "receipt": str(final_path.resolve()),
        },
    }
    fixture.index.parent.mkdir(parents=True)
    fixture.index.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
    fixture.paths = {
        "source_receipt": source,
        "binary": checkout / "_build/bin/cachesim",
        "task_manifest": task_manifest,
        "trace_bytes": Path(public[0]["path"]),
        "r3_commitment": task_manifest,
        "measurement": r2s[0].parent / json.loads(r2s[0].read_text())["measurements"][0]["path"],
        "raw_stdout": r2s[0].parent / "measurements/0000/stdout.raw",
        "calibration": calibration,
        "frozen_commit": package_path,
        "candidate_diff": package_path,
        "ledger": ledger_path,
        "r3_receipt": final_path,
    }
    return fixture


def mutate(fixture: RetainedFixture, target: str) -> None:
    path = fixture.paths[target]
    if target in {"binary", "trace_bytes", "raw_stdout"}:
        path.write_bytes(path.read_bytes() + b"tamper")
        return
    value = json.loads(path.read_text())
    if target == "source_receipt":
        value["platform"] = "tampered"
    elif target == "task_manifest":
        value["traces"][0]["organization"] = "tampered"
    elif target == "r3_commitment":
        value["r3_commitment_sha256"] = "0" * 64
    elif target == "measurement":
        value["object_miss_ratio"] = "0.4"
    elif target == "calibration":
        value["references"]["Sieve"]["dev-a"]["0.01"]["throughput_median_mqps"] = "19"
    elif target == "frozen_commit":
        value["frozen_commit"] = "0" * 40
    elif target == "candidate_diff":
        value["candidate_diff_sha256"] = "0" * 64
    elif target == "ledger":
        value["state"] = "available"
    elif target == "r3_receipt":
        value["state"] = "process_failed"
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(target)
    path.chmod(0o600)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def forbidden_outcome_key(value: object) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"pass", "score", "aggregate", "overall"}:
                return key
            nested = forbidden_outcome_key(item)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = forbidden_outcome_key(item)
            if nested is not None:
                return nested
    return None


def test_verifier_is_standalone_standard_library() -> None:
    tree = ast.parse(SCRIPT.read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = ("commissioning.cache_campaign", "arbor", "src")
    assert not [name for name in imported if name.startswith(forbidden)]


def test_verifier_reports_independent_factual_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = valid_retained_substrate(tmp_path, monkeypatch)
    result = fixture.module.verify(fixture.index)
    assert set(result) == {
        "source",
        "data_boundary",
        "r0",
        "r1",
        "r2",
        "calibration",
        "r3",
        "unresolved_audit",
    }
    assert forbidden_outcome_key(result) is None
    assert result["unresolved_audit"] == [
        "metadata_allocation_coverage",
        "amortized_o1_complexity",
    ]


@pytest.mark.parametrize(
    "target",
    [
        "source_receipt",
        "binary",
        "task_manifest",
        "trace_bytes",
        "r3_commitment",
        "measurement",
        "raw_stdout",
        "calibration",
        "frozen_commit",
        "candidate_diff",
        "ledger",
        "r3_receipt",
    ],
)
def test_verifier_rejects_each_broken_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    fixture = valid_retained_substrate(tmp_path, monkeypatch)
    mutate(fixture, target)
    with pytest.raises(fixture.module.VerificationError):
        fixture.module.verify(fixture.index)


def test_cli_writes_canonical_json_and_uses_exit_two_for_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = valid_retained_substrate(tmp_path, monkeypatch)
    assert fixture.module.main([str(fixture.index)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    decoded = json.loads(captured.out)
    assert captured.out.encode() == canonical_bytes(decoded) + b"\n"
    fixture.index.write_text('{"schema_version":1,"schema_version":1}\n')
    assert fixture.module.main([str(fixture.index)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: substrate verification failed\n"
