from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from . import SCHEMA_VERSION
from .records import ContractError, sha256_file, write_new_record


Run = Callable[..., subprocess.CompletedProcess[bytes]]
_MUTATING_GIT_OPERATIONS = {"clean", "clone", "fetch", "reset"}


class SourceError(ValueError):
    pass


def _lock_string(lock: Mapping[str, object], key: str) -> str:
    value = lock.get(key)
    if not isinstance(value, str) or not value:
        raise SourceError(f"source lock requires a nonempty string: {key}")
    return value


def _lock_argv(lock: Mapping[str, object], key: str) -> list[str]:
    value = lock.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise SourceError(f"source lock requires a nonempty argv: {key}")
    argv = list(value)
    if Path(argv[0]).name == "git" and _MUTATING_GIT_OPERATIONS.intersection(argv[1:]):
        raise SourceError(f"checkout-mutating Git command is forbidden: {argv}")
    return argv


def _git(checkout: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=checkout,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise SourceError(f"Git command failed ({' '.join(argv)}): {stderr}")
    return result.stdout.decode("utf-8").strip()


def _normalized_repository_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.rstrip("/")


def _status_allows_only_build_output(status: str, build_directory: str | None) -> bool:
    if not status:
        return True
    if build_directory is None:
        return False
    prefix = f"{build_directory}/"
    for entry in status.splitlines():
        relative = entry[3:].rstrip("/")
        if entry[:3] not in {"?? ", "!! "} or not (
            relative == build_directory or relative.startswith(prefix)
        ):
            return False
    return True


def _validate_source(
    checkout: Path,
    lock: Mapping[str, object],
    *,
    build_directory: str | None,
) -> bool:
    expected_commit = _lock_string(lock, "commit")
    expected_tree = _lock_string(lock, "tree")
    expected_url = _lock_string(lock, "repository_url")

    commit = _git(checkout, "rev-parse", "HEAD")
    if commit != expected_commit:
        raise SourceError(f"source HEAD mismatch: expected {expected_commit}, observed {commit}")

    tree = _git(checkout, "rev-parse", "HEAD^{tree}")
    if tree != expected_tree:
        raise SourceError(f"source tree mismatch: expected {expected_tree}, observed {tree}")

    origin = _git(checkout, "config", "--get", "remote.origin.url")
    if _normalized_repository_url(origin) != _normalized_repository_url(expected_url):
        raise SourceError(f"source origin mismatch: expected {expected_url}, observed {origin}")

    status = _git(
        checkout,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    )
    clean = _status_allows_only_build_output(status, build_directory)
    if not clean:
        raise SourceError("source checkout is dirty")

    index_entries = _git(checkout, "ls-files", "-v", "-z")
    if any(
        entry[:1] == "S" or entry[:1].islower()
        for entry in index_entries.split("\0")
        if entry
    ):
        raise SourceError("source checkout has ambiguous index flags")
    return clean


def validate_source(checkout: Path, lock: Mapping[str, object]) -> None:
    _validate_source(checkout, lock, build_directory=None)


def _locked_build_directory(configure_argv: list[str]) -> str:
    try:
        raw = configure_argv[configure_argv.index("-B") + 1]
    except (IndexError, ValueError) as error:
        raise SourceError("configure argv requires a -B build directory") from error
    path = PurePosixPath(raw)
    if path.is_absolute() or path.as_posix() == "." or ".." in path.parts:
        raise SourceError("configure build directory must be a relative child path")
    return path.as_posix()


def _output_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def _run_command(run: Run, argv: list[str], checkout: Path) -> subprocess.CompletedProcess[bytes]:
    result = run(argv, cwd=checkout, capture_output=True, check=False)
    if result.returncode != 0:
        raise SourceError(f"command failed with exit {result.returncode}: {argv}")
    return result


def _version(run: Run, argv: list[str], checkout: Path) -> str:
    result = _run_command(run, argv, checkout)
    return _output_bytes(result.stdout).decode("utf-8", errors="replace").strip()


def prepare_source(
    checkout: Path,
    receipt_path: Path,
    lock: Mapping[str, object],
    *,
    run: Run = subprocess.run,
) -> dict[str, object]:
    if receipt_path.exists():
        raise ContractError(f"refusing to replace immutable record: {receipt_path}")

    locked_commands = [
        _lock_argv(lock, "configure_argv"),
        _lock_argv(lock, "build_argv"),
        _lock_argv(lock, "test_argv"),
    ]
    build_directory = _locked_build_directory(locked_commands[0])
    validate_source(checkout, lock)

    commands: list[dict[str, object]] = []
    for argv in locked_commands:
        result = _run_command(run, argv, checkout)
        commands.append(
            {
                "argv": argv,
                "returncode": result.returncode,
                "stdout_sha256": hashlib.sha256(_output_bytes(result.stdout)).hexdigest(),
                "stderr_sha256": hashlib.sha256(_output_bytes(result.stderr)).hexdigest(),
            }
        )

    clean = _validate_source(checkout, lock, build_directory=build_directory)
    binary = checkout / _lock_string(lock, "binary")
    if not binary.is_file():
        raise SourceError(f"built cache simulator is missing: {binary}")

    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "repository_url": _lock_string(lock, "repository_url"),
        "commit": _lock_string(lock, "commit"),
        "tree": _lock_string(lock, "tree"),
        "clean": clean,
        "commands": commands,
        "versions": {
            "cmake": _version(run, ["cmake", "--version"], checkout),
            "ninja": _version(run, ["ninja", "--version"], checkout),
            "compiler": _version(run, ["c++", "--version"], checkout),
        },
        "interpreter": sys.version,
        "platform": platform.platform(),
        "binary": _lock_string(lock, "binary"),
        "binary_sha256": sha256_file(binary),
    }
    write_new_record(receipt_path, receipt, "receipt_sha256")
    return receipt
