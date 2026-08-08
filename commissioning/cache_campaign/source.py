from __future__ import annotations

import hashlib
import os
import platform
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from . import SCHEMA_VERSION
from .records import ContractError, sha256_file, write_new_record


Run = Callable[..., subprocess.CompletedProcess[bytes]]
_MUTATING_GIT_OPERATIONS = {"clean", "clone", "fetch", "reset"}
_GIT_CONFIG_OVERRIDES = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.fileMode=true",
    "-c",
    "core.trustctime=true",
    "-c",
    "core.checkStat=default",
]


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


def _output_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def _bounded_text(value: bytes | str | None, limit: int = 1024) -> str:
    decoded = _output_bytes(value).decode("utf-8", errors="replace")
    single_line = " ".join(decoded.split())
    if len(single_line) > limit:
        return single_line[:limit] + "..."
    return single_line or "<empty>"


def _git_bytes(
    checkout: Path,
    *argv: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = subprocess.run(
        ["git", *_GIT_CONFIG_OVERRIDES, *argv],
        cwd=checkout,
        capture_output=True,
        check=False,
        env=environment,
    )
    if result.returncode not in allowed_returncodes:
        stderr = _bounded_text(result.stderr)
        raise SourceError(f"Git command failed ({' '.join(argv)}): {stderr}")
    return result.stdout


def _git(
    checkout: Path,
    *argv: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    return _git_bytes(
        checkout,
        *argv,
        allowed_returncodes=allowed_returncodes,
    ).decode("utf-8").strip()


def _normalized_repository_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.rstrip("/")


def _tree_entries(checkout: Path, object_format: str) -> dict[str, tuple[str, str]]:
    oid_length = {"sha1": 40, "sha256": 64}.get(object_format)
    if oid_length is None:
        raise SourceError(f"unsupported Git object format: {object_format}")

    entries: dict[str, tuple[str, str]] = {}
    raw = _git_bytes(checkout, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_type, raw_oid = metadata.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            entry_type = raw_type.decode("ascii")
            oid = raw_oid.decode("ascii")
        except (UnicodeError, ValueError) as error:
            raise SourceError("invalid raw HEAD tree entry") from error
        if mode not in {"100644", "100755", "120000"} or entry_type != "blob":
            raise SourceError(f"unsupported tracked HEAD entry: {record!r}")
        if len(oid) != oid_length or any(character not in "0123456789abcdef" for character in oid):
            raise SourceError(f"invalid tracked HEAD object ID: {oid}")
        path = os.fsdecode(raw_path)
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or path in entries:
            raise SourceError(f"invalid or duplicate tracked HEAD path: {path!r}")
        entries[path] = (mode, oid)
    return entries


def _index_entries(checkout: Path) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    raw = _git_bytes(checkout, "ls-files", "--stage", "-z")
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_oid, raw_stage = metadata.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            oid = raw_oid.decode("ascii")
            stage = raw_stage.decode("ascii")
        except (UnicodeError, ValueError) as error:
            raise SourceError("invalid raw index entry") from error
        path = os.fsdecode(raw_path)
        if stage != "0" or path in entries:
            raise SourceError("source checkout is dirty: index contains conflict stages")
        entries[path] = (mode, oid)
    return entries


def _build_root(checkout: Path, build_directory: str) -> Path:
    root = checkout.joinpath(*PurePosixPath(build_directory).parts)
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
        resolved.relative_to(checkout.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise SourceError("source checkout is dirty: build root escapes the checkout") from error
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        raise SourceError("source checkout is dirty: build root is not a real directory")
    return root


def _filesystem_entries(
    checkout: Path,
    build_directory: str | None,
    tracked_directories: set[str],
) -> dict[str, tuple[Path, os.stat_result]]:
    build_root = _build_root(checkout, build_directory) if build_directory is not None else None
    files: dict[str, tuple[Path, os.stat_result]] = {}
    directories: list[tuple[Path, str]] = [(checkout, "")]
    while directories:
        directory, prefix = directories.pop()
        with os.scandir(directory) as scanner:
            for entry in scanner:
                if not prefix and entry.name == ".git":
                    continue
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                path = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                if build_root is not None and path == build_root:
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in tracked_directories:
                        raise SourceError(
                            f"source checkout is dirty: untracked worktree directory: {relative}"
                        )
                    directories.append((path, relative))
                else:
                    files[relative] = (path, metadata)
    return files


def _blob_oid(raw: bytes, object_format: str) -> str:
    constructor = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}.get(object_format)
    if constructor is None:
        raise SourceError(f"unsupported Git object format: {object_format}")
    header = f"blob {len(raw)}\0".encode("ascii")
    return constructor(header + raw).hexdigest()


def _read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _raw_source_audit(
    checkout: Path,
    build_directory: str | None,
    expected_commit: str,
    expected_tree: str,
    expected_url: str,
) -> bool:
    top_level = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top_level != checkout.resolve(strict=True):
        raise SourceError(f"source top-level mismatch: expected {checkout}, observed {top_level}")

    fetch_urls = _git(checkout, "remote", "get-url", "--all", "origin").splitlines()
    if len(fetch_urls) != 1 or _normalized_repository_url(
        fetch_urls[0]
    ) != _normalized_repository_url(expected_url):
        raise SourceError(
            f"source origin fetch URLs mismatch: expected only {expected_url}, "
            f"observed {fetch_urls}"
        )

    push_url_records = _git(
        checkout,
        "config",
        "--get-regexp",
        r"^remote\.origin\.pushurl$",
        allowed_returncodes=(0, 1),
    )
    if push_url_records:
        raise SourceError(f"source origin has unexpected push URLs: {push_url_records}")

    commit = _git(checkout, "rev-parse", "HEAD")
    if commit != expected_commit:
        raise SourceError(
            f"source HEAD mismatch after diagnostic: expected {expected_commit}, observed {commit}"
        )
    tree = _git(checkout, "rev-parse", "HEAD^{tree}")
    if tree != expected_tree:
        raise SourceError(
            f"source tree mismatch after diagnostic: expected {expected_tree}, observed {tree}"
        )

    index_entries = _git(checkout, "ls-files", "-v", "-z")
    if any(
        entry[:1] == "S" or entry[:1].islower()
        for entry in index_entries.split("\0")
        if entry
    ):
        raise SourceError("source checkout has ambiguous index flags")

    object_format = _git(checkout, "rev-parse", "--show-object-format")
    head = _tree_entries(checkout, object_format)
    index = _index_entries(checkout)
    if index != head:
        raise SourceError("source checkout is dirty: index does not exactly match HEAD")

    tracked_directories: set[str] = set()
    for path in head:
        parent = PurePosixPath(path).parent
        while parent.as_posix() != ".":
            tracked_directories.add(parent.as_posix())
            parent = parent.parent

    filesystem = _filesystem_entries(checkout, build_directory, tracked_directories)
    untracked = sorted(set(filesystem) - set(head))
    if untracked:
        raise SourceError(f"source checkout is dirty: untracked worktree entry: {untracked[0]}")

    for path, (mode, expected_oid) in head.items():
        observed = filesystem.get(path)
        if observed is None:
            raise SourceError(f"source checkout is dirty: tracked worktree entry is missing: {path}")
        local_path, metadata = observed
        if mode in {"100644", "100755"}:
            expected_executable = mode == "100755"
            observed_executable = bool(metadata.st_mode & stat.S_IXUSR)
            if not stat.S_ISREG(metadata.st_mode) or observed_executable != expected_executable:
                raise SourceError(f"source checkout is dirty: tracked worktree mode mismatch: {path}")
            raw = _read_regular(local_path)
        else:
            if not stat.S_ISLNK(metadata.st_mode):
                raise SourceError(f"source checkout is dirty: tracked worktree type mismatch: {path}")
            raw = os.fsencode(os.readlink(local_path))
        if _blob_oid(raw, object_format) != expected_oid:
            raise SourceError(f"source checkout is dirty: tracked worktree bytes mismatch: {path}")
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

    _git(
        checkout,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    return _raw_source_audit(
        checkout,
        build_directory,
        expected_commit,
        expected_tree,
        expected_url,
    )


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


def _cmake_compilers(cache_path: Path) -> dict[str, str]:
    entries: dict[str, list[str]] = {
        "CMAKE_C_COMPILER": [],
        "CMAKE_CXX_COMPILER": [],
    }
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        for name in entries:
            if line.startswith(f"{name}:") and "=" in line:
                entries[name].append(line.split("=", 1)[1])

    compilers: dict[str, str] = {}
    for name, values in entries.items():
        if len(values) != 1 or not Path(values[0]).is_absolute():
            raise SourceError(f"CMake cache requires one absolute {name}")
        compilers[name] = values[0]
    return compilers


def _run_command(run: Run, argv: list[str], checkout: Path) -> subprocess.CompletedProcess[bytes]:
    result = run(argv, cwd=checkout, capture_output=True, check=False)
    if result.returncode != 0:
        raise SourceError(
            f"command failed with exit {result.returncode}: {argv}; "
            f"stderr: {_bounded_text(result.stderr)}"
        )
    return result


def _version(run: Run, argv: list[str], checkout: Path) -> str:
    result = _run_command(run, argv, checkout)
    output = _output_bytes(result.stdout).decode("utf-8", errors="replace").strip()
    if not output:
        raise SourceError(f"empty version output from command: {argv}")
    return output.splitlines()[0][:512]


def _validated_binary(checkout: Path, build_directory: str, locked_binary: str) -> Path:
    binary_relative = PurePosixPath(locked_binary)
    if binary_relative.is_absolute() or ".." in binary_relative.parts:
        raise SourceError("locked binary must be a relative path without parent traversal")

    try:
        checkout_resolved = checkout.resolve(strict=True)
        build_resolved = (checkout / build_directory).resolve(strict=True)
        build_resolved.relative_to(checkout_resolved)
    except (OSError, ValueError) as error:
        raise SourceError("locked binary build directory escapes the checkout") from error

    binary = checkout.joinpath(*binary_relative.parts)
    if binary.is_symlink():
        raise SourceError(f"built cache simulator binary is a symlink: {binary}")
    try:
        binary_resolved = binary.resolve(strict=True)
        binary_resolved.relative_to(build_resolved)
    except (OSError, ValueError) as error:
        raise SourceError(f"built cache simulator binary escapes the build directory: {binary}") from error
    if not binary_resolved.is_file() or not os.access(binary_resolved, os.X_OK):
        raise SourceError(f"built cache simulator binary is not a regular executable: {binary}")
    return binary_resolved


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
    compilers: dict[str, str] | None = None
    for index, argv in enumerate(locked_commands):
        result = _run_command(run, argv, checkout)
        commands.append(
            {
                "argv": argv,
                "returncode": result.returncode,
                "stdout_sha256": hashlib.sha256(_output_bytes(result.stdout)).hexdigest(),
                "stderr_sha256": hashlib.sha256(_output_bytes(result.stderr)).hexdigest(),
            }
        )
        if index == 0:
            compilers = _cmake_compilers(checkout / build_directory / "CMakeCache.txt")

    if compilers is None:
        raise SourceError("configure command did not select compilers")

    clean = _validate_source(checkout, lock, build_directory=build_directory)
    binary = _validated_binary(
        checkout,
        build_directory,
        _lock_string(lock, "binary"),
    )

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
        },
        "compilers": {
            "c": {
                "path": compilers["CMAKE_C_COMPILER"],
                "version": _version(
                    run,
                    [compilers["CMAKE_C_COMPILER"], "--version"],
                    checkout,
                ),
            },
            "cxx": {
                "path": compilers["CMAKE_CXX_COMPILER"],
                "version": _version(
                    run,
                    [compilers["CMAKE_CXX_COMPILER"], "--version"],
                    checkout,
                ),
            },
        },
        "interpreter": sys.version,
        "platform": platform.platform(),
        "binary": _lock_string(lock, "binary"),
        "binary_sha256": sha256_file(binary),
    }
    write_new_record(receipt_path, receipt, "receipt_sha256")
    return receipt
