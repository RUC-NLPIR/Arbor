from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from commissioning.cache_campaign.records import (
    ContractError,
    load_object,
    record_sha256,
    sha256_file,
)
from commissioning.cache_campaign.source import SourceError, prepare_source, validate_source


ROOT = Path(__file__).resolve().parent.parent
LOCK = {
    "schema_version": 1,
    "repository_url": "https://github.com/1a1a11a/libCacheSim.git",
    "commit": "da022c2945146e9577d91375a48d53850d7041a3",
    "tree": "d59c0319fff072788ab5d5a5c1f204f758082c80",
    "configure_argv": [
        "cmake",
        "-S",
        ".",
        "-B",
        "_build",
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DENABLE_TESTS=ON",
    ],
    "build_argv": ["cmake", "--build", "_build", "-j", "8"],
    "test_argv": ["ctest", "--test-dir", "_build", "--output-on-failure"],
    "binary": "_build/bin/cachesim",
    "baseline_policies": ["Sieve", "S3FIFO"],
    "comparison_policies": ["LRU", "ARC", "WTinyLFU", "Sieve", "S3FIFO", "BeladySize"],
}


def git(checkout: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *argv],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def fake_checkout(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    checkout = tmp_path / "libCacheSim"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "Cache Test")
    git(checkout, "config", "user.email", "cache-test@example.invalid")
    git(checkout, "remote", "add", "origin", "https://example.invalid/libCacheSim.git")

    binary = checkout / "_build/bin/cachesim"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"unbuilt cache simulator\n")
    binary.chmod(0o755)
    (checkout / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
    git(checkout, "add", "CMakeLists.txt", "_build/bin/cachesim")
    git(checkout, "commit", "-qm", "fixture")

    lock = copy.deepcopy(LOCK)
    lock["repository_url"] = "https://example.invalid/libCacheSim.git"
    lock["commit"] = git(checkout, "rev-parse", "HEAD")
    lock["tree"] = git(checkout, "rev-parse", "HEAD^{tree}")
    return checkout, lock


def mutate(checkout: Path, lock: dict[str, object], mutation: str) -> None:
    if mutation == "wrong_head":
        (checkout / "next.txt").write_text("next\n", encoding="utf-8")
        git(checkout, "add", "next.txt")
        git(checkout, "commit", "-qm", "next")
    elif mutation == "dirty":
        (checkout / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    elif mutation == "wrong_remote":
        git(checkout, "remote", "set-url", "origin", "https://example.invalid/wrong.git")
    elif mutation == "wrong_tree":
        lock["tree"] = "0" * 40
    else:
        raise AssertionError(f"unknown mutation: {mutation}")


def fake_run(
    argv: list[str],
    *,
    cwd: Path,
    capture_output: bool,
    check: bool,
) -> subprocess.CompletedProcess[bytes]:
    assert capture_output is True
    assert check is False
    checkout = Path(cwd)
    binary = checkout / "_build/bin/cachesim"
    command = list(argv)

    if command == LOCK["configure_argv"]:
        assert binary.read_bytes() == b"unbuilt cache simulator\n"
    elif command == LOCK["build_argv"]:
        binary.write_bytes(b"built cache simulator\n")
        binary.chmod(0o755)
    elif command == LOCK["test_argv"]:
        assert binary.read_bytes() == b"built cache simulator\n"

    versions = {
        ("cmake", "--version"): b"cmake version 3.test\n",
        ("ninja", "--version"): b"1.test\n",
        ("c++", "--version"): b"test compiler 1.0\n",
    }
    stdout = versions.get(tuple(command), b"command output\n")
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")


def test_source_lock_is_exact() -> None:
    lock = load_object(ROOT / "commissioning/cache_campaign/source.lock.json")
    assert lock == LOCK


@pytest.mark.parametrize("mutation", ["wrong_head", "dirty", "wrong_remote", "wrong_tree"])
def test_validate_source_rejects_unbound_checkout(tmp_path: Path, mutation: str) -> None:
    checkout, lock = fake_checkout(tmp_path)
    mutate(checkout, lock, mutation)
    with pytest.raises(SourceError):
        validate_source(checkout, lock)


def test_validate_source_accepts_normalized_origin_url(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    lock["repository_url"] = "https://example.invalid/libCacheSim"
    validate_source(checkout, lock)


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_validate_source_rejects_ambiguous_index_flags(
    tmp_path: Path, index_flag: str
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    git(checkout, "update-index", index_flag, "CMakeLists.txt")
    (checkout / "CMakeLists.txt").write_text("# hidden mutation\n", encoding="utf-8")
    assert git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""

    with pytest.raises(SourceError, match="ambiguous index flags"):
        validate_source(checkout, lock)


def test_prepare_records_commands_versions_and_binary_hash(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "source-receipt.json"
    assert not receipt_path.exists()

    receipt = prepare_source(checkout, receipt_path, lock, run=fake_run)

    assert receipt["commit"] == lock["commit"]
    assert receipt["tree"] == lock["tree"]
    assert receipt["clean"] is True
    assert receipt["binary_sha256"] == sha256_file(checkout / str(lock["binary"]))
    assert [item["argv"] for item in receipt["commands"]] == [
        lock["configure_argv"],
        lock["build_argv"],
        lock["test_argv"],
    ]
    assert all(item["returncode"] == 0 for item in receipt["commands"])
    assert all(len(item["stdout_sha256"]) == 64 for item in receipt["commands"])
    assert all(len(item["stderr_sha256"]) == 64 for item in receipt["commands"])
    assert receipt["versions"] == {
        "cmake": "cmake version 3.test",
        "compiler": "test compiler 1.0",
        "ninja": "1.test",
    }
    assert isinstance(receipt["interpreter"], str)
    assert isinstance(receipt["platform"], str)
    assert receipt["receipt_sha256"] == record_sha256(receipt, "receipt_sha256")
    assert load_object(receipt_path) == receipt


def test_prepare_refuses_existing_receipt(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "source-receipt.json"
    receipt_path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(ContractError, match="refusing to replace immutable record"):
        prepare_source(checkout, receipt_path, lock, run=fake_run)
    assert receipt_path.read_text(encoding="utf-8") == "existing\n"


@pytest.mark.parametrize("operation", ["clone", "fetch", "reset", "clean"])
def test_prepare_rejects_checkout_mutating_git_commands(tmp_path: Path, operation: str) -> None:
    checkout, lock = fake_checkout(tmp_path)
    lock["configure_argv"] = ["git", operation]

    with pytest.raises(SourceError, match="checkout-mutating Git command"):
        prepare_source(checkout, tmp_path / "receipt.json", lock, run=fake_run)


def test_load_object_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"key": 1, "key": 2}\n', encoding="utf-8")

    with pytest.raises(ContractError, match="duplicate JSON key: key"):
        load_object(path)
