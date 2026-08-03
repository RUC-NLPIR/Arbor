"""Durable AROS store primitive tests."""

from __future__ import annotations

import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO, Callable

import pytest

import arbor.aros.store as store_module
from arbor.aros.store import atomic_write_json, create_json, read_json


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

    assert read_json(target) == value
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".record.json.*.tmp")) == []


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
    assert list(tmp_path.glob(".record.json.*.tmp")) == []


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
    assert list(target.parent.glob(".record.json.*.tmp")) == []
