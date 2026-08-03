"""Pure receipt hashing compatibility tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock

import arbor.aros.runner as runner_module
import arbor.aros.runs as runs_module
import arbor.aros.tasks as tasks_module
import pytest
from arbor.aros.receipts import content_receipt, digest_chunks, record_sha256
from arbor.aros.store import json_sha256


def test_record_sha256_excludes_only_named_hash_field() -> None:
    record = {"schema_version": 1, "value": "x", "record_sha256": "old"}

    assert record_sha256(record, "record_sha256") == json_sha256(
        {"schema_version": 1, "value": "x"}
    )


def test_content_receipt_preserves_existing_shape() -> None:
    digest = hashlib.sha256(b"abc").hexdigest()

    assert content_receipt("stdout.log", 3, digest) == {
        "path": "stdout.log",
        "bytes": 3,
        "sha256": digest,
    }


def test_digest_chunks_hashes_content_without_joining() -> None:
    assert digest_chunks([b"ab", b"c"]) == (
        3,
        hashlib.sha256(b"abc").hexdigest(),
    )


def test_run_receipt_wrappers_preserve_final_shape_and_self_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_record_sha256 = Mock(wraps=record_sha256)
    observed_digest_chunks = Mock(wraps=digest_chunks)
    observed_content_receipt = Mock(wraps=content_receipt)

    monkeypatch.setattr(
        runs_module, "record_sha256", observed_record_sha256, raising=False
    )
    monkeypatch.setattr(
        runner_module, "digest_chunks", observed_digest_chunks, raising=False
    )
    monkeypatch.setattr(
        runner_module,
        "content_receipt",
        observed_content_receipt,
        raising=False,
    )

    log_path = tmp_path / "stdout.log"
    log_path.write_bytes(b"run receipt\n")
    relative = ".aros/runs/RUN-test/stdout.log"
    legacy_output = runner_module._file_receipt(log_path, relative)
    byte_count, sha256 = digest_chunks([log_path.read_bytes()])
    shared_output = content_receipt(relative, byte_count, sha256)
    legacy_final = {
        "schema_version": 1,
        "state": "completed",
        "stdout": legacy_output,
    }
    shared_final = {**legacy_final, "stdout": shared_output}
    receipt = {
        "schema_version": 1,
        "kind": "run_prelaunch",
        "receipt_sha256": "old",
    }

    assert legacy_final == shared_final
    assert runs_module._receipt_sha256(receipt) == record_sha256(
        receipt, "receipt_sha256"
    )
    observed_record_sha256.assert_called_once_with(receipt, "receipt_sha256")
    assert observed_digest_chunks.call_count == 1
    observed_content_receipt.assert_called_once_with(relative, byte_count, sha256)


def test_task_receipt_wrappers_preserve_final_dictionary_and_self_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_record_sha256 = Mock(wraps=record_sha256)
    observed_digest_chunks = Mock(wraps=digest_chunks)
    observed_content_receipt = Mock(wraps=content_receipt)

    monkeypatch.setattr(
        tasks_module, "record_sha256", observed_record_sha256, raising=False
    )
    monkeypatch.setattr(
        tasks_module, "digest_chunks", observed_digest_chunks, raising=False
    )
    monkeypatch.setattr(
        tasks_module, "content_receipt", observed_content_receipt, raising=False
    )

    log_path = tmp_path / "stdout.log"
    log_path.write_bytes(b"task receipt\n")
    log_path.chmod(0o600)
    relative = ".aros/tasks/TASK-test/stdout.log"
    legacy_output = tasks_module._file_receipt(
        log_path,
        relative,
        permissions_enforced=True,
    )
    byte_count, sha256 = digest_chunks([log_path.read_bytes()])
    shared_output = content_receipt(relative, byte_count, sha256)
    legacy_final = {
        "schema_version": 1,
        "state": "completed",
        "stdout": legacy_output,
    }
    legacy_final["final_sha256"] = tasks_module._record_sha256(
        legacy_final, "final_sha256"
    )
    shared_final = {**legacy_final, "stdout": shared_output}
    shared_final["final_sha256"] = record_sha256(shared_final, "final_sha256")

    assert legacy_final == shared_final
    observed_record_sha256.assert_called_once_with(legacy_final, "final_sha256")
    assert observed_digest_chunks.call_count == 1
    observed_content_receipt.assert_called_once_with(relative, byte_count, sha256)
