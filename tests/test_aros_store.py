"""Durable AROS store primitive tests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Callable

import pytest

import arbor.aros.task_runner as task_runner_module
import arbor.aros.store as store_module
from arbor.aros.store import (
    FINAL_IDENTITY_FIELDS,
    atomic_write_json,
    canonical_json_bytes,
    create_json,
    final_identity,
    read_json,
)


_CRASH_AFTER_LINK_EXIT = 91


def test_bundle_final_identity_extends_without_changing_legacy_identity() -> None:
    legacy_manifest = {
        field: f"legacy-{field}" for field in FINAL_IDENTITY_FIELDS
    }
    legacy_identity = dict(legacy_manifest)
    legacy_bytes = canonical_json_bytes(legacy_identity)
    execution_bundle = {
        "candidate": {"path": "candidate", "commit": "a" * 40, "tree": "b" * 40},
        "apparatus": {"path": "apparatus", "commit": "c" * 40, "tree": "d" * 40},
        "temp": "tmp",
        "bundle_sha256": "e" * 64,
    }

    assert canonical_json_bytes(final_identity(legacy_manifest)) == legacy_bytes
    assert final_identity({**legacy_manifest, "execution_bundle": execution_bundle}) == {
        **legacy_identity,
        "execution_bundle": execution_bundle,
    }
    assert canonical_json_bytes(final_identity(legacy_manifest)) == legacy_bytes


def _temporary_paths(target: Path) -> list[Path]:
    return [
        entry
        for entry in target.parent.iterdir()
        if entry.name.startswith(".") and entry.name.endswith(".tmp")
    ]


def _crash_after_link(target: Path, value: object) -> None:
    pid = os.fork()
    if pid == 0:
        real_link = store_module._link

        def link_then_crash(
            source: Path,
            destination: Path,
            *,
            follow_symlinks: bool,
        ) -> None:
            real_link(
                source,
                destination,
                follow_symlinks=follow_symlinks,
            )
            os._exit(_CRASH_AFTER_LINK_EXIT)

        store_module._link = link_then_crash
        try:
            create_json(target, value)
        except BaseException:
            os._exit(_CRASH_AFTER_LINK_EXIT + 2)
        os._exit(_CRASH_AFTER_LINK_EXIT + 1)

    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == _CRASH_AFTER_LINK_EXIT


def test_create_json_does_not_publish_partial_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "record.json"
    value = {"payload": "x" * 1000}
    half_written = threading.Event()
    continue_write = threading.Event()
    real_fdopen = store_module.os.fdopen

    class PausedWriter:
        def __init__(self, handle: BinaryIO) -> None:
            self.handle = handle

        def __enter__(self) -> PausedWriter:
            self.handle.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.handle.__exit__(*args)

        def write(self, payload: bytes) -> int:
            midpoint = len(payload) // 2
            self.handle.write(payload[:midpoint])
            self.handle.flush()
            half_written.set()
            assert continue_write.wait(timeout=5)
            self.handle.write(payload[midpoint:])
            return len(payload)

        def flush(self) -> None:
            self.handle.flush()

        def fileno(self) -> int:
            return self.handle.fileno()

    monkeypatch.setattr(
        store_module.os,
        "fdopen",
        lambda fd, mode: PausedWriter(real_fdopen(fd, mode)),
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(create_json, target, value)
        assert half_written.wait(timeout=5)
        try:
            assert not target.exists()
        finally:
            continue_write.set()
        assert future.result(timeout=5) is True

    monkeypatch.setattr(store_module.os, "fdopen", real_fdopen)
    assert read_json(target) == value
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _temporary_paths(target) == []


def test_create_json_has_exactly_one_winner_and_preserves_existing_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "record.json"
    barrier = threading.Barrier(2)
    values = ({"writer": 1}, {"writer": 2})

    def create(value: dict[str, int]) -> bool:
        barrier.wait(timeout=5)
        return create_json(target, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, values))

    assert sorted(results) == [False, True]
    winner = read_json(target)
    assert winner in values
    published = target.read_bytes()
    assert create_json(target, {"writer": 3}) is False
    assert target.read_bytes() == published
    assert read_json(target) == winner
    assert target.stat().st_nlink == 1
    assert _temporary_paths(target) == []


@pytest.mark.parametrize("write_json", (create_json, atomic_write_json))
def test_json_writes_fsync_each_new_ancestor_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_json: Callable[[str | Path, object], object],
) -> None:
    target = tmp_path / "first" / "second" / "record.json"
    synced: list[Path] = []
    monkeypatch.setattr(store_module, "_fsync_directory", synced.append)

    result = write_json(target, {"value": 1})

    if write_json is create_json:
        assert result is True
    assert tmp_path in synced
    assert tmp_path / "first" in synced
    assert tmp_path / "first" / "second" in synced
    assert read_json(target) == {"value": 1}
    assert _temporary_paths(target) == []


def test_create_json_retry_fsyncs_existing_directory_chain_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "first" / "second" / "record.json"

    class InjectedCrash(RuntimeError):
        pass

    def crash_before_first_directory_fsync(_path: Path) -> None:
        raise InjectedCrash

    monkeypatch.setattr(
        store_module,
        "_fsync_directory",
        crash_before_first_directory_fsync,
    )
    with pytest.raises(InjectedCrash):
        create_json(target, {"value": 1})
    assert target.parent.is_dir()
    assert not target.exists()

    synced: list[Path] = []
    monkeypatch.setattr(store_module, "_fsync_directory", synced.append)

    assert create_json(target, {"value": 1}) is True

    device = target.parent.stat().st_dev
    expected: list[Path] = []
    directory = target.parent
    while True:
        expected.append(directory)
        parent = directory.parent
        if parent == directory or parent.stat().st_dev != device:
            break
        directory = parent
    assert synced[: len(expected)] == expected


def test_relative_create_retry_fsyncs_absolute_directory_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_directory = tmp_path / "deep" / "cwd"
    working_directory.mkdir(parents=True)
    monkeypatch.chdir(working_directory)
    target = Path("first") / "record.json"

    class InjectedCrash(RuntimeError):
        pass

    def crash_before_first_directory_fsync(_path: Path) -> None:
        raise InjectedCrash

    monkeypatch.setattr(
        store_module,
        "_fsync_directory",
        crash_before_first_directory_fsync,
    )
    with pytest.raises(InjectedCrash):
        create_json(target, {"value": 1})
    assert (working_directory / target.parent).is_dir()
    assert not target.exists()

    synced: list[Path] = []
    monkeypatch.setattr(store_module, "_fsync_directory", synced.append)

    assert create_json(target, {"value": 1}) is True

    directory = working_directory / target.parent
    device = directory.stat().st_dev
    expected: list[Path] = []
    while True:
        expected.append(directory)
        parent = directory.parent
        if parent == directory or parent.stat().st_dev != device:
            break
        directory = parent
    assert synced[: len(expected)] == expected


def test_fsync_directory_chain_stops_at_device_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    device = leaf.stat().st_dev
    boundary = tmp_path.parent
    real_stat = Path.stat

    def stat_with_boundary(path: Path, *args: object, **kwargs: object) -> object:
        if path == boundary:
            return SimpleNamespace(st_dev=device + 1)
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    synced: list[Path] = []
    monkeypatch.setattr(Path, "stat", stat_with_boundary)
    monkeypatch.setattr(store_module, "_fsync_directory", synced.append)

    store_module._fsync_directory_chain(leaf)

    assert synced == [leaf, tmp_path]


def test_read_json_recovers_same_inode_temp_alias_after_process_crash(
    tmp_path: Path,
) -> None:
    target = tmp_path / "record.json"
    value = {"payload": "complete"}

    _crash_after_link(target, value)

    temporary = _temporary_paths(target)
    assert len(temporary) == 1
    assert target.stat().st_nlink == 2
    assert temporary[0].stat().st_ino == target.stat().st_ino

    assert read_json(target) == value
    assert target.stat().st_nlink == 1
    assert _temporary_paths(target) == []


def test_read_json_strict_no_repair_rejects_and_preserves_crash_alias(
    tmp_path: Path,
) -> None:
    target = tmp_path / "record.json"
    value = {"payload": "complete"}
    _crash_after_link(target, value)
    temporary = _temporary_paths(target)
    assert len(temporary) == 1
    alias = temporary[0]
    before = {
        path: (path.lstat().st_ino, path.lstat().st_nlink, path.read_bytes())
        for path in (target, alias)
    }

    with pytest.raises(ValueError, match="single-link"):
        store_module.read_json_strict_no_repair(target)

    assert {
        path: (path.lstat().st_ino, path.lstat().st_nlink, path.read_bytes())
        for path in (target, alias)
    } == before
    assert read_json(target) == value
    assert target.stat().st_nlink == 1
    assert _temporary_paths(target) == []


def test_read_json_rejects_and_preserves_unrelated_hardlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "record.json"
    assert create_json(target, {"payload": "authority"}) is True
    unrelated = tmp_path / "unrelated.json"
    os.link(target, unrelated, follow_symlinks=False)
    digest = hashlib.sha256(os.fsencode(target.name)).hexdigest()
    unrelated_temp = tmp_path / f".aros-json-{digest}.foreign.tmp"
    unrelated_temp.write_text("foreign\n", encoding="utf-8")

    with pytest.raises(ValueError, match="single-link"):
        read_json(target)

    assert target.stat().st_nlink == unrelated.stat().st_nlink == 2
    assert unrelated_temp.read_text(encoding="utf-8") == "foreign\n"


def test_read_json_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "record.json"
    assert create_json(target, {"payload": "authority"}) is True
    alias = tmp_path / "alias.json"
    alias.symlink_to(target.name)

    with pytest.raises(ValueError, match="regular"):
        read_json(alias)

    assert alias.is_symlink()


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b'{"value":1,"value":2}\n', "duplicate"),
        (b'{"value":NaN}\n', "non-finite"),
        (b'{"value":Infinity}\n', "non-finite"),
    ),
)
def test_read_json_strict_rejects_ambiguous_json_without_weakening_secure_read(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    target = tmp_path / "record.json"
    target.write_bytes(payload)

    with pytest.raises(ValueError, match=message):
        store_module.read_json_strict(target)

    alias = tmp_path / "alias.json"
    alias.symlink_to(target.name)
    with pytest.raises(ValueError, match="regular"):
        store_module.read_json_strict(alias)


def test_read_json_strict_rejects_overflowing_float_without_changing_default(
    tmp_path: Path,
) -> None:
    target = tmp_path / "record.json"
    target.write_bytes(b'{"value":1e400}\n')

    default_value = read_json(target)
    assert math.isinf(default_value["value"])
    with pytest.raises(ValueError, match="non-finite"):
        store_module.read_json_strict(target)


@pytest.mark.parametrize("encoding", ("utf-16", "utf-32"))
def test_strict_json_bytes_reject_utf16_and_utf32(encoding: str) -> None:
    payload = '{"value":1}'.encode(encoding)
    assert json.loads(payload) == {"value": 1}

    with pytest.raises(UnicodeDecodeError):
        store_module._strict_json_loads(payload)


def test_strict_json_normalizes_decoder_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recursive_decoder(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("decoder recursion")

    monkeypatch.setattr(store_module.json, "loads", recursive_decoder)

    with pytest.raises(ValueError, match="depth|recursive|recursion"):
        store_module._strict_json_loads("[]")


def test_anchored_json_shape_limits_are_iterative_and_opt_in(
    tmp_path: Path,
) -> None:
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 70 + "0" + "]" * 70, encoding="utf-8")
    wide = tmp_path / "wide.json"
    wide.write_text("[" + ",".join("0" for _ in range(10_001)) + "]", encoding="utf-8")

    with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
        assert isinstance(reader.read_json("deep.json"), list)
        assert isinstance(reader.read_json("wide.json"), list)

    with store_module.AnchoredWorkspaceReader(
        tmp_path,
        max_json_depth=64,
        max_json_nodes=10_000,
    ) as reader:
        with pytest.raises(store_module.AnchoredReadStructureError, match="depth|64"):
            reader.read_json("deep.json")
        with pytest.raises(store_module.AnchoredReadStructureError, match="nodes|10000"):
            reader.read_json("wide.json")


def test_read_json_rejects_path_replaced_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "record.json"
    replacement = tmp_path / "replacement.json"
    assert create_json(target, {"version": 1}) is True
    assert create_json(replacement, {"version": 2}) is True
    real_load = store_module.json.load

    def load_then_replace(handle: object) -> object:
        value = real_load(handle)
        os.replace(replacement, target)
        return value

    monkeypatch.setattr(store_module.json, "load", load_then_replace)

    with pytest.raises(ValueError, match="changed"):
        read_json(target)


def test_anchored_workspace_reader_revalidates_lineage_and_closes_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "record.json"
    replacement = tmp_path / "replacement.json"
    assert create_json(target, {"version": 1}) is True
    assert create_json(replacement, {"version": 1}) is True
    reader_type = getattr(store_module, "AnchoredWorkspaceReader", None)
    assert reader_type is not None
    real_open = store_module.os.open
    opened: list[int] = []

    def recording_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(store_module.os, "open", recording_open)

    with pytest.raises(ValueError, match="changed|identity"):
        with reader_type(tmp_path) as reader:
            assert reader.read_json("record.json") == {"version": 1}
            os.replace(replacement, target)
            reader.revalidate()

    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_anchored_workspace_reader_streams_without_oversized_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * (2 * 1024 * 1024 + 17)
    target = tmp_path / "large.log"
    target.write_bytes(payload)
    real_read = store_module.os.read
    requested: list[int] = []

    def bounded_read(descriptor: int, size: int) -> bytes:
        requested.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(store_module.os, "read", bounded_read)

    with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
        captured = reader.verify_stream(
            "large.log",
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            capture_limit=1024,
        )
        assert captured is None
        assert all(not hasattr(entry, "payload") for entry in reader._files.values())

    assert requested
    assert max(requested) <= 65_536


def test_anchored_json_limit_rejects_before_decode_and_defaults_unbounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "large.json"
    value = {"payload": "x" * 1_000}
    target.write_text(json.dumps(value), encoding="utf-8")

    with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
        assert reader.read_json("large.json") == value

    decoded = False
    real_loads = store_module._strict_json_loads

    def recording_loads(raw: object) -> object:
        nonlocal decoded
        decoded = True
        return real_loads(raw)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "_strict_json_loads", recording_loads)

    with store_module.AnchoredWorkspaceReader(
        tmp_path,
        max_json_bytes=32,
    ) as reader:
        with pytest.raises(store_module.AnchoredReadLimitError, match="JSON|32"):
            reader.read_json("large.json")

    assert decoded is False


def test_anchored_capture_budget_charges_unique_files_and_excludes_verify_stream(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"payload": "a" * 40}), encoding="utf-8")
    second.write_text(json.dumps({"payload": "b" * 40}), encoding="utf-8")
    log = tmp_path / "output.log"
    log.write_bytes(b"z" * 10_000)
    budget = first.stat().st_size + second.stat().st_size - 1

    with store_module.AnchoredWorkspaceReader(
        tmp_path,
        max_capture_bytes=budget,
    ) as reader:
        first_value = reader.read_json("first.json")
        assert reader.read_json("first.json") is first_value
        assert reader.verify_stream(
            "output.log",
            expected_size=log.stat().st_size,
            expected_sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
            capture_limit=None,
        ) is None
        with pytest.raises(store_module.AnchoredReadLimitError, match="aggregate|budget"):
            reader.read_json("second.json")


def test_anchored_stream_rejects_actual_size_before_declared_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "large.log"
    target.write_bytes(b"x" * (2 * 1024 * 1024))
    real_read = store_module.os.read
    read_calls = 0

    def counting_read(descriptor: int, size: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return real_read(descriptor, size)

    monkeypatch.setattr(store_module.os, "read", counting_read)

    with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
        with pytest.raises(store_module.AnchoredReadError, match="size|receipt"):
            reader.verify_stream(
                "large.log",
                expected_size=1,
                expected_sha256="0" * 64,
                capture_limit=1024,
            )
        assert read_calls == 0


def test_anchored_workspace_reader_bounds_peak_descriptors_across_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = tmp_path / "records"
    records.mkdir()
    for index in range(128):
        (records / f"{index:03d}.json").write_text(
            '{"value":1}\n',
            encoding="utf-8",
        )
    real_open = store_module.os.open
    real_close = store_module.os.close
    live: set[int] = set()
    peak = 0

    def tracking_open(*args: object, **kwargs: object) -> int:
        nonlocal peak
        descriptor = real_open(*args, **kwargs)  # type: ignore[arg-type]
        live.add(descriptor)
        peak = max(peak, len(live))
        return descriptor

    def tracking_close(descriptor: int) -> None:
        live.discard(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(store_module.os, "open", tracking_open)
    monkeypatch.setattr(store_module.os, "close", tracking_close)

    with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
        for index in range(128):
            reader.require_file(f"records/{index:03d}.json")
        assert peak <= 16

    assert not live


@pytest.mark.parametrize(
    ("site", "failed_open_index"),
    (("constructor", 1), ("traversal", 0)),
)
def test_anchored_workspace_reader_closes_descriptor_when_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    failed_open_index: int,
) -> None:
    reader = None
    if site == "traversal":
        (tmp_path / "first" / "second").mkdir(parents=True)
        reader = store_module.AnchoredWorkspaceReader(tmp_path)
    real_open = store_module.os.open
    real_fstat = store_module.os.fstat
    opened: list[int] = []
    failed = False

    def tracking_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    def failing_fstat(descriptor: int):  # type: ignore[no-untyped-def]
        nonlocal failed
        if (
            not failed
            and len(opened) > failed_open_index
            and descriptor == opened[failed_open_index]
        ):
            failed = True
            raise OSError("injected fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(store_module.os, "open", tracking_open)
    monkeypatch.setattr(store_module.os, "fstat", failing_fstat)

    with pytest.raises(OSError, match="injected fstat"):
        if site == "constructor":
            store_module.AnchoredWorkspaceReader(tmp_path)
        else:
            assert reader is not None
            reader.require_directory("first/second")
    if reader is not None:
        reader.close()

    assert failed is True
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_anchored_json_reader_preserves_large_strict_reader_compatibility(
    tmp_path: Path,
) -> None:
    target = tmp_path / "large.json"
    value = {"payload": "x" * (1024 * 1024 + 128)}
    assert create_json(target, value) is True
    expected = store_module.read_json_strict_no_repair(target)

    with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
        assert reader.read_json("large.json") == expected


def test_anchored_workspace_reader_exit_revalidates_automatically(
    tmp_path: Path,
) -> None:
    target = tmp_path / "record.json"
    replacement = tmp_path / "replacement.json"
    assert create_json(target, {"version": 1}) is True
    assert create_json(replacement, {"version": 1}) is True

    with pytest.raises(store_module.AnchoredReadError, match="changed|identity"):
        with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
            assert reader.read_json("record.json") == {"version": 1}
            os.replace(replacement, target)


def test_anchored_workspace_reader_preserves_body_and_revalidation_errors(
    tmp_path: Path,
) -> None:
    target = tmp_path / "record.json"
    replacement = tmp_path / "replacement.json"
    assert create_json(target, {"version": 1}) is True
    assert create_json(replacement, {"version": 1}) is True

    class BodyError(RuntimeError):
        pass

    with pytest.raises(store_module.AnchoredReadError) as caught:
        with store_module.AnchoredWorkspaceReader(tmp_path) as reader:
            reader.read_json("record.json")
            os.replace(replacement, target)
            raise BodyError("body failed")

    assert isinstance(caught.value.original_error, BodyError)
    assert isinstance(caught.value.revalidation_error, store_module.AnchoredReadError)


def test_task_execution_claim_read_recovers_crashed_store_alias(
    tmp_path: Path,
) -> None:
    task_id = "TASK-20260803-recovered-execution"
    target = tmp_path / "execution.json"
    brief = {"task_id": task_id, "brief_sha256": "a" * 64}
    ownership = {"ownership_sha256": "b" * 64}
    launch = {"launch_sha256": "c" * 64, "host": "test-host"}
    claim: dict[str, object] = {
        "schema_version": 1,
        "task_id": task_id,
        "brief_sha256": brief["brief_sha256"],
        "ownership_sha256": ownership["ownership_sha256"],
        "launch_sha256": launch["launch_sha256"],
        "host": launch["host"],
        "runner_pid": 123,
        "runner_pgid": 123,
        "runner_start_token": "linux-proc-start:1",
        "claimed_at": "2026-08-03T00:00:00.000Z",
    }
    claim["execution_sha256"] = task_runner_module._record_sha256(
        claim,
        "execution_sha256",
    )

    class Service:
        def _execution_path(self, observed_task_id: str) -> Path:
            assert observed_task_id == task_id
            return target

    _crash_after_link(target, claim)

    assert task_runner_module.load_execution_claim(
        Service(),  # type: ignore[arg-type]
        brief,
        ownership,
        launch,
    ) == claim
    assert target.stat().st_nlink == 1
    assert _temporary_paths(target) == []


def test_create_json_existing_target_skips_temp_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "record.json"
    target.write_text('{"original":true}\n', encoding="utf-8")

    def deny_temp_creation(*args: object, **kwargs: object) -> object:
        raise PermissionError("parent is not writable")

    monkeypatch.setattr(store_module.tempfile, "mkstemp", deny_temp_creation)

    assert create_json(target, {"replacement": True}) is False
    assert target.read_text(encoding="utf-8") == '{"original":true}\n'


@pytest.mark.parametrize("write_json", (create_json, atomic_write_json))
def test_json_writes_support_maximum_length_target_name(
    tmp_path: Path,
    write_json: Callable[[str | Path, object], object],
) -> None:
    target = tmp_path / ("x" * 255)
    if write_json is atomic_write_json:
        target.write_text('{"version":0}\n', encoding="utf-8")

    result = write_json(target, {"version": 1})

    if write_json is create_json:
        assert result is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
    assert _temporary_paths(target) == []
