"""Durable AROS store primitive tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO, Callable

import pytest

import arbor.aros.task_runner as task_runner_module
import arbor.aros.store as store_module
from arbor.aros.store import atomic_write_json, create_json, read_json


_CRASH_AFTER_LINK_EXIT = 91


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
