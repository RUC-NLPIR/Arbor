from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from commissioning.cache_campaign.records import (
    ContractError,
    canonical_bytes,
    load_object,
    record_sha256,
    sha256_file,
    write_new_record,
)
from commissioning.cache_campaign.source import SourceError, prepare_source, validate_source
from scripts import prepare_aros_cache_source as source_cli


ROOT = Path(__file__).resolve().parent.parent
C_COMPILER = "/opt/aros-toolchain/cc"
CXX_COMPILER = "/opt/aros-toolchain/c++"
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

    (checkout / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
    (checkout / ".gitignore").write_text("*_build*\n*.log\n", encoding="utf-8")
    git(checkout, "add", ".gitignore", "CMakeLists.txt")
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
        assert not binary.exists()
        cache = checkout / "_build/CMakeCache.txt"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            f"CMAKE_C_COMPILER:FILEPATH={C_COMPILER}\n"
            f"CMAKE_CXX_COMPILER:FILEPATH={CXX_COMPILER}\n",
            encoding="utf-8",
        )
    elif command == LOCK["build_argv"]:
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"built cache simulator\n")
        binary.chmod(0o755)
    elif command == LOCK["test_argv"]:
        assert binary.read_bytes() == b"built cache simulator\n"

    versions = {
        ("cmake", "--version"): b"cmake version 3.test\n",
        ("ninja", "--version"): b"1.test\n",
        (C_COMPILER, "--version"): b"test C compiler 1.0\nmore C details\n",
        (CXX_COMPILER, "--version"): b"test C++ compiler 2.0\nmore C++ details\n",
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


def test_validate_source_rejects_multiple_origin_fetch_urls(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    expected = str(lock["repository_url"])
    git(checkout, "config", "--unset-all", "remote.origin.url")
    git(checkout, "config", "--add", "remote.origin.url", "https://example.invalid/evil.git")
    git(checkout, "config", "--add", "remote.origin.url", expected)
    assert git(checkout, "config", "--get", "remote.origin.url") == expected

    with pytest.raises(SourceError, match="fetch URL"):
        validate_source(checkout, lock)


def test_validate_source_rejects_origin_pushurl(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    git(
        checkout,
        "config",
        "--add",
        "remote.origin.pushurl",
        "ssh://example.invalid/evil.git",
    )

    with pytest.raises(SourceError, match="push URL"):
        validate_source(checkout, lock)


def test_validate_source_rejects_empty_origin_pushurl(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    git(checkout, "config", "--add", "remote.origin.pushurl", "")
    assert git(checkout, "config", "--get-all", "remote.origin.pushurl") == ""

    with pytest.raises(SourceError, match="push URL"):
        validate_source(checkout, lock)


def test_validate_source_rejects_nested_checkout_path(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    nested = checkout / "nested"
    nested.mkdir()

    with pytest.raises(SourceError, match="top-level"):
        validate_source(nested, lock)


def test_prepare_ignores_git_redirect_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    attacker_root = tmp_path / "attacker"
    attacker_root.mkdir()
    attacker, _ = fake_checkout(attacker_root)
    (attacker / "attacker.txt").write_text("attacker\n", encoding="utf-8")
    git(attacker, "add", "attacker.txt")
    git(attacker, "commit", "-qm", "attacker")
    malicious_environment = {
        "GIT_DIR": str(attacker / ".git"),
        "GIT_WORK_TREE": str(attacker),
        "GIT_INDEX_FILE": str(attacker / ".git/index"),
        "GIT_OBJECT_DIRECTORY": str(attacker / ".git/objects"),
    }
    for key, value in malicious_environment.items():
        monkeypatch.setenv(key, value)

    receipt = prepare_source(checkout, tmp_path / "receipt.json", lock, run=fake_run)

    assert receipt["commit"] == lock["commit"]


def test_validate_source_rejects_replacement_tree(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    (checkout / "CMakeLists.txt").write_text("# replacement source\n", encoding="utf-8")
    git(checkout, "add", "CMakeLists.txt")
    replacement_tree = git(checkout, "write-tree")
    git(checkout, "replace", str(lock["tree"]), replacement_tree)

    assert git(checkout, "rev-parse", "HEAD^{tree}") == lock["tree"]
    assert git(checkout, "status", "--porcelain=v1") == ""
    assert git(checkout, "show", "HEAD:CMakeLists.txt") == "# replacement source"

    with pytest.raises(SourceError, match="dirty"):
        validate_source(checkout, lock)


def test_validate_source_rejects_fsmonitor_hidden_change(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    hook = checkout / ".git/hooks/fake-fsmonitor"
    hook.write_text("#!/bin/sh\nprintf 'token\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    git(checkout, "config", "core.fsmonitor", str(hook))
    git(checkout, "config", "core.fsmonitorHookVersion", "2")
    assert git(checkout, "status", "--porcelain=v1") == ""
    (checkout / "CMakeLists.txt").write_text("# hidden by fsmonitor\n", encoding="utf-8")
    assert git(checkout, "status", "--porcelain=v1") == ""

    with pytest.raises(SourceError, match="dirty"):
        validate_source(checkout, lock)


def test_validate_source_rejects_filemode_hidden_change(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    git(checkout, "config", "core.fileMode", "false")
    (checkout / "CMakeLists.txt").chmod(0o755)
    assert git(checkout, "status", "--porcelain=v1") == ""

    with pytest.raises(SourceError, match="dirty"):
        validate_source(checkout, lock)


def test_validate_source_rejects_minimal_stat_hidden_change(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    source = checkout / "CMakeLists.txt"
    initial = source.stat()
    aged_mtime = initial.st_mtime_ns - 10_000_000_000
    os.utime(source, ns=(initial.st_atime_ns, aged_mtime))
    git(checkout, "config", "core.trustctime", "false")
    git(checkout, "config", "core.checkStat", "minimal")
    assert git(checkout, "status", "--porcelain=v1") == ""
    metadata = source.stat()
    time.sleep(1.1)
    replacement = b"# altered\n"
    assert len(replacement) == metadata.st_size
    source.write_bytes(replacement)
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    with pytest.raises(SourceError, match="dirty"):
        validate_source(checkout, lock)


@pytest.mark.parametrize("relative_path", ["_build/bin/cachesim", "outside.log"])
def test_validate_source_rejects_ignored_untracked_input(
    tmp_path: Path, relative_path: str
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    ignored = checkout / relative_path
    ignored.parent.mkdir(parents=True, exist_ok=True)
    ignored.write_text("ignored input\n", encoding="utf-8")

    with pytest.raises(SourceError, match="dirty"):
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
    assert not (checkout / str(lock["binary"])).exists()

    calls: list[list[str]] = []

    def recording_run(
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return fake_run(
            argv,
            cwd=cwd,
            capture_output=capture_output,
            check=check,
        )

    receipt = prepare_source(checkout, receipt_path, lock, run=recording_run)

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
        "ninja": "1.test",
    }
    assert receipt["compilers"] == {
        "c": {"path": C_COMPILER, "version": "test C compiler 1.0"},
        "cxx": {"path": CXX_COMPILER, "version": "test C++ compiler 2.0"},
    }
    assert [C_COMPILER, "--version"] in calls
    assert [CXX_COMPILER, "--version"] in calls
    assert ["c++", "--version"] not in calls
    assert isinstance(receipt["interpreter"], str)
    assert isinstance(receipt["platform"], str)
    assert receipt["receipt_sha256"] == record_sha256(receipt, "receipt_sha256")
    assert load_object(receipt_path) == receipt


@pytest.mark.parametrize(
    ("cache_text", "missing_name"),
    [
        (f"CMAKE_CXX_COMPILER:FILEPATH={CXX_COMPILER}\n", "CMAKE_C_COMPILER"),
        (f"CMAKE_C_COMPILER:FILEPATH={C_COMPILER}\n", "CMAKE_CXX_COMPILER"),
        (
            "CMAKE_C_COMPILER:FILEPATH=relative-cc\n"
            f"CMAKE_CXX_COMPILER:FILEPATH={CXX_COMPILER}\n",
            "CMAKE_C_COMPILER",
        ),
        (
            f"CMAKE_C_COMPILER:FILEPATH={C_COMPILER}\n"
            "CMAKE_CXX_COMPILER:FILEPATH=relative-cxx\n",
            "CMAKE_CXX_COMPILER",
        ),
    ],
)
def test_prepare_requires_absolute_cmake_compilers(
    tmp_path: Path, cache_text: str, missing_name: str
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    def incomplete_cache_run(
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run(
            argv,
            cwd=cwd,
            capture_output=capture_output,
            check=check,
        )
        if argv == lock["configure_argv"]:
            (checkout / "_build/CMakeCache.txt").write_text(cache_text, encoding="utf-8")
        return result

    with pytest.raises(SourceError, match=missing_name):
        prepare_source(checkout, receipt_path, lock, run=incomplete_cache_run)
    assert not receipt_path.exists()


@pytest.mark.parametrize("mutation", ["non_executable", "symlink_escape"])
def test_prepare_rejects_invalid_binary(
    tmp_path: Path, mutation: str
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    def invalid_binary_run(
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run(
            argv,
            cwd=cwd,
            capture_output=capture_output,
            check=check,
        )
        if argv == lock["test_argv"]:
            binary = checkout / str(lock["binary"])
            if mutation == "non_executable":
                binary.chmod(0o644)
            else:
                outside = tmp_path / "outside-cachesim"
                outside.write_bytes(b"outside simulator\n")
                outside.chmod(0o755)
                binary.unlink()
                binary.symlink_to(outside)
        return result

    with pytest.raises(SourceError, match="binary"):
        prepare_source(checkout, receipt_path, lock, run=invalid_binary_run)
    assert not receipt_path.exists()


@pytest.mark.parametrize(
    "failed_argv",
    [
        LOCK["configure_argv"],
        LOCK["build_argv"],
        LOCK["test_argv"],
        ["cmake", "--version"],
        ["ninja", "--version"],
        [C_COMPILER, "--version"],
        [CXX_COMPILER, "--version"],
    ],
    ids=["configure", "build", "test", "cmake", "ninja", "c-compiler", "cxx-compiler"],
)
def test_prepare_stops_on_failed_command_with_bounded_stderr(
    tmp_path: Path, failed_argv: list[str]
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    calls: list[list[str]] = []

    def failing_run(
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        if argv == failed_argv:
            stderr = b"stage exploded\nsecond line " + b"x" * 10_000
            return subprocess.CompletedProcess(argv, 23, stdout=b"", stderr=stderr)
        return fake_run(
            argv,
            cwd=cwd,
            capture_output=capture_output,
            check=check,
        )

    with pytest.raises(SourceError, match="stage exploded") as captured:
        prepare_source(checkout, receipt_path, lock, run=failing_run)

    assert calls[-1] == failed_argv
    assert "\n" not in str(captured.value)
    assert len(str(captured.value)) < 2048
    assert not receipt_path.exists()


@pytest.mark.parametrize(
    "empty_argv",
    [
        ["cmake", "--version"],
        ["ninja", "--version"],
        [C_COMPILER, "--version"],
        [CXX_COMPILER, "--version"],
    ],
    ids=["cmake", "ninja", "c-compiler", "cxx-compiler"],
)
def test_prepare_rejects_empty_version_output(
    tmp_path: Path, empty_argv: list[str]
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    calls: list[list[str]] = []

    def empty_version_run(
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        if argv == empty_argv:
            return subprocess.CompletedProcess(argv, 0, stdout=b" \n", stderr=b"")
        return fake_run(
            argv,
            cwd=cwd,
            capture_output=capture_output,
            check=check,
        )

    with pytest.raises(SourceError, match="empty version output"):
        prepare_source(checkout, receipt_path, lock, run=empty_version_run)

    assert calls[-1] == empty_argv
    assert not receipt_path.exists()


def test_source_cli_prints_exactly_one_error_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_prepare(checkout: Path, receipt: Path, lock: dict[str, object]) -> None:
        raise SourceError("first failure line\nsecond failure line")

    monkeypatch.setattr(source_cli, "prepare_source", fail_prepare)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_aros_cache_source.py",
            "--checkout",
            str(tmp_path),
            "--receipt",
            str(tmp_path / "receipt.json"),
        ],
    )

    assert source_cli.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["error: first failure line second failure line"]


@pytest.mark.parametrize("mutation_command", ["build_argv", "test_argv"])
def test_prepare_rejects_tracked_mutation_during_commands(
    tmp_path: Path, mutation_command: str
) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "source-receipt.json"

    def mutating_run(
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run(
            argv,
            cwd=cwd,
            capture_output=capture_output,
            check=check,
        )
        if argv == lock[mutation_command]:
            (checkout / "CMakeLists.txt").write_text("# mutated\n", encoding="utf-8")
        return result

    with pytest.raises(SourceError, match="dirty"):
        prepare_source(checkout, receipt_path, lock, run=mutating_run)
    assert not receipt_path.exists()


def test_prepare_rejects_ignored_output_outside_build_directory(tmp_path: Path) -> None:
    checkout, lock = fake_checkout(tmp_path)
    receipt_path = tmp_path / "source-receipt.json"

    def mutating_run(
        argv: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run(
            argv,
            cwd=cwd,
            capture_output=capture_output,
            check=check,
        )
        if argv == lock["test_argv"]:
            (checkout / "outside.log").write_text("ignored output\n", encoding="utf-8")
        return result

    with pytest.raises(SourceError, match="dirty"):
        prepare_source(checkout, receipt_path, lock, run=mutating_run)
    assert not receipt_path.exists()


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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_bytes_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="JSON"):
        canonical_bytes({"value": value})


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_load_object_rejects_non_finite_constants(
    tmp_path: Path, constant: str
) -> None:
    path = tmp_path / "non-finite.json"
    path.write_text(f'{{"value": {constant}}}\n', encoding="utf-8")

    with pytest.raises(ContractError, match="non-finite JSON constant"):
        load_object(path)


@pytest.mark.parametrize("number", ["1e400", "-1e400"])
def test_load_object_rejects_float_overflow(tmp_path: Path, number: str) -> None:
    path = tmp_path / "overflow.json"
    path.write_text(f'{{"value": {number}}}\n', encoding="utf-8")

    with pytest.raises(ContractError, match="non-finite JSON number"):
        load_object(path)


def test_write_new_record_exclusively_creates_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    path.write_text("raced-in record\n", encoding="utf-8")
    original_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda candidate: False if candidate == path else original_exists(candidate),
    )

    with pytest.raises(ContractError, match="refusing to replace immutable record"):
        write_new_record(path, {"schema_version": 1}, "record_sha256")
    assert path.read_text(encoding="utf-8") == "raced-in record\n"


def test_write_new_record_cleans_up_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"

    def fail_fsync(descriptor: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated fsync failure"):
        write_new_record(path, {"schema_version": 1}, "record_sha256")

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_new_record_cleans_up_after_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"

    def fail_link(source: str | bytes, target: str | bytes) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="simulated publish failure"):
        write_new_record(path, {"schema_version": 1}, "record_sha256")

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
