from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from commissioning.cache_campaign import manifests as manifests_module
from commissioning.cache_campaign import oracle as oracle_module
from commissioning.cache_campaign import records as records_module
from commissioning.cache_campaign.manifests import ManifestError, freeze_manifests
from commissioning.cache_campaign.oracle import OracleError, scan_oracle_general
from commissioning.cache_campaign.records import TraceWindow, load_object, record_sha256


ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv/bin/python"
ORACLE = struct.Struct("<IQIq")
SOURCE_COMMIT = "da022c2945146e9577d91375a48d53850d7041a3"
NO_NEXT = -1


@dataclass
class Candidate:
    path: Path
    value: dict[str, object]
    trace_paths: list[Path]

    def write(self) -> None:
        self.path.write_text(
            json.dumps(self.value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _window(path: Path, seed: int, start_request: int) -> int:
    object_ids = [1, 2, 1, 3, 4, 2, 5, 6, 7, 8]
    objects = {item: seed * 1_000 + item for item in set(object_ids)}
    sizes = {item: seed * 10 + item * 64 for item in set(object_ids)}
    next_indexes = {0: 2, 1: 5}
    records = []
    for index, item in enumerate(object_ids):
        next_access = next_indexes.get(index, NO_NEXT)
        if next_access != NO_NEXT:
            next_access += start_request + 1
        records.append(
            ORACLE.pack(
                seed * 100 + index // 2,
                objects[item],
                sizes[item],
                next_access,
            )
        )
    path.write_bytes(b"".join(records))
    return sum(sizes.values())


def _trace(
    tmp_path: Path,
    *,
    trace_id: str,
    split: str,
    organization: str,
    application: str,
    dataset: str,
    origin: str,
    start_request: int,
    seed: int,
) -> tuple[dict[str, object], Path]:
    path = tmp_path / f"{trace_id}.oracleGeneral.bin"
    working_set_bytes = _window(path, seed, start_request)
    provenance_url = "https://github.com/cacheMon/cache_dataset"
    license_ref = "cache_dataset README and upstream dataset terms"
    if organization == "Tencent":
        provenance_url = "https://example.invalid/tencent/photo-cdn"
        license_ref = "Tencent photo-CDN upstream terms"
    trace = {
        "trace_id": trace_id,
        "split": split,
        "organization": organization,
        "application": application,
        "dataset": dataset,
        "provenance_url": provenance_url,
        "license_ref": license_ref,
        "path": str(path),
        "trace_type": "oracleGeneral",
        "origin_sha256": origin * 64,
        "start_request": start_request,
        "warmup_seconds": 1,
        "max_requests": 10,
        "working_set_bytes": working_set_bytes,
    }
    return trace, path


def valid_candidate(tmp_path: Path) -> Candidate:
    specifications = [
        ("dev-meta-kv-a", "dev", "Meta", "key-value", "2022_metaKV", "1", 0, 1),
        ("dev-meta-kv-b", "dev", "Meta", "key-value", "2022_metaKV", "1", 10, 2),
        ("dev-twitter-kv", "dev", "Twitter", "key-value", "2020_twitterKV", "2", 0, 3),
        ("visible-meta-cdn", "visible", "Meta", "CDN", "2022_metaCDN", "3", 0, 4),
        (
            "visible-twitter-kv-late",
            "visible",
            "Twitter",
            "application-time",
            "2020_twitterKV",
            "2",
            10,
            5,
        ),
        ("r3-tencent-photo", "r3", "Tencent", "photo-CDN", "2023_tencentPhoto", "4", 0, 6),
    ]
    traces: list[dict[str, object]] = []
    paths: list[Path] = []
    for trace_id, split, organization, application, dataset, origin, start, seed in specifications:
        trace, path = _trace(
            tmp_path,
            trace_id=trace_id,
            split=split,
            organization=organization,
            application=application,
            dataset=dataset,
            origin=origin,
            start_request=start,
            seed=seed,
        )
        traces.append(trace)
        paths.append(path)
    value: dict[str, object] = {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "cache_fractions": [0.01, 0.05, 0.10],
        "traces": traces,
    }
    candidate = Candidate(tmp_path / "candidate.json", value, paths)
    candidate.write()
    return candidate


def _traces(candidate: Candidate) -> list[dict[str, object]]:
    traces = candidate.value["traces"]
    assert isinstance(traces, list)
    return traces


def task_root(tmp_path: Path) -> Path:
    return tmp_path / "task-root"


def task_output(tmp_path: Path) -> Path:
    return task_root(tmp_path) / "task"


def host_root(tmp_path: Path) -> Path:
    return tmp_path / "host-root"


def host_output(tmp_path: Path) -> Path:
    return host_root(tmp_path) / "host"


def inject(candidate: Candidate, defect: str) -> None:
    traces = _traces(candidate)
    if defect == "duplicate_bytes":
        candidate.trace_paths[1].write_bytes(candidate.trace_paths[0].read_bytes())
    elif defect == "overlapping_origin_interval":
        traces[1]["start_request"] = 9
    elif defect == "random_sampling":
        traces[0]["sampling"] = "random"
    elif defect == "too_few_dev_windows":
        traces.pop(2)
    elif defect == "one_dev_source":
        traces[2]["organization"] = "Meta"
    elif defect == "visible_reuses_window":
        traces[3]["path"] = traces[0]["path"]
    elif defect == "r3_seen_organization":
        traces[-1]["organization"] = "Meta"
    elif defect == "fraction_mismatch":
        candidate.value["cache_fractions"] = [0.01, 0.05, 0.20]
    elif defect == "bad_hash":
        traces[0]["origin_sha256"] = "not-a-sha256"
    elif defect == "relative_path":
        traces[0]["path"] = candidate.trace_paths[0].name
    else:
        raise AssertionError(f"unknown defect: {defect}")
    candidate.write()


def _freeze(candidate: Candidate, tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    return freeze_manifests(candidate.path, task_output(tmp_path), host_output(tmp_path))


def test_freeze_writes_visible_splits_and_only_r3_commitment(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    task, host = _freeze(candidate, tmp_path)
    task_bytes = b"".join(
        path.read_bytes()
        for path in sorted(task_root(tmp_path).rglob("*"))
        if path.is_file()
    )
    r3 = _traces(candidate)[-1]
    r3_path = candidate.trace_paths[-1]

    assert b"r3-tencent-photo" not in task_bytes
    assert os.fsencode(r3_path) not in task_bytes
    for key in (
        "trace_id",
        "organization",
        "application",
        "dataset",
        "provenance_url",
        "license_ref",
        "origin_sha256",
    ):
        assert str(r3[key]).encode() not in task_bytes
    assert host["traces"][0]["sha256"].encode() not in task_bytes
    assert host["traces"][0]["diagnostic_sha256"].encode() not in task_bytes
    assert task["r3_commitment_sha256"] == host["manifest_sha256"]
    assert {item["split"] for item in task["traces"]} == {"dev", "visible"}
    assert "r3_count" not in task
    assert load_object(task_output(tmp_path) / "task.json") == task
    assert load_object(host_output(tmp_path) / "r3.json") == host
    assert (task_output(tmp_path) / "task.json").read_bytes() == (
        json.dumps(task, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    assert (host_output(tmp_path) / "r3.json").read_bytes() == (
        json.dumps(host, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    assert record_sha256(task, "manifest_sha256") == task["manifest_sha256"]
    assert record_sha256(host, "manifest_sha256") == host["manifest_sha256"]


@pytest.mark.parametrize(
    "placement",
    ["candidate", "host_output", "host_parent", "r3_trace"],
)
def test_freeze_rejects_r3_material_inside_task_root(
    tmp_path: Path,
    placement: str,
) -> None:
    candidate = valid_candidate(tmp_path)
    root = task_root(tmp_path)
    root.mkdir()
    candidate_path = candidate.path
    private_output = host_output(tmp_path)
    if placement == "candidate":
        candidate_path = root / "candidate.json"
        candidate_path.write_bytes(candidate.path.read_bytes())
    elif placement == "host_output":
        private_output = root / "host"
    elif placement == "host_parent":
        private_output = root / "private" / "host"
    elif placement == "r3_trace":
        exposed = root / candidate.trace_paths[-1].name
        exposed.write_bytes(candidate.trace_paths[-1].read_bytes())
        _traces(candidate)[-1]["path"] = str(exposed)
        candidate.write()

    with pytest.raises(ManifestError, match="task root"):
        freeze_manifests(candidate_path, task_output(tmp_path), private_output)

    assert not task_output(tmp_path).exists()
    assert not private_output.exists()


def test_freeze_recomputes_sizes_hashes_and_diagnostics(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    task, host = _freeze(candidate, tmp_path)
    by_id = {item["trace_id"]: item for item in [*task["traces"], *host["traces"]]}

    for source, path in zip(_traces(candidate), candidate.trace_paths, strict=True):
        frozen = by_id[source["trace_id"]]
        assert frozen["size_bytes"] == len(path.read_bytes()) == ORACLE.size * 10
        assert frozen["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        diagnostic = frozen["diagnostics"]
        assert frozen["diagnostic_sha256"] == diagnostic["diagnostic_sha256"]
        assert record_sha256(diagnostic, "diagnostic_sha256") == diagnostic["diagnostic_sha256"]
        assert diagnostic["request_count"] == 10
        assert diagnostic["unique_object_count"] == 8
        assert diagnostic["one_hit_object_fraction"] == {"denominator": 8, "numerator": 6}
        assert diagnostic["one_hit_request_fraction"] == {"denominator": 10, "numerator": 6}
        assert diagnostic["reuse_distance"]["counts"] == {"1": 1, "2": 1}
        assert diagnostic["reuse_distance"]["no_next_count"] == 8


@pytest.mark.parametrize(
    "literal",
    ["0.0100000000000000001", "0.0099999999999999999"],
)
def test_cache_fraction_validation_preserves_json_lexical_precision(
    tmp_path: Path, literal: str
) -> None:
    candidate = valid_candidate(tmp_path)
    raw = candidate.path.read_text(encoding="utf-8")
    candidate.path.write_text(
        raw.replace("    0.01,", f"    {literal},", 1),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="cache_fractions"):
        _freeze(candidate, tmp_path)


@pytest.mark.parametrize("value", [True, "0.01"])
def test_cache_fraction_validation_rejects_bool_and_string(
    tmp_path: Path, value: object
) -> None:
    candidate = valid_candidate(tmp_path)
    candidate.value["cache_fractions"] = [value, 0.05, 0.10]
    candidate.write()

    with pytest.raises(ManifestError, match="cache_fractions"):
        _freeze(candidate, tmp_path)


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate_bytes",
        "overlapping_origin_interval",
        "random_sampling",
        "too_few_dev_windows",
        "one_dev_source",
        "visible_reuses_window",
        "r3_seen_organization",
        "fraction_mismatch",
        "bad_hash",
        "relative_path",
    ],
)
def test_freeze_rejects_contaminated_or_incomplete_portfolio(
    tmp_path: Path, defect: str
) -> None:
    candidate = valid_candidate(tmp_path)
    inject(candidate, defect)

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()


@pytest.mark.parametrize(
    ("scope", "key", "value"),
    [
        ("top", "unexpected", True),
        ("top", "schema_version", "1"),
        ("top", "schema_version", True),
        ("top", "source_commit", "0" * 40),
        ("trace", "unexpected", True),
        ("trace", "split", []),
        ("trace", "trace_type", "oracle"),
        ("trace", "origin_sha256", "A" * 64),
        ("trace", "start_request", "0"),
        ("trace", "start_request", True),
        ("trace", "start_request", -1),
        ("trace", "warmup_seconds", 0),
        ("trace", "max_requests", 0),
        ("trace", "working_set_bytes", 0),
    ],
)
def test_candidate_requires_exact_keys_types_and_bounds(
    tmp_path: Path, scope: str, key: str, value: object
) -> None:
    candidate = valid_candidate(tmp_path)
    target = candidate.value if scope == "top" else _traces(candidate)[0]
    target[key] = value
    candidate.write()

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_freeze_rejects_symlink_and_nonregular_trace_files(tmp_path: Path, kind: str) -> None:
    candidate = valid_candidate(tmp_path)
    replacement = tmp_path / f"replacement-{kind}"
    if kind == "symlink":
        replacement.symlink_to(candidate.trace_paths[0])
    else:
        replacement.mkdir()
    _traces(candidate)[0]["path"] = str(replacement)
    candidate.write()

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "truncated",
        "fewer",
        "surplus_full",
        "surplus_partial",
        "nonmonotonic",
        "zero_size",
        "backward_next",
        "self_next",
    ],
)
def test_freeze_rejects_invalid_oracle_records(tmp_path: Path, mutation: str) -> None:
    candidate = valid_candidate(tmp_path)
    path = candidate.trace_paths[0]
    records = [list(ORACLE.unpack_from(path.read_bytes(), offset)) for offset in range(0, ORACLE.size * 10, ORACLE.size)]
    if mutation == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    elif mutation == "fewer":
        path.write_bytes(path.read_bytes()[:-ORACLE.size])
    elif mutation == "surplus_full":
        path.write_bytes(path.read_bytes() + ORACLE.pack(999, 999, 999, NO_NEXT))
    elif mutation == "surplus_partial":
        path.write_bytes(path.read_bytes() + b"\x00")
    elif mutation == "nonmonotonic":
        records[5][0] = 0
    elif mutation == "zero_size":
        records[4][2] = 0
    elif mutation == "backward_next":
        records[4][3] = 1
    elif mutation == "self_next":
        records[4][3] = 5
    if mutation not in {"truncated", "fewer", "surplus_full", "surplus_partial"}:
        path.write_bytes(b"".join(ORACLE.pack(*record) for record in records))

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)


def test_oracle_immediate_reuse_is_base2_bin_zero(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    path = candidate.trace_paths[0]
    records = [
        list(ORACLE.unpack_from(path.read_bytes(), offset))
        for offset in range(0, ORACLE.size * 10, ORACLE.size)
    ]
    records[0][3] = 2
    path.write_bytes(b"".join(ORACLE.pack(*record) for record in records))

    task, _ = _freeze(candidate, tmp_path)

    trace = next(item for item in task["traces"] if item["trace_id"] == "dev-meta-kv-a")
    assert trace["diagnostics"]["reuse_distance"]["counts"]["0"] == 1


def test_direct_scanner_closes_owned_temporary_descriptor_on_early_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    trace = TraceWindow.from_candidate(_traces(candidate)[0])
    trace = replace(trace, size_bytes=trace.size_bytes + 1)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_open = oracle_module.os.open
    real_close = oracle_module.os.close
    opened: list[int] = []
    closed: list[int] = []

    def record_open(path: object, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if Path(path) == scratch:
            opened.append(descriptor)
        return descriptor

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(oracle_module.os, "open", record_open)
    monkeypatch.setattr(oracle_module.os, "close", record_close)

    with pytest.raises(OracleError, match="misaligned"):
        scan_oracle_general(trace, scratch)

    assert set(opened) <= set(closed)


def test_scanner_bucket_fstat_failure_closes_and_removes_registered_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    trace = TraceWindow.from_candidate(_traces(candidate)[0])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch_descriptor = os.open(
        scratch,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    prefix = ".oracle-test-fstat-"
    real_open = oracle_module.os.open
    real_fstat = oracle_module.os.fstat
    bucket_descriptors: list[int] = []
    failed = False

    def record_open(path: object, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(prefix):
            bucket_descriptors.append(descriptor)
        return descriptor

    def fail_bucket_fstat_once(descriptor: int) -> os.stat_result:
        nonlocal failed
        if descriptor in bucket_descriptors and not failed:
            failed = True
            raise OSError("simulated bucket fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(oracle_module.os, "open", record_open)
    monkeypatch.setattr(oracle_module.os, "fstat", fail_bucket_fstat_once)
    try:
        with pytest.raises((OSError, OracleError)):
            scan_oracle_general(
                trace,
                scratch,
                temporary_descriptor=scratch_descriptor,
                scan_prefix=prefix,
            )
    finally:
        os.close(scratch_descriptor)

    assert failed
    assert not [path for path in scratch.iterdir() if path.name.startswith(prefix)]
    for descriptor in bucket_descriptors:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_scanner_bucket_fdopen_failure_closes_created_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    trace = TraceWindow.from_candidate(_traces(candidate)[0])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch_descriptor = os.open(
        scratch,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    prefix = ".oracle-test-fdopen-"
    real_open = oracle_module.os.open
    real_fdopen = oracle_module.os.fdopen
    real_fstat = oracle_module.os.fstat
    bucket_descriptors: list[int] = []
    failed = False

    def record_open(path: object, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(prefix):
            bucket_descriptors.append(descriptor)
        return descriptor

    def fail_bucket_fdopen(descriptor: int, *args: object, **kwargs: object) -> object:
        nonlocal failed
        if descriptor in bucket_descriptors and not failed:
            failed = True
            raise OSError("simulated bucket fdopen failure")
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(oracle_module.os, "open", record_open)
    monkeypatch.setattr(oracle_module.os, "fdopen", fail_bucket_fdopen)
    try:
        with pytest.raises((OSError, OracleError)):
            scan_oracle_general(
                trace,
                scratch,
                temporary_descriptor=scratch_descriptor,
                scan_prefix=prefix,
            )
    finally:
        os.close(scratch_descriptor)

    assert failed
    assert not [path for path in scratch.iterdir() if path.name.startswith(prefix)]
    for descriptor in bucket_descriptors:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_scanner_cleanup_error_continues_and_closes_owned_directory_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    trace_path = candidate.trace_paths[0]
    records = [
        list(ORACLE.unpack_from(trace_path.read_bytes(), offset))
        for offset in range(0, ORACLE.size * 10, ORACLE.size)
    ]
    records[4][2] = 0
    trace_path.write_bytes(b"".join(ORACLE.pack(*record) for record in records))
    trace = TraceWindow.from_candidate(_traces(candidate)[0])
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    prefix = ".oracle-test-cleanup-"
    real_open = oracle_module.os.open
    real_stat = oracle_module.os.stat
    real_fstat = oracle_module.os.fstat
    scratch_descriptors: list[int] = []
    failed = False

    def record_open(path: object, *args: object, **kwargs: object) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if path == scratch:
            scratch_descriptors.append(descriptor)
        return descriptor

    def fail_one_cleanup_stat(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal failed
        if isinstance(path, str) and path.startswith(prefix) and not failed:
            failed = True
            raise OSError("simulated cleanup stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(oracle_module.os, "open", record_open)
    monkeypatch.setattr(oracle_module.os, "stat", fail_one_cleanup_stat)

    with pytest.raises((OSError, OracleError)):
        scan_oracle_general(trace, scratch, scan_prefix=prefix)

    assert failed
    remaining = [path for path in scratch.iterdir() if path.name.startswith(prefix)]
    assert len(remaining) == 1
    for descriptor in scratch_descriptors:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_freeze_rejects_working_set_mismatch(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    _traces(candidate)[0]["working_set_bytes"] += 1
    candidate.write()

    with pytest.raises(ManifestError, match="working set"):
        _freeze(candidate, tmp_path)


def test_freeze_rejects_compressed_oracle_input(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    compressed = tmp_path / "dev-meta-kv-a.oracleGeneral.bin.gz"
    compressed.write_bytes(gzip.compress(candidate.trace_paths[0].read_bytes()))
    _traces(candidate)[0]["path"] = str(compressed)
    candidate.write()

    with pytest.raises(ManifestError, match="compressed"):
        _freeze(candidate, tmp_path)


def test_freeze_rejects_duplicate_trace_ids(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    _traces(candidate)[1]["trace_id"] = _traces(candidate)[0]["trace_id"]
    candidate.write()

    with pytest.raises(ManifestError, match="trace ID"):
        _freeze(candidate, tmp_path)


def test_visible_same_application_requires_same_origin_disjoint_interval(
    tmp_path: Path,
) -> None:
    candidate = valid_candidate(tmp_path)
    visible = _traces(candidate)[3]
    visible["organization"] = "UnseenVisibleOrg"
    visible["application"] = "key-value"
    visible["origin_sha256"] = "5" * 64
    candidate.write()

    with pytest.raises(ManifestError, match="visible"):
        _freeze(candidate, tmp_path)


def test_visible_same_application_accepts_same_origin_disjoint_interval(
    tmp_path: Path,
) -> None:
    candidate = valid_candidate(tmp_path)
    traces = _traces(candidate)
    traces[0]["application"] = "social"
    traces[1]["application"] = "social"
    traces[4]["application"] = "key-value"
    candidate.write()

    task, _ = _freeze(candidate, tmp_path)

    assert any(item["trace_id"] == "visible-twitter-kv-late" for item in task["traces"])


@pytest.mark.parametrize("existing", ["task", "host"])
def test_freeze_refuses_preexisting_outputs(tmp_path: Path, existing: str) -> None:
    candidate = valid_candidate(tmp_path)
    protected = task_output(tmp_path) if existing == "task" else host_output(tmp_path)
    protected.parent.mkdir(parents=True)
    protected.mkdir()
    marker = protected / "caller-data"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    assert marker.read_text(encoding="utf-8") == "keep\n"
    other = host_output(tmp_path) if existing == "task" else task_output(tmp_path)
    assert not other.exists()


@pytest.mark.parametrize("alias", ["same_outputs", "nested_outputs", "input_output"])
def test_freeze_rejects_aliasing_input_and_output_paths(tmp_path: Path, alias: str) -> None:
    candidate = valid_candidate(tmp_path)
    task = task_output(tmp_path)
    host = host_output(tmp_path)
    if alias == "same_outputs":
        host = task
    elif alias == "nested_outputs":
        host = task / "host"
    elif alias == "input_output":
        task = candidate.path

    with pytest.raises(ManifestError):
        freeze_manifests(candidate.path, task, host)

    if alias != "input_output":
        assert not task.exists()
    assert not host.exists()


def test_failed_validation_cleans_staging_directories(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    candidate.trace_paths[0].write_bytes(candidate.trace_paths[0].read_bytes()[:-1])

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


@pytest.mark.parametrize("failure_point", ["before_rename", "after_rename"])
def test_second_publication_failure_rolls_back_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    calls = 0

    def fail_second(staging: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure_point == "after_rename":
                real_publish(staging, output)
            raise OSError("simulated host publication failure")
        real_publish(staging, output)

    monkeypatch.setattr(manifests_module, "_publish_directory", fail_second)

    with pytest.raises(ManifestError, match="publication"):
        _freeze(candidate, tmp_path)

    assert calls == 2
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_scanner_uses_retained_stages_without_child_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_scan = manifests_module.scan_oracle_general
    real_mkdir = manifests_module.os.mkdir
    observed: list[tuple[str, Path, int | None, object]] = []
    child_directories: list[str] = []

    def record_scan(
        trace: object,
        temporary_directory: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        observed.append(
            (
                trace.split,
                temporary_directory,
                kwargs.get("temporary_descriptor"),
                kwargs.get("scan_prefix"),
            )
        )
        return real_scan(trace, temporary_directory, **kwargs)

    def record_mkdir(path: object, *args: object, **kwargs: object) -> None:
        if path == ".oracle-scan":
            child_directories.append(str(path))
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(manifests_module, "scan_oracle_general", record_scan)
    monkeypatch.setattr(manifests_module.os, "mkdir", record_mkdir)

    _freeze(candidate, tmp_path)

    assert not child_directories
    assert len(observed) == len(_traces(candidate))
    prefixes = []
    for split, stage, descriptor, prefix in observed:
        expected = ".host." if split == "r3" else ".task."
        assert stage.name.startswith(expected)
        assert isinstance(descriptor, int)
        assert isinstance(prefix, str) and prefix.startswith(".oracle-")
        prefixes.append(prefix)
    assert len(prefixes) == len(set(prefixes))


def test_r3_scanner_failure_leaves_no_bucket_or_manifest_staging(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    r3_path = candidate.trace_paths[-1]
    records = [
        list(ORACLE.unpack_from(r3_path.read_bytes(), offset))
        for offset in range(0, ORACLE.size * 10, ORACLE.size)
    ]
    records[4][2] = 0
    r3_path.write_bytes(b"".join(ORACLE.pack(*record) for record in records))

    with pytest.raises(ManifestError, match="nonpositive"):
        _freeze(candidate, tmp_path)

    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_publish_fails_closed_without_atomic_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    staging.mkdir()

    class LibcWithoutRenameat2:
        pass

    monkeypatch.setattr(
        manifests_module.ctypes,
        "CDLL",
        lambda *args, **kwargs: LibcWithoutRenameat2(),
    )

    with pytest.raises(ManifestError, match="no-replace"):
        manifests_module._publish_directory(staging, output)

    assert staging.is_dir()
    assert not output.exists()


def test_staging_cleanup_failure_is_reported_and_other_stage_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    candidate.trace_paths[0].write_bytes(candidate.trace_paths[0].read_bytes()[:-1])
    real_rmdir = manifests_module.os.rmdir

    def fail_task_stage(path: object, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".task."):
            raise OSError("simulated cleanup failure")
        real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(manifests_module.os, "rmdir", fail_task_stage)

    with pytest.raises(ManifestError, match="temporary directory cleanup failed"):
        _freeze(candidate, tmp_path)

    assert list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_cleanup_preserves_replaced_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    caller_sentinel: Path | None = None

    def replace_staging(
        trace: object,
        temporary_directory: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal caller_sentinel
        staging = temporary_directory
        shutil.rmtree(staging)
        staging.mkdir()
        caller_sentinel = staging / "caller-sentinel"
        caller_sentinel.write_text("keep\n", encoding="utf-8")
        raise OSError("simulated scan failure after staging replacement")

    monkeypatch.setattr(manifests_module, "scan_oracle_general", replace_staging)

    with pytest.raises(ManifestError, match="cleanup conflict"):
        _freeze(candidate, tmp_path)

    assert caller_sentinel is not None
    assert caller_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not list(host_root(tmp_path).glob(".host.*"))
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()


def test_cleanup_preserves_stage_replaced_after_ownership_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    candidate.trace_paths[0].write_bytes(candidate.trace_paths[0].read_bytes()[:-1])
    real_state = manifests_module._directory_state
    caller_sentinel: Path | None = None

    def replace_after_check(path: Path, identity: tuple[int, int] | None) -> str:
        nonlocal caller_sentinel
        state = real_state(path, identity)
        if state == "owned" and path.name.startswith(".task.") and caller_sentinel is None:
            shutil.rmtree(path)
            path.mkdir()
            caller_sentinel = path / "caller-sentinel"
            caller_sentinel.write_text("keep\n", encoding="utf-8")
        return state

    monkeypatch.setattr(manifests_module, "_directory_state", replace_after_check)

    with pytest.raises(ManifestError, match="cleanup conflict"):
        _freeze(candidate, tmp_path)

    assert caller_sentinel is not None
    assert caller_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_rollback_preserves_replaced_first_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    caller_sentinel = task_output(tmp_path) / "caller-sentinel"
    calls = 0

    def replace_first_then_fail_second(staging: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_publish(staging, output)
            return
        shutil.rmtree(task_output(tmp_path))
        task_output(tmp_path).mkdir()
        caller_sentinel.write_text("keep\n", encoding="utf-8")
        raise OSError("simulated host failure after task replacement")

    monkeypatch.setattr(
        manifests_module,
        "_publish_directory",
        replace_first_then_fail_second,
    )

    with pytest.raises(ManifestError, match="rollback conflict"):
        _freeze(candidate, tmp_path)

    assert caller_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_rollback_preserves_output_replaced_after_ownership_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    real_state = manifests_module._directory_state
    caller_sentinel = task_output(tmp_path) / "caller-sentinel"
    calls = 0
    task_checks = 0
    replaced = False

    def fail_second(staging: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated host publication failure")
        real_publish(staging, output)

    def replace_after_check(path: Path, identity: tuple[int, int] | None) -> str:
        nonlocal replaced, task_checks
        state = real_state(path, identity)
        if path == task_output(tmp_path):
            task_checks += 1
        if task_checks >= 2 and state == "owned" and not replaced:
            replaced = True
            shutil.rmtree(path)
            path.mkdir()
            caller_sentinel.write_text("keep\n", encoding="utf-8")
        return state

    monkeypatch.setattr(manifests_module, "_publish_directory", fail_second)
    monkeypatch.setattr(manifests_module, "_directory_state", replace_after_check)

    with pytest.raises(ManifestError, match="rollback conflict"):
        _freeze(candidate, tmp_path)

    assert caller_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_publish_rejects_stage_replaced_immediately_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    foreign_sentinel = task_output(tmp_path) / "foreign-sentinel"
    calls = 0

    def replace_before_publish(staging: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            shutil.rmtree(staging)
            staging.mkdir()
            (staging / "foreign-sentinel").write_text("keep\n", encoding="utf-8")
        real_publish(staging, output)

    monkeypatch.setattr(manifests_module, "_publish_directory", replace_before_publish)

    with pytest.raises(ManifestError, match="ownership conflict"):
        _freeze(candidate, tmp_path)

    assert calls == 1
    assert foreign_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_publish_then_raise_tracks_output_when_vacated_stage_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    caller_sentinel: Path | None = None
    calls = 0

    def publish_reuse_then_raise(staging: Path, output: Path) -> None:
        nonlocal caller_sentinel, calls
        calls += 1
        if calls == 1:
            real_publish(staging, output)
            staging.mkdir()
            caller_sentinel = staging / "caller-sentinel"
            caller_sentinel.write_text("keep\n", encoding="utf-8")
            raise OSError("simulated post-rename failure")
        real_publish(staging, output)

    monkeypatch.setattr(manifests_module, "_publish_directory", publish_reuse_then_raise)

    with pytest.raises(ManifestError, match="publication"):
        _freeze(candidate, tmp_path)

    assert calls == 1
    assert caller_sentinel is not None
    assert caller_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_publish_revalidates_manifest_bytes_after_each_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    mutated = task_output(tmp_path) / "task.json"
    calls = 0

    def mutate_after_publish(staging: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        real_publish(staging, output)
        if calls == 1:
            mutated.write_bytes(b"caller-mutated-task\n")

    monkeypatch.setattr(manifests_module, "_publish_directory", mutate_after_publish)

    with pytest.raises(ManifestError, match="changed"):
        _freeze(candidate, tmp_path)

    assert calls == 1
    assert mutated.read_bytes() == b"caller-mutated-task\n"
    assert not host_output(tmp_path).exists()


def test_freeze_revalidates_manifest_bytes_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_fsync = manifests_module._fsync_directory
    mutated = host_output(tmp_path) / "r3.json"
    changed = False

    def mutate_after_parent_fsync(path: Path) -> None:
        nonlocal changed
        real_fsync(path)
        if not changed:
            changed = True
            mutated.write_bytes(b"caller-mutated-r3\n")

    monkeypatch.setattr(manifests_module, "_fsync_directory", mutate_after_parent_fsync)

    with pytest.raises(ManifestError, match="changed"):
        _freeze(candidate, tmp_path)

    assert changed
    assert mutated.read_bytes() == b"caller-mutated-r3\n"
    assert not task_output(tmp_path).exists()


def test_final_path_binding_rejects_replaced_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_verify = manifests_module._verify_owned_record
    host_sentinel = host_output(tmp_path) / "caller-sentinel"
    verifications = 0

    def replace_after_retained_verification(
        directory: object,
        name: str,
        value: dict[str, object],
    ) -> None:
        nonlocal verifications
        real_verify(directory, name, value)
        verifications += 1
        if verifications == 4:
            shutil.rmtree(host_output(tmp_path))
            host_output(tmp_path).mkdir()
            host_sentinel.write_text("keep\n", encoding="utf-8")

    monkeypatch.setattr(
        manifests_module,
        "_verify_owned_record",
        replace_after_retained_verification,
    )

    with pytest.raises(ManifestError, match="ownership conflict"):
        _freeze(candidate, tmp_path)

    assert verifications == 5
    assert host_sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not task_output(tmp_path).exists()


def test_linked_manifest_is_cleaned_when_directory_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_fsync = manifests_module.os.fsync
    failed = False

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError("simulated staging fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(manifests_module.os, "fsync", fail_first_directory_fsync)

    with pytest.raises(ManifestError, match="fsync"):
        _freeze(candidate, tmp_path)

    assert failed
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_temp_record_replacement_is_not_unlinked_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_fsync = manifests_module.os.fsync
    foreign_temp: Path | None = None

    def replace_temp_then_fail(descriptor: int) -> None:
        nonlocal foreign_temp
        if foreign_temp is None and stat.S_ISREG(os.fstat(descriptor).st_mode):
            stages = list(host_root(tmp_path).glob(".host.*"))
            assert len(stages) == 1
            temporary = [path for path in stages[0].iterdir() if path.name.endswith(".tmp")]
            assert len(temporary) == 1
            foreign_temp = temporary[0]
            foreign_temp.unlink()
            foreign_temp.write_text("caller-temp\n", encoding="utf-8")
            raise OSError("simulated record write failure")
        real_fsync(descriptor)

    monkeypatch.setattr(manifests_module.os, "fsync", replace_temp_then_fail)

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    assert foreign_temp is not None
    assert foreign_temp.read_text(encoding="utf-8") == "caller-temp\n"
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()


def test_final_receipt_survives_first_final_name_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_read = manifests_module._read_owned_manifest
    failed = False

    def fail_first_final_read(
        directory: object,
        name: str,
    ) -> object:
        nonlocal failed
        if name == "r3.json" and not failed:
            failed = True
            raise OSError("simulated final-name read failure")
        return real_read(directory, name)

    monkeypatch.setattr(manifests_module, "_read_owned_manifest", fail_first_final_read)

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    assert failed
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_owned_file_refresh_rejects_same_inode_hash_mutation(tmp_path: Path) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    entry = directory / "entry"
    entry.write_bytes(b"original")
    owned = manifests_module._open_owned_directory(directory)
    try:
        owned.files["entry"] = manifests_module._read_owned_manifest(owned, "entry")
        entry.write_bytes(b"mutated!")

        with pytest.raises(ManifestError, match="changed"):
            manifests_module._refresh_owned_file(owned, "entry")
    finally:
        os.close(owned.descriptor)


def test_final_capture_rejects_same_inode_hash_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_capture = manifests_module._capture_owned_manifest
    mutated_path: Path | None = None

    def mutate_before_capture(directory: object, name: str) -> None:
        nonlocal mutated_path
        if name == "r3.json" and mutated_path is None:
            assert directory.path is not None
            mutated_path = directory.path / name
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_TRUNC,
                dir_fd=directory.descriptor,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"caller-mutated-r3\n")
        real_capture(directory, name)

    monkeypatch.setattr(
        manifests_module,
        "_capture_owned_manifest",
        mutate_before_capture,
    )

    with pytest.raises(ManifestError, match="changed"):
        _freeze(candidate, tmp_path)

    assert mutated_path is not None
    assert mutated_path.read_bytes() == b"caller-mutated-r3\n"
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()


def test_cleanup_removes_owned_r3_manifest_but_preserves_foreign_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_capture = manifests_module._capture_owned_manifest
    host_stage: Path | None = None

    def add_foreign_entry_then_fail(directory: object, name: str) -> None:
        nonlocal host_stage
        real_capture(directory, name)
        if name == "r3.json":
            assert directory.path is not None
            host_stage = directory.path
            (host_stage / "caller-sentinel").write_text("keep\n", encoding="utf-8")
            raise OSError("simulated failure with mixed entries")

    monkeypatch.setattr(
        manifests_module,
        "_capture_owned_manifest",
        add_foreign_entry_then_fail,
    )

    with pytest.raises(ManifestError, match="cleanup conflict"):
        _freeze(candidate, tmp_path)

    assert host_stage is not None
    assert (host_stage / "caller-sentinel").read_text(encoding="utf-8") == "keep\n"
    assert not (host_stage / "r3.json").exists()
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()


@pytest.mark.parametrize("target", ["manifest", "temp", "bucket"])
def test_quarantine_delete_preserves_swap_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    candidate = valid_candidate(tmp_path)
    module = oracle_module if target == "bucket" else manifests_module
    real_quarantine = module.quarantine_unlink
    swapped = False

    def swap_then_quarantine(
        directory_descriptor: int,
        name: str,
        identity: tuple[int, int],
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        selected = (
            (target == "manifest" and name == "r3.json")
            or (target == "temp" and name.endswith(".tmp"))
            or (target == "bucket" and "bucket-" in name)
        )
        if selected and not swapped:
            swapped = True
            os.unlink(name, dir_fd=directory_descriptor)
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                dir_fd=directory_descriptor,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(f"caller-{target}\n".encode())
        real_quarantine(directory_descriptor, name, identity, **kwargs)

    monkeypatch.setattr(module, "quarantine_unlink", swap_then_quarantine)
    if target == "manifest":
        real_publish = manifests_module._publish_directory
        publish_calls = 0

        def fail_second_publish(staging: Path, output: Path) -> None:
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 2:
                raise OSError("simulated host publication failure")
            real_publish(staging, output)

        monkeypatch.setattr(
            manifests_module,
            "_publish_directory",
            fail_second_publish,
        )

    with pytest.raises(ManifestError, match="conflict|changed"):
        _freeze(candidate, tmp_path)

    assert swapped
    preserved = b"".join(
        path.read_bytes()
        for root in (task_root(tmp_path), host_root(tmp_path))
        if root.exists()
        for path in root.rglob("*")
        if path.is_file()
    )
    assert f"caller-{target}\n".encode() in preserved


def test_manifest_link_temp_unlink_directory_fsync_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_link = manifests_module.os.link
    real_unlink = manifests_module.os.unlink
    real_fsync = manifests_module.os.fsync
    events: list[str] = []
    linked = False

    def record_link(*args: object, **kwargs: object) -> None:
        nonlocal linked
        real_link(*args, **kwargs)
        linked = True
        events.append("link")

    def record_unlink(path: object, *args: object, **kwargs: object) -> None:
        real_unlink(path, *args, **kwargs)
        if linked and isinstance(path, str) and path.startswith(".quarantine-"):
            events.append("unlink-temp")

    def record_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        if linked and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            events.append("fsync-directory")

    monkeypatch.setattr(manifests_module.os, "link", record_link)
    monkeypatch.setattr(manifests_module.os, "unlink", record_unlink)
    monkeypatch.setattr(manifests_module.os, "fsync", record_fsync)

    _freeze(candidate, tmp_path)

    first_link = events.index("link")
    first_unlink = events.index("unlink-temp", first_link)
    first_fsync = events.index("fsync-directory", first_unlink)
    assert first_link < first_unlink < first_fsync


def test_identity_only_quarantine_does_not_read_file_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "quarantine"
    directory.mkdir()
    target = directory / "bucket"
    target.write_bytes(b"bucket-content")
    metadata = target.stat()
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    def forbid_fdopen(*args: object, **kwargs: object) -> object:
        raise AssertionError("identity-only quarantine must not read file bytes")

    monkeypatch.setattr(records_module.os, "fdopen", forbid_fdopen)
    try:
        records_module.quarantine_unlink(
            descriptor,
            target.name,
            (metadata.st_dev, metadata.st_ino),
        )
    finally:
        os.close(descriptor)

    assert not target.exists()


@pytest.mark.parametrize("stage_name", ["task", "host"])
def test_stage_open_failure_cleans_all_registered_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_name: str,
) -> None:
    candidate = valid_candidate(tmp_path)
    real_open = manifests_module.os.open
    failed = False

    def fail_stage_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal failed
        if (
            not failed
            and isinstance(path, Path)
            and path.name.startswith(f".{stage_name}.")
        ):
            failed = True
            raise OSError(f"simulated {stage_name} stage open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(manifests_module.os, "open", fail_stage_open)

    with pytest.raises(ManifestError, match="open failure"):
        _freeze(candidate, tmp_path)

    assert failed
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()


def test_r3_manifest_write_does_not_follow_replaced_host_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_record_sha256 = manifests_module.record_sha256
    foreign_stage: Path | None = None

    def replace_host_stage(value: object, field: str) -> str:
        nonlocal foreign_stage
        result = real_record_sha256(value, field)
        if (
            foreign_stage is None
            and isinstance(value, dict)
            and isinstance(value.get("traces"), list)
            and value["traces"]
            and all(item.get("split") == "r3" for item in value["traces"])
        ):
            stages = list(host_root(tmp_path).glob(".host.*"))
            assert len(stages) == 1
            foreign_stage = stages[0]
            shutil.rmtree(foreign_stage)
            foreign_stage.mkdir()
            (foreign_stage / "caller-sentinel").write_text("keep\n", encoding="utf-8")
        return result

    monkeypatch.setattr(manifests_module, "record_sha256", replace_host_stage)

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    assert foreign_stage is not None
    foreign_bytes = b"".join(
        path.read_bytes() for path in foreign_stage.iterdir() if path.is_file()
    )
    assert b"r3-tencent-photo" not in foreign_bytes
    assert b"Tencent" not in foreign_bytes
    assert (foreign_stage / "caller-sentinel").read_text(encoding="utf-8") == "keep\n"
    assert not task_output(tmp_path).exists()
    assert not host_output(tmp_path).exists()


def test_cleanup_exception_closes_all_retained_directory_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_attach_owned = manifests_module._attach_owned_directory
    real_publish = manifests_module._publish_directory
    retained_fds: list[int] = []
    calls = 0

    def record_owned(owned: object) -> None:
        real_attach_owned(owned)
        assert owned.descriptor is not None
        retained_fds.append(owned.descriptor)

    def replace_manifest_then_fail(staging: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_publish(staging, output)
            return
        manifest = task_output(tmp_path) / "task.json"
        manifest.unlink()
        manifest.mkdir()
        (manifest / "caller-sentinel").write_text("keep\n", encoding="utf-8")
        raise OSError("simulated host publication failure")

    monkeypatch.setattr(manifests_module, "_attach_owned_directory", record_owned)
    monkeypatch.setattr(manifests_module, "_publish_directory", replace_manifest_then_fail)

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    leaked = []
    for descriptor in retained_fds:
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        leaked.append(descriptor)
        os.close(descriptor)
    assert not leaked
    assert (task_output(tmp_path) / "task.json/caller-sentinel").read_text(
        encoding="utf-8"
    ) == "keep\n"
    assert not host_output(tmp_path).exists()
    assert not list(task_root(tmp_path).glob(".task.*"))
    assert not list(host_root(tmp_path).glob(".host.*"))


def test_success_ignores_reused_vacated_staging_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    real_fsync = manifests_module._fsync_directory
    vacated: list[Path] = []
    sentinels: list[Path] = []
    recreated = False

    def record_publish(staging: Path, output: Path) -> None:
        real_publish(staging, output)
        vacated.append(staging)

    def recreate_vacated_names(path: Path) -> None:
        nonlocal recreated
        if not recreated:
            recreated = True
            for staging in vacated:
                staging.mkdir()
                sentinel = staging / "caller-sentinel"
                sentinel.write_text("keep\n", encoding="utf-8")
                sentinels.append(sentinel)
        real_fsync(path)

    monkeypatch.setattr(manifests_module, "_publish_directory", record_publish)
    monkeypatch.setattr(manifests_module, "_fsync_directory", recreate_vacated_names)

    task, host = _freeze(candidate, tmp_path)

    assert load_object(task_output(tmp_path) / "task.json") == task
    assert load_object(host_output(tmp_path) / "r3.json") == host
    assert len(sentinels) == 2
    assert all(path.read_text(encoding="utf-8") == "keep\n" for path in sentinels)


def _run_cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PYTHON), str(ROOT / "scripts/freeze_aros_cache_manifests.py"), *argv],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def test_freeze_cli_success_prints_only_public_commitment(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    cli_task_output = task_output(tmp_path)
    cli_host_output = host_output(tmp_path)

    result = _run_cli(
        "--input",
        str(candidate.path),
        "--task-output",
        str(cli_task_output),
        "--host-output",
        str(cli_host_output),
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    printed = json.loads(result.stdout)
    task = load_object(cli_task_output / "task.json")
    assert printed == {
        "r3_commitment_sha256": task["r3_commitment_sha256"],
        "task_manifest": str((cli_task_output / "task.json").absolute()),
    }
    assert "private-host" not in result.stdout
    assert "Tencent" not in result.stdout
    assert result.stdout.count("\n") == 1


def test_freeze_cli_missing_arguments_prints_one_error_line() -> None:
    result = _run_cli()

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert result.stderr.count("\n") == 1


def test_freeze_cli_validation_error_prints_one_error_line(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    candidate.value = copy.deepcopy(candidate.value)
    candidate.value["cache_fractions"] = [0.01]
    candidate.write()

    result = _run_cli(
        "--input",
        str(candidate.path),
        "--task-output",
        str(task_output(tmp_path)),
        "--host-output",
        str(host_output(tmp_path)),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert result.stderr.count("\n") == 1
