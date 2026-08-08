from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from commissioning.cache_campaign import manifests as manifests_module
from commissioning.cache_campaign.manifests import ManifestError, freeze_manifests
from commissioning.cache_campaign.records import load_object, record_sha256


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
            next_access += start_request
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
    return freeze_manifests(candidate.path, tmp_path / "task", tmp_path / "host")


def test_freeze_writes_visible_splits_and_only_r3_commitment(tmp_path: Path) -> None:
    candidate = valid_candidate(tmp_path)
    task, host = _freeze(candidate, tmp_path)
    task_bytes = b"".join(
        path.read_bytes()
        for path in sorted((tmp_path / "task").rglob("*"))
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
    assert load_object(tmp_path / "task/task.json") == task
    assert load_object(tmp_path / "host/r3.json") == host
    assert record_sha256(task, "manifest_sha256") == task["manifest_sha256"]
    assert record_sha256(host, "manifest_sha256") == host["manifest_sha256"]


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

    assert not (tmp_path / "task").exists()
    assert not (tmp_path / "host").exists()


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
    if mutation not in {"truncated", "fewer", "surplus_full", "surplus_partial"}:
        path.write_bytes(b"".join(ORACLE.pack(*record) for record in records))

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)


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
    protected = tmp_path / existing
    protected.mkdir()
    marker = protected / "caller-data"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(ManifestError):
        _freeze(candidate, tmp_path)

    assert marker.read_text(encoding="utf-8") == "keep\n"
    other = tmp_path / ("host" if existing == "task" else "task")
    assert not other.exists()


@pytest.mark.parametrize("alias", ["same_outputs", "nested_outputs", "input_output"])
def test_freeze_rejects_aliasing_input_and_output_paths(tmp_path: Path, alias: str) -> None:
    candidate = valid_candidate(tmp_path)
    task = tmp_path / "task"
    host = tmp_path / "host"
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

    assert {path.name for path in tmp_path.iterdir()} == {
        candidate.path.name,
        *(path.name for path in candidate.trace_paths),
    }


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
    assert not (tmp_path / "task").exists()
    assert not (tmp_path / "host").exists()
    assert not list(tmp_path.glob(".task.*"))
    assert not list(tmp_path.glob(".host.*"))


def test_scanner_keeps_r3_buckets_in_host_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_scan = manifests_module.scan_oracle_general
    observed: dict[str, Path] = {}

    def record_scan(trace: object, temporary_directory: Path) -> dict[str, object]:
        split = trace.split
        observed.setdefault(split, temporary_directory)
        return real_scan(trace, temporary_directory)

    monkeypatch.setattr(manifests_module, "scan_oracle_general", record_scan)

    _freeze(candidate, tmp_path)

    assert observed["dev"].parent.name.startswith(".task.")
    assert observed["visible"].parent.name.startswith(".task.")
    assert observed["r3"].parent.name.startswith(".host.")


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
    real_rmtree = manifests_module.shutil.rmtree

    def fail_task_stage(path: object, *args: object, **kwargs: object) -> None:
        if Path(path).name.startswith(".task."):
            raise OSError("simulated cleanup failure")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(manifests_module.shutil, "rmtree", fail_task_stage)

    with pytest.raises(ManifestError, match="temporary directory cleanup failed"):
        _freeze(candidate, tmp_path)

    assert list(tmp_path.glob(".task.*"))
    assert not list(tmp_path.glob(".host.*"))


def test_cleanup_preserves_replaced_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    caller_sentinel: Path | None = None

    def replace_staging(trace: object, temporary_directory: Path) -> dict[str, object]:
        nonlocal caller_sentinel
        staging = temporary_directory.parent
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
    assert not list(tmp_path.glob(".host.*"))
    assert not (tmp_path / "task").exists()
    assert not (tmp_path / "host").exists()


def test_rollback_preserves_replaced_first_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = valid_candidate(tmp_path)
    real_publish = manifests_module._publish_directory
    caller_sentinel = tmp_path / "task/caller-sentinel"
    calls = 0

    def replace_first_then_fail_second(staging: Path, output: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_publish(staging, output)
            return
        shutil.rmtree(tmp_path / "task")
        (tmp_path / "task").mkdir()
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
    assert not (tmp_path / "host").exists()
    assert not list(tmp_path.glob(".task.*"))
    assert not list(tmp_path.glob(".host.*"))


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

    assert load_object(tmp_path / "task/task.json") == task
    assert load_object(tmp_path / "host/r3.json") == host
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
    task_output = tmp_path / "task"
    host_output = tmp_path / "private-host"

    result = _run_cli(
        "--input",
        str(candidate.path),
        "--task-output",
        str(task_output),
        "--host-output",
        str(host_output),
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    printed = json.loads(result.stdout)
    task = load_object(task_output / "task.json")
    assert printed == {
        "r3_commitment_sha256": task["r3_commitment_sha256"],
        "task_manifest": str((task_output / "task.json").absolute()),
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
        str(tmp_path / "task"),
        "--host-output",
        str(tmp_path / "host"),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert result.stderr.count("\n") == 1
