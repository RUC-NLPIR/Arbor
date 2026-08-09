from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from .cachesim import ChildResult, parse_cachesim_output, run_child
from .records import (
    load_object,
    record_sha256,
    sha256_file,
    write_new_record,
)
from .scope import PolicyContract, ScopeFacts, evaluate_scope
from .source import validate_source


SOURCE_LOCK = load_object(Path(__file__).with_name("source.lock.json"))
Run = Callable[..., ChildResult]
_ORACLE = struct.Struct("<IQIq")
_REQUEST_COUNT = 10_000
_SEED = 0xA205_2026
_SANITIZER = re.compile(
    rb"(?:AddressSanitizer|UndefinedBehaviorSanitizer|LeakSanitizer|"
    rb"runtime error:|SUMMARY: [^\r\n]*Sanitizer)",
    re.IGNORECASE,
)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_CONFIG = [
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


class EvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class _Binding:
    head: str
    tree: str
    origin: bytes
    push_urls: bytes
    index: bytes
    index_flags: bytes
    tracked: tuple[tuple[str, str, str], ...]


@dataclass
class _Invocation:
    record: dict[str, object]
    result: ChildResult | None
    stdout: bytes
    stderr: bytes
    stdout_identity: tuple[int, int] | None
    stderr_identity: tuple[int, int] | None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.returncode == 0


@dataclass(frozen=True)
class _EvidenceExpectation:
    path: str
    identity: tuple[int, int] | None
    size_bytes: int | None
    sha256: str | None


def _command_outcome(invocation: _Invocation) -> bool | None:
    if invocation.result is None:
        return None
    return invocation.result.returncode == 0


def _combined_outcome(*values: bool | None) -> bool | None:
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def _clear_unavailable_checks(checks: dict[str, bool | None]) -> None:
    for key in (
        "build",
        "full_tests",
        "candidate_test",
        "sanitizer",
        "deterministic",
        "capacity",
        "metadata_probe",
    ):
        checks[key] = None


def _bounded(value: object, limit: int = 512) -> str:
    message = " ".join(str(value).split()) or value.__class__.__name__
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _git_result(checkout: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        ["git", *_GIT_CONFIG, *argv],
        cwd=checkout,
        capture_output=True,
        check=False,
        env=environment,
    )


def _git_bytes(checkout: Path, *argv: str) -> bytes:
    result = _git_result(checkout, *argv)
    if result.returncode != 0:
        raise EvaluationError(
            f"Git command failed ({' '.join(argv)}): "
            f"{_bounded(result.stderr.decode('utf-8', errors='replace'), 300)}"
        )
    return result.stdout


def _git(checkout: Path, *argv: str) -> str:
    try:
        return _git_bytes(checkout, *argv).decode("utf-8").strip()
    except UnicodeError as error:
        raise EvaluationError("Git binding output is not UTF-8") from error


def _regular_bytes(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvaluationError(f"expected a regular file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _regular_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise EvaluationError(f"expected a regular non-symlink file: {path}")
    return metadata.st_dev, metadata.st_ino


def _refresh_file_record(
    path: Path,
    record: dict[str, object],
    identity: tuple[int, int],
) -> bool:
    initial_size = record.get("size_bytes")
    initial_sha256 = record.get("sha256")
    try:
        raw = _regular_bytes(path)
        observed_identity = _regular_identity(path)
        observed_size: int | None = len(raw)
        observed_sha256: str | None = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        observed_identity = None
        observed_size = None
        observed_sha256 = None
    intact = (
        observed_identity == identity
        and observed_size == initial_size
        and observed_sha256 == initial_sha256
    )
    record["binding_intact"] = intact
    if not intact:
        record["initial_size_bytes"] = initial_size
        record["initial_sha256"] = initial_sha256
        record["size_bytes"] = observed_size
        record["sha256"] = observed_sha256
    return intact


def _capture_expected_evidence(path: Path, stage: Path) -> _EvidenceExpectation:
    relative = str(path.relative_to(stage))
    try:
        raw = _regular_bytes(path)
        identity = _regular_identity(path)
    except (OSError, ValueError):
        return _EvidenceExpectation(relative, None, None, None)
    return _EvidenceExpectation(
        relative,
        identity,
        len(raw),
        hashlib.sha256(raw).hexdigest(),
    )


def _checkout_path(path: Path) -> Path:
    candidate = Path(path).absolute()
    try:
        metadata = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise EvaluationError("checkout must be a real directory")
        return candidate.resolve(strict=True)
    except EvaluationError:
        raise
    except OSError as error:
        raise EvaluationError("checkout must exist") from error


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _output_path(path: Path, checkout: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise EvaluationError("output must be absolute")
    if candidate.name in {"", ".", ".."}:
        raise EvaluationError("output must name a new directory")
    parent = candidate.parent
    try:
        metadata = parent.lstat()
        if parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise EvaluationError("output parent must be a real directory")
        resolved_parent = parent.resolve(strict=True)
    except EvaluationError:
        raise
    except OSError as error:
        raise EvaluationError("output parent must exist") from error
    resolved = (resolved_parent / candidate.name).resolve(strict=False)
    if _paths_overlap(checkout, resolved):
        raise EvaluationError("checkout and output paths must not overlap")
    if os.path.lexists(candidate) or os.path.lexists(resolved):
        raise EvaluationError(f"output must not exist: {resolved}")
    return resolved


def _stage_directory(output: Path) -> tuple[Path, tuple[int, int]]:
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output.name}-stage-", dir=output.parent)
    )
    stage.chmod(0o700)
    metadata = stage.lstat()
    return stage, (metadata.st_dev, metadata.st_ino)


def _directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise EvaluationError(f"apparatus directory is not real: {path}")
    return metadata.st_dev, metadata.st_ino


def _cleanup_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        observed = _directory_identity(path)
    except FileNotFoundError:
        return
    if observed != identity:
        raise EvaluationError(f"refusing to remove replaced apparatus directory: {path}")
    shutil.rmtree(path)


def _publish_stage(stage: Path, identity: tuple[int, int], output: Path) -> None:
    if _directory_identity(stage) != identity:
        raise EvaluationError("evaluation stage changed before publication")
    if os.path.lexists(output):
        raise EvaluationError(f"refusing to replace output directory: {output}")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise EvaluationError("atomic no-replace publication is unavailable") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(stage), -100, os.fsencode(output), 1)
    if result != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise EvaluationError(f"refusing to replace output directory: {output}")
        raise OSError(number, os.strerror(number), output)
    descriptor = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path: Path, raw: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def generate_synthetic_trace(path: Path) -> dict[str, object]:
    """Create the fixed apparatus-owned 10,000-record OracleGeneral trace."""
    state = _SEED
    objects: list[int] = []
    for _index in range(_REQUEST_COUNT):
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFF_FFFF
        objects.append(1 + ((state >> 8) % 512))
    next_access = [-1] * _REQUEST_COUNT
    following: dict[int, int] = {}
    for index in range(_REQUEST_COUNT - 1, -1, -1):
        object_id = objects[index]
        next_access[index] = following.get(object_id, -1)
        following[object_id] = index + 1
    raw = bytearray()
    sizes: dict[int, int] = {}
    for index, object_id in enumerate(objects, start=1):
        size = 64 * (1 + object_id % 4)
        sizes[object_id] = size
        raw.extend(_ORACLE.pack(index - 1, object_id, size, next_access[index - 1]))
    _write_private(Path(path), bytes(raw))
    return {
        "classification": "pre_registered_synthetic_unit_data",
        "record_layout": "<IQIq",
        "request_count": _REQUEST_COUNT,
        "seed": _SEED,
        "generator": "lcg32-numerical-recipes",
        "distribution": "object_id=1+((state>>8)%512); size=64*(1+object_id%4)",
        "next_access_vtime": "one_based_future_request_or_minus_one",
        "working_set_bytes": sum(sizes.values()),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _printable_output(raw: str, label: str) -> list[str]:
    if type(raw) is not str or any(
        character != "\n" and not 0x20 <= ord(character) <= 0x7E
        for character in raw
    ):
        raise EvaluationError(f"{label} output must be printable ASCII and LF")
    return raw.splitlines()


def parse_capacity_probe(output: str) -> dict[str, int | bool]:
    lines = _printable_output(output, "capacity probe")
    pattern = re.compile(r"([a-z_]+)=([0-9]+)\Z")
    values: dict[str, int] = {}
    for line in lines:
        match = pattern.fullmatch(line)
        if match is None or match.group(1) in values:
            raise EvaluationError("malformed capacity probe output")
        values[match.group(1)] = int(match.group(2))
    expected = {
        "capacity_conserved",
        "requests",
        "max_occupied_bytes",
        "cache_size_bytes",
    }
    if set(values) != expected:
        raise EvaluationError("capacity probe fields mismatch")
    if (
        values["capacity_conserved"] != 1
        or values["requests"] != _REQUEST_COUNT
        or values["cache_size_bytes"] <= 0
        or not 0 <= values["max_occupied_bytes"] <= values["cache_size_bytes"]
    ):
        raise EvaluationError("capacity probe reported a capacity violation")
    return {
        "capacity_conserved": True,
        "requests": values["requests"],
        "max_occupied_bytes": values["max_occupied_bytes"],
        "cache_size_bytes": values["cache_size_bytes"],
    }


def parse_metadata_probe(output: str) -> tuple[Decimal, int]:
    lines = _printable_output(output, "metadata probe")
    if len(lines) != 5 or lines[-1] != "status=ok":
        raise EvaluationError("allocation-accounting metadata probe failed")
    global_match = re.fullmatch(r"global_metadata_bytes=([0-9]+)", lines[0])
    if global_match is None:
        raise EvaluationError("malformed global metadata measurement")
    global_bytes = int(global_match.group(1))
    sample_pattern = re.compile(
        r"sample=([0-9]+) live_bytes=([0-9]+) resident_objects=([0-9]+)\Z"
    )
    expected_points = (1_000, 5_000, 10_000)
    measurements: list[Decimal] = []
    for line, expected_point in zip(lines[1:4], expected_points, strict=True):
        match = sample_pattern.fullmatch(line)
        if match is None or int(match.group(1)) != expected_point:
            raise EvaluationError("malformed metadata sample")
        live_bytes = int(match.group(2))
        resident = int(match.group(3))
        if live_bytes < global_bytes or resident <= 0:
            raise EvaluationError("invalid allocation-accounting metadata sample")
        try:
            measurements.append(
                Decimal(live_bytes - global_bytes) / Decimal(resident)
            )
        except (InvalidOperation, ZeroDivisionError) as error:
            raise EvaluationError("invalid exact metadata arithmetic") from error
    return max(measurements), global_bytes


def _candidate_ctest_passed(invocation: _Invocation, policy: str) -> bool:
    if not invocation.ok:
        return False
    try:
        output = invocation.stdout.decode("ascii")
    except UnicodeError:
        return False
    test_name = f"test_{policy}"
    return (
        "No tests were found" not in output
        and re.search(rf"\bStart\s+[0-9]+:\s+{re.escape(test_name)}\b", output)
        is not None
        and re.search(
            rf"\b1/1\s+Test\s+#[0-9]+:\s+{re.escape(test_name)}\s+.*\bPassed\b",
            output,
        )
        is not None
        and "100% tests passed, 0 tests failed out of 1" in output
    )


def _tracked_snapshot(checkout: Path) -> tuple[tuple[str, str, str], ...]:
    raw_paths = _git_bytes(checkout, "ls-files", "-z")
    entries: list[tuple[str, str, str]] = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = os.fsdecode(raw_path)
            path = checkout.joinpath(*PurePosixPath(relative).parts)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                mode = "symlink"
                value = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                mode = "executable" if metadata.st_mode & stat.S_IXUSR else "regular"
                value = _regular_bytes(path)
            else:
                raise EvaluationError(f"unsupported tracked source entry: {relative}")
        except (OSError, ValueError) as error:
            raise EvaluationError("cannot snapshot tracked source bytes") from error
        entries.append((relative, mode, hashlib.sha256(value).hexdigest()))
    return tuple(sorted(entries))


def _binding(checkout: Path) -> _Binding:
    push_result = _git_result(
        checkout, "config", "--get-regexp", r"^remote\.origin\.pushurl$"
    )
    if push_result.returncode not in {0, 1}:
        raise EvaluationError("cannot audit candidate origin push URLs")
    return _Binding(
        head=_git(checkout, "rev-parse", "HEAD"),
        tree=_git(checkout, "rev-parse", "HEAD^{tree}"),
        origin=_git_bytes(checkout, "remote", "get-url", "--all", "origin"),
        push_urls=push_result.stdout,
        index=_git_bytes(checkout, "ls-files", "--stage", "-z"),
        index_flags=_git_bytes(checkout, "ls-files", "-v", "-z"),
        tracked=_tracked_snapshot(checkout),
    )


def _audit_filesystem(
    checkout: Path,
    tracked: tuple[tuple[str, str, str], ...],
    allowed_roots: Sequence[Path],
) -> None:
    tracked_files = {path for path, _mode, _digest in tracked}
    tracked_directories: set[str] = set()
    for relative in tracked_files:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            tracked_directories.add(parent.as_posix())
            parent = parent.parent
    allowed: set[str] = set()
    for root in allowed_roots:
        try:
            relative = Path(root).absolute().relative_to(checkout).as_posix()
        except ValueError:
            continue
        if relative != ".":
            allowed.add(relative)

    directories: list[tuple[Path, str]] = [(checkout, "")]
    while directories:
        directory, prefix = directories.pop()
        with os.scandir(directory) as scanner:
            for entry in scanner:
                if not prefix and entry.name == ".git":
                    continue
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if relative in allowed:
                    continue
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in tracked_directories:
                        raise EvaluationError(
                            f"candidate source gained an untracked directory: {relative}"
                        )
                    directories.append((Path(entry.path), relative))
                elif relative not in tracked_files:
                    raise EvaluationError(
                        f"candidate source gained an untracked entry: {relative}"
                    )


def _post_binding(
    checkout: Path,
    expected: _Binding,
    *,
    allowed_roots: Sequence[Path] = (),
) -> None:
    observed = _binding(checkout)
    if observed != expected:
        raise EvaluationError("candidate source binding mutated during evaluation")
    _audit_filesystem(checkout, expected.tracked, allowed_roots)


def _source_receipt(path: Path, lock: Mapping[str, object]) -> dict[str, object]:
    raw_path = Path(path)
    try:
        metadata = raw_path.lstat()
        if raw_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise EvaluationError("source receipt must be a regular non-symlink file")
        resolved = raw_path.resolve(strict=True)
    except EvaluationError:
        raise
    except OSError as error:
        raise EvaluationError("source receipt is missing") from error
    receipt = load_object(resolved)
    expected_keys = {
        "schema_version",
        "repository_url",
        "commit",
        "tree",
        "clean",
        "commands",
        "versions",
        "compilers",
        "interpreter",
        "platform",
        "binary",
        "binary_sha256",
        "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise EvaluationError("source receipt keys do not match the Task 1 receipt")
    digest = receipt.get("receipt_sha256")
    if (
        type(digest) is not str
        or _HEX64.fullmatch(digest) is None
        or digest != record_sha256(receipt, "receipt_sha256")
    ):
        raise EvaluationError("source receipt self-hash mismatch")
    exact = {
        "schema_version": 1,
        "repository_url": lock.get("repository_url"),
        "commit": lock.get("commit"),
        "tree": lock.get("tree"),
        "binary": lock.get("binary"),
    }
    if any(receipt.get(key) != value for key, value in exact.items()):
        raise EvaluationError("source receipt does not match the pinned source lock")
    if receipt.get("clean") is not True:
        raise EvaluationError("source receipt does not record a clean source")
    binary_sha256 = receipt.get("binary_sha256")
    if type(binary_sha256) is not str or _HEX64.fullmatch(binary_sha256) is None:
        raise EvaluationError("source receipt binary binding is invalid")
    commands = receipt.get("commands")
    expected_argv = [
        lock.get("configure_argv"),
        lock.get("build_argv"),
        lock.get("test_argv"),
    ]
    if not isinstance(commands, list) or len(commands) != 3:
        raise EvaluationError("source receipt command binding is invalid")
    for item, argv in zip(commands, expected_argv, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"argv", "returncode", "stdout_sha256", "stderr_sha256"}
            or item.get("argv") != argv
            or type(item.get("returncode")) is not int
            or item.get("returncode") != 0
            or type(item.get("stdout_sha256")) is not str
            or _HEX64.fullmatch(item["stdout_sha256"]) is None
            or type(item.get("stderr_sha256")) is not str
            or _HEX64.fullmatch(item["stderr_sha256"]) is None
        ):
            raise EvaluationError("source receipt command binding is invalid")
    compilers = receipt.get("compilers")
    if not isinstance(compilers, dict) or set(compilers) != {"c", "cxx"}:
        raise EvaluationError("source receipt C compiler binding is missing")
    for language in ("c", "cxx"):
        compiler_record = compilers.get(language)
        if not isinstance(compiler_record, dict) or set(compiler_record) != {
            "path",
            "version",
        }:
            raise EvaluationError("source receipt compiler binding is invalid")
        compiler_path = compiler_record.get("path")
        compiler_version = compiler_record.get("version")
        if (
            type(compiler_path) is not str
            or not Path(compiler_path).is_absolute()
            or type(compiler_version) is not str
            or not compiler_version
        ):
            raise EvaluationError("source receipt compiler binding is invalid")
    versions = receipt.get("versions")
    if (
        not isinstance(versions, dict)
        or set(versions) != {"cmake", "ninja"}
        or any(type(value) is not str or not value for value in versions.values())
        or type(receipt.get("interpreter")) is not str
        or not receipt["interpreter"]
        or type(receipt.get("platform")) is not str
        or not receipt["platform"]
    ):
        raise EvaluationError("source receipt tool binding is invalid")
    receipt["_resolved_path"] = str(resolved)
    receipt["_file_sha256"] = sha256_file(resolved)
    return receipt


def _preflight(
    checkout: Path,
    base: str,
    candidate: str,
    policy: str,
    source_receipt: Path,
    progress: dict[str, object],
) -> tuple[
    dict[str, object],
    str,
    ScopeFacts,
    PolicyContract | None,
    _Binding,
]:
    try:
        root = Path(checkout).resolve(strict=True)
        if Path(checkout).is_symlink() or not root.is_dir():
            raise EvaluationError("checkout must be a real directory")
    except EvaluationError:
        raise
    except OSError as error:
        raise EvaluationError("checkout must exist") from error
    locked_base = SOURCE_LOCK.get("commit")
    locked_tree = SOURCE_LOCK.get("tree")
    if base != locked_base or _HEX40.fullmatch(base) is None:
        raise EvaluationError("base must equal the exact pinned source commit")
    if candidate != _git(root, "rev-parse", "HEAD"):
        raise EvaluationError("candidate checkout HEAD mismatch")
    if _HEX40.fullmatch(candidate) is None:
        raise EvaluationError("candidate must be a lowercase SHA-1 commit")
    if _git(root, "rev-parse", f"{base}^{{tree}}") != locked_tree:
        raise EvaluationError("base tree does not equal the pinned source tree")
    ancestry = _git_result(root, "merge-base", "--is-ancestor", base, candidate)
    if ancestry.returncode != 0:
        raise EvaluationError("candidate is not a descendant of the pinned base")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    progress["candidate_tree"] = tree
    candidate_lock = {
        "commit": candidate,
        "tree": tree,
        "repository_url": SOURCE_LOCK.get("repository_url"),
    }
    try:
        validate_source(root, candidate_lock)
    except (OSError, ValueError) as error:
        raise EvaluationError(_bounded(error)) from error
    for name in ("_build-release", "_build-sanitize"):
        if os.path.lexists(root / name):
            raise EvaluationError(f"clean evaluation build directory must be absent: {name}")
    source = _source_receipt(source_receipt, SOURCE_LOCK)
    progress["source_binding"] = True
    progress["source_receipt"] = source
    facts, contract = evaluate_scope(
        root,
        base=base,
        candidate=candidate,
        policy=policy,
    )
    progress["scope"] = facts
    progress["contract"] = contract
    structurally_valid = (
        facts.allowed_paths
        and facts.baseline_unchanged
        and facts.additive_wiring_only
        and (facts.contract_bound is True if candidate != base else facts.contract_bound is None)
    )
    if not structurally_valid:
        raise EvaluationError("candidate scope or policy contract is invalid")
    return source, tree, facts, contract, _binding(root)


def _command_record(
    stage: Path,
    index: int,
    label: str,
    argv: Sequence[str],
    cwd: Path,
    run: Run,
) -> _Invocation:
    output = stage / "commands" / f"{index:02d}-{label}"
    output.parent.mkdir(exist_ok=True)
    command = list(argv)
    try:
        result = run(command, output, cwd=cwd)
        if not isinstance(result, ChildResult):
            raise EvaluationError("command runner returned an invalid process receipt")
        if result.argv != tuple(command):
            raise EvaluationError("command runner argv receipt mismatch")
        expected_stdout = output / "stdout.raw"
        expected_stderr = output / "stderr.raw"
        if result.stdout_path != expected_stdout or result.stderr_path != expected_stderr:
            raise EvaluationError("command runner raw output path mismatch")
        stdout = _regular_bytes(expected_stdout)
        stderr = _regular_bytes(expected_stderr)
        stdout_identity = _regular_identity(expected_stdout)
        stderr_identity = _regular_identity(expected_stderr)
        if (
            len(stdout) != result.stdout_bytes
            or len(stderr) != result.stderr_bytes
            or hashlib.sha256(stdout).hexdigest() != result.stdout_sha256
            or hashlib.sha256(stderr).hexdigest() != result.stderr_sha256
        ):
            raise EvaluationError("command runner raw output receipt mismatch")
        record: dict[str, object] = {
            "index": index,
            "label": label,
            "argv": command,
            "cwd": str(cwd),
            "returncode": result.returncode,
            "wall_ns": result.wall_ns,
            "cpu_ns": result.cpu_ns,
            "stdout": {
                "path": str(expected_stdout.relative_to(stage)),
                "size_bytes": result.stdout_bytes,
                "sha256": result.stdout_sha256,
            },
            "stderr": {
                "path": str(expected_stderr.relative_to(stage)),
                "size_bytes": result.stderr_bytes,
                "sha256": result.stderr_sha256,
            },
        }
        return _Invocation(
            record,
            result,
            stdout,
            stderr,
            stdout_identity,
            stderr_identity,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.TimeoutExpired):
            error_message = f"command timed out after {error.timeout} seconds"
        else:
            error_message = _bounded(error)
        record = {
            "index": index,
            "label": label,
            "argv": command,
            "cwd": str(cwd),
            "returncode": None,
            "error": error_message,
            "stdout": {
                "path": str((output / "stdout.raw").relative_to(stage)),
                "size_bytes": None,
                "sha256": None,
                "binding_intact": False,
            },
            "stderr": {
                "path": str((output / "stderr.raw").relative_to(stage)),
                "size_bytes": None,
                "sha256": None,
                "binding_intact": False,
            },
        }
        return _Invocation(record, None, b"", b"", None, None)


def _revalidate_command_evidence(stage: Path, commands: list[_Invocation]) -> bool:
    intact = True
    for invocation in commands:
        if invocation.result is None:
            continue
        for name, identity in (
            ("stdout", invocation.stdout_identity),
            ("stderr", invocation.stderr_identity),
        ):
            raw_record = invocation.record.get(name)
            if not isinstance(raw_record, dict) or identity is None:
                intact = False
                continue
            raw_path = raw_record.get("path")
            if type(raw_path) is not str:
                intact = False
                continue
            if not _refresh_file_record(stage / raw_path, raw_record, identity):
                intact = False
    return intact


def _command_evidence_expectations(
    commands: list[_Invocation],
) -> list[_EvidenceExpectation]:
    expected: list[_EvidenceExpectation] = []
    for invocation in commands:
        for name, raw, identity in (
            (
                "stdout",
                invocation.stdout if invocation.result is not None else None,
                invocation.stdout_identity,
            ),
            (
                "stderr",
                invocation.stderr if invocation.result is not None else None,
                invocation.stderr_identity,
            ),
        ):
            record = invocation.record.get(name)
            raw_path = record.get("path") if isinstance(record, dict) else None
            if type(raw_path) is not str:
                raise EvaluationError("command evidence path is unavailable")
            expected.append(
                _EvidenceExpectation(
                    raw_path,
                    identity,
                    len(raw) if raw is not None else None,
                    hashlib.sha256(raw).hexdigest() if raw is not None else None,
                )
            )
    return expected


def _expected_directories(expected: list[_EvidenceExpectation]) -> set[str]:
    directories: set[str] = set()
    for item in expected:
        parent = PurePosixPath(item.path).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _present_inventory_directories(
    inventory: list[dict[str, object]],
) -> set[str]:
    paths = [
        _EvidenceExpectation(str(item["path"]), None, None, None)
        for item in inventory
        if item["present"] is True
    ]
    directories = _expected_directories(paths)
    if any(str(item["path"]).startswith("commands/") for item in inventory):
        directories.add("commands")
    return directories


def _stage_paths(stage: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    pending: list[tuple[Path, str]] = [(stage, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as scanner:
            for entry in scanner:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    directories.add(relative)
                    pending.append((Path(entry.path), relative))
                else:
                    files.add(relative)
    return files, directories


def _evidence_inventory(
    stage: Path,
    expected: list[_EvidenceExpectation],
) -> tuple[list[dict[str, object]], bool]:
    if len({item.path for item in expected}) != len(expected):
        raise EvaluationError("duplicate expected evidence path")
    inventory: list[dict[str, object]] = []
    intact = True
    for item in sorted(expected, key=lambda value: value.path):
        path = stage / item.path
        try:
            raw = _regular_bytes(path)
            identity = _regular_identity(path)
            present = True
            size_bytes: int | None = len(raw)
            sha256: str | None = hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError):
            identity = None
            present = False
            size_bytes = None
            sha256 = None
        binding_intact = (
            present
            and item.identity is not None
            and identity == item.identity
            and size_bytes == item.size_bytes
            and sha256 == item.sha256
        )
        intact = binding_intact and intact
        inventory.append(
            {
                "path": item.path,
                "identity": (
                    {"device": item.identity[0], "inode": item.identity[1]}
                    if item.identity is not None
                    else None
                ),
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "present": present,
                "observed_identity": (
                    {"device": identity[0], "inode": identity[1]}
                    if identity is not None
                    else None
                ),
                "observed_size_bytes": size_bytes,
                "observed_sha256": sha256,
                "binding_intact": binding_intact,
            }
        )
    files, directories = _stage_paths(stage)
    expected_files = {item.path for item in expected}
    evidence_files = files - {"receipt.json"}
    intact = (
        evidence_files
        == {item["path"] for item in inventory if item["present"]}
        and intact
    )
    intact = directories == _present_inventory_directories(inventory) and intact
    if evidence_files - expected_files:
        intact = False
    return inventory, intact


def _canonical_record_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _verify_final_stage(
    stage: Path,
    expected: list[_EvidenceExpectation],
    inventory: list[dict[str, object]],
    receipt: dict[str, object],
) -> None:
    observed_inventory, _inventory_intact = _evidence_inventory(stage, expected)
    if observed_inventory != inventory:
        raise EvaluationError("evidence inventory changed after final receipt write")
    receipt_path = stage / "receipt.json"
    receipt_identity = _regular_identity(receipt_path)
    raw = _regular_bytes(receipt_path)
    if raw != _canonical_record_bytes(receipt):
        raise EvaluationError("final receipt canonical bytes changed")
    if receipt.get("receipt_sha256") != record_sha256(receipt, "receipt_sha256"):
        raise EvaluationError("final receipt self-hash mismatch")
    if _regular_identity(receipt_path) != receipt_identity:
        raise EvaluationError("final receipt identity changed")
    files, directories = _stage_paths(stage)
    present_evidence = {
        item["path"] for item in inventory if item["present"] is True
    }
    if files != present_evidence | {"receipt.json"}:
        raise EvaluationError("final publication file inventory mismatch")
    if directories != _present_inventory_directories(inventory):
        raise EvaluationError("final publication directory inventory mismatch")


def _unexpected_stage_entries(
    stage: Path,
    commands: list[_Invocation],
) -> list[dict[str, object]]:
    expected_files = {
        "synthetic.oracleGeneral.bin",
        "simulator-results.cachesim",
        "capacity_probe.c",
        "capacity-probe",
        "metadata_probe.c",
        "metadata-probe",
    }
    expected_directories = {"commands"}
    for invocation in commands:
        for name in ("stdout", "stderr"):
            raw_record = invocation.record.get(name)
            if not isinstance(raw_record, dict):
                continue
            raw_path = raw_record.get("path")
            if type(raw_path) is str:
                expected_files.add(raw_path)
                expected_directories.add(str(PurePosixPath(raw_path).parent))

    unexpected: list[
        tuple[Path, tuple[int, int], dict[str, object], bool]
    ] = []
    directories: list[tuple[Path, str]] = [(stage, "")]
    while directories:
        directory, prefix = directories.pop()
        with os.scandir(directory) as scanner:
            for entry in scanner:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                path = Path(entry.path)
                metadata = entry.stat(follow_symlinks=False)
                identity = (metadata.st_dev, metadata.st_ino)
                if stat.S_ISDIR(metadata.st_mode):
                    if relative in expected_directories:
                        directories.append((path, relative))
                        continue
                    record = {
                        "path": relative,
                        "type": "directory",
                        "size_bytes": None,
                        "sha256": None,
                    }
                    unexpected.append((path, identity, record, True))
                    continue
                if relative in expected_files and stat.S_ISREG(metadata.st_mode):
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    raw = _regular_bytes(path)
                    entry_type = "regular"
                elif stat.S_ISLNK(metadata.st_mode):
                    raw = os.fsencode(os.readlink(path))
                    entry_type = "symlink"
                else:
                    raw = b""
                    entry_type = "other"
                record = {
                    "path": relative,
                    "type": entry_type,
                    "size_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
                unexpected.append((path, identity, record, False))

    for path, identity, _record, is_directory in unexpected:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity:
            raise EvaluationError(f"unexpected stage entry changed: {path.name}")
        if is_directory:
            _cleanup_owned(path, identity)
        else:
            os.unlink(path)
    if unexpected:
        descriptor = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return [record for _path, _identity, record, _is_directory in unexpected]


_CAPACITY_TEMPLATE = r'''#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libCacheSim.h"

int main(int argc, char **argv) {
  if (argc != 4 || strcmp(argv[2], "@POLICY@") != 0) return 2;
  char *end = NULL;
  uint64_t cache_size = strtoull(argv[3], &end, 10);
  if (!end || *end != '\0' || cache_size == 0) return 2;
  reader_init_param_t reader_params = default_reader_init_params();
  reader_params.cap_at_n_req = 10000;
  reader_t *reader = setup_reader(argv[1], ORACLE_GENERAL_TRACE, &reader_params);
  common_cache_params_t cache_params = default_common_cache_params();
  cache_params.cache_size = cache_size;
  cache_params.hashpower = 16;
  cache_params.consider_obj_metadata = true;
  cache_t *cache = @POLICY@_init(cache_params, NULL);
  if (!reader || !cache) return 2;
  int64_t maximum = 0;
  uint64_t requests = 0;
  request_t *request = new_request();
  while (requests < 10000 && read_one_req(reader, request) == 0) {
    cache->get(cache, request);
    int64_t occupied = cache->get_occupied_byte(cache);
    if (occupied < 0 || occupied > cache->cache_size) {
      fprintf(stderr, "capacity violation at request %" PRIu64 "\n", requests + 1);
      return 3;
    }
    if (occupied > maximum) maximum = occupied;
    requests++;
  }
  if (requests != 10000) return 4;
  printf("capacity_conserved=1\nrequests=%" PRIu64
         "\nmax_occupied_bytes=%" PRId64 "\ncache_size_bytes=%" PRId64 "\n",
         requests, maximum, cache->cache_size);
  free_request(request);
  close_reader(reader);
  cache->cache_free(cache);
  return 0;
}
'''.encode()


_METADATA_TEMPLATE = r'''#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libCacheSim.h"

#define POINTER_CAPACITY 262144
struct pointer_entry { void *pointer; size_t requested; };
static struct pointer_entry pointers[POINTER_CAPACITY];
static size_t live_bytes;
static int accounting_error;
void *__real_malloc(size_t);
void *__real_calloc(size_t, size_t);
void *__real_realloc(void *, size_t);
void __real_free(void *);
static int remember(void *pointer, size_t requested) {
  if (!pointer) return 1;
  for (size_t i = 0; i < POINTER_CAPACITY; i++) if (!pointers[i].pointer) {
    pointers[i].pointer = pointer; pointers[i].requested = requested;
    live_bytes += requested; return 1;
  }
  accounting_error = 1; return 0; /* fixed table overflow */
}
void *__wrap_malloc(size_t size) { void *p = __real_malloc(size); remember(p, size); return p; }
void *__wrap_calloc(size_t n, size_t size) {
  if (size && n > SIZE_MAX / size) { accounting_error = 1; return NULL; }
  void *p = __real_calloc(n, size); remember(p, n * size); return p;
}
void __wrap_free(void *pointer) {
  if (!pointer) return;
  for (size_t i = 0; i < POINTER_CAPACITY; i++) if (pointers[i].pointer == pointer) {
    live_bytes -= pointers[i].requested; pointers[i].pointer = NULL;
    pointers[i].requested = 0; __real_free(pointer); return;
  }
  accounting_error = 1; /* unknown free */
}
void *__wrap_realloc(void *pointer, size_t size) {
  if (!pointer) return __wrap_malloc(size);
  size_t old = 0; size_t slot = POINTER_CAPACITY;
  for (size_t i = 0; i < POINTER_CAPACITY; i++) if (pointers[i].pointer == pointer) {
    old = pointers[i].requested; slot = i; break;
  }
  if (slot == POINTER_CAPACITY) { accounting_error = 1; return NULL; } /* unknown realloc */
  void *replacement = __real_realloc(pointer, size);
  if (replacement) { pointers[slot].pointer = replacement; pointers[slot].requested = size;
    live_bytes = live_bytes - old + size; }
  return replacement;
}
int main(int argc, char **argv) {
  if (argc != 4 || strcmp(argv[2], "@POLICY@") != 0) return 2;
  char *end = NULL;
  uint64_t cache_size = strtoull(argv[3], &end, 10);
  if (!end || *end != '\0' || cache_size == 0) return 2;
  common_cache_params_t cache_params = default_common_cache_params();
  cache_params.cache_size = cache_size;
  cache_params.hashpower = 16;
  cache_params.consider_obj_metadata = true;
  cache_t *cache = @POLICY@_init(cache_params, NULL);
  if (!cache) return 2;
  size_t global = live_bytes;
  size_t samples[3] = {0, 0, 0};
  int64_t residents[3] = {0, 0, 0};
  size_t sample_index = 0;
  request_t request;
  memset(&request, 0, sizeof(request));
  request.obj_size = 64;
  request.valid = true;
  for (size_t inserted = 1; inserted <= 10000; inserted++) {
    request.obj_id = inserted;
    request.clock_time = inserted - 1;
    cache->get(cache, &request);
    if (inserted == 1000 || inserted == 5000 || inserted == 10000) {
      samples[sample_index] = live_bytes;
      residents[sample_index] = cache->get_n_obj(cache);
      sample_index++;
    }
  }
  cache->cache_free(cache);
  printf("global_metadata_bytes=%zu\n", global);
  printf("sample=1000 live_bytes=%zu resident_objects=%lld\n",
         samples[0], (long long)residents[0]);
  printf("sample=5000 live_bytes=%zu resident_objects=%lld\n",
         samples[1], (long long)residents[1]);
  printf("sample=10000 live_bytes=%zu resident_objects=%lld\n",
         samples[2], (long long)residents[2]);
  printf("status=%s\n", accounting_error ? "accounting_error" : "ok");
  return accounting_error ? 3 : 0;
}
'''.encode()


def _probe_source(template: bytes, policy: str) -> bytes:
    return template.replace(b"@POLICY@", policy.encode("ascii"))


def _probe_build_flags(
    cache_path: Path, source_receipt: Mapping[str, object]
) -> tuple[list[str], list[str], str]:
    raw = _regular_bytes(cache_path)
    if len(raw) > 4 * 1024 * 1024:
        raise EvaluationError("Release CMake cache is too large")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise EvaluationError("Release CMake cache is not UTF-8") from error
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith(("#", "//")) or ":" not in line or "=" not in line:
            continue
        name = line.split(":", 1)[0]
        value = line.split("=", 1)[1]
        if name in values:
            raise EvaluationError(f"duplicate Release CMake cache binding: {name}")
        values[name] = value

    compilers = source_receipt["compilers"]
    expected_c = compilers["c"]["path"]
    expected_cxx = compilers["cxx"]["path"]
    if (
        values.get("CMAKE_C_COMPILER") != expected_c
        or values.get("CMAKE_CXX_COMPILER") != expected_cxx
    ):
        raise EvaluationError("Release compiler selection differs from the source receipt")
    raw_includes = values.get("GLib_INCLUDE_DIRS", "").split(";")
    if not raw_includes or any(
        not item or not Path(item).is_absolute() for item in raw_includes
    ):
        raise EvaluationError("Release GLib include binding is invalid")
    raw_glib_libraries = values.get("GLib_LIBRARIES", "").split(";")
    if not raw_glib_libraries or any(
        re.fullmatch(r"[A-Za-z0-9_.+-]+", item) is None
        for item in raw_glib_libraries
    ):
        raise EvaluationError("Release GLib library binding is invalid")
    include_flags = [flag for item in raw_includes for flag in ("-I", item)]
    link_flags = [f"-l{item}" for item in raw_glib_libraries]
    if values.get("OPT_SUPPORT_ZSTD_TRACE") == "ON":
        zstd = values.get("ZSTD_LIBRARY_RELEASE")
        if zstd is None or not Path(zstd).is_absolute() or zstd.endswith("-NOTFOUND"):
            raise EvaluationError("Release ZSTD library binding is invalid")
        link_flags.append(zstd)
    tcmalloc = values.get("Tcmalloc_LIBRARY")
    if tcmalloc and not tcmalloc.endswith("-NOTFOUND"):
        if not Path(tcmalloc).is_absolute():
            raise EvaluationError("Release tcmalloc library binding is invalid")
        link_flags.append(tcmalloc)
    link_flags.extend(["-lstdc++", "-lm", "-ldl", "-pthread"])
    return include_flags, link_flags, hashlib.sha256(raw).hexdigest()


def _executable_hash(path: Path) -> str | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        return None
    return sha256_file(path)


def _capture_executable(
    path: Path, stage: Path
) -> tuple[dict[str, object], tuple[int, int] | None]:
    digest = _executable_hash(path)
    record: dict[str, object] = {
        "path": str(path.relative_to(stage)),
        "size_bytes": None,
        "sha256": digest,
        "binding_intact": digest is not None,
    }
    if digest is None:
        return record, None
    metadata = path.lstat()
    record["size_bytes"] = metadata.st_size
    return record, (metadata.st_dev, metadata.st_ino)


def _contract_receipt(contract: PolicyContract | None) -> dict[str, object] | None:
    if contract is None:
        return None
    return {
        "policy": contract.policy,
        "reference_policy": contract.reference_policy,
        "policy_source": contract.policy_source,
        "object_metadata_bytes": contract.object_metadata_bytes,
        "global_metadata_bytes": contract.global_metadata_bytes,
        "global_metadata_evidence": [
            {"source": source, "line": line, "expression": expression}
            for source, line, expression in contract.global_metadata_evidence
        ],
        "update_complexity": contract.update_complexity,
    }


def _failure_receipt(
    *,
    base: str,
    candidate: str,
    policy: str,
    source_receipt: Path,
    error: BaseException,
    started: int,
    progress: Mapping[str, object],
) -> dict[str, object]:
    checks = {
        "source_binding": progress.get("source_binding", False),
        "evidence_binding": None,
        "build": None,
        "full_tests": None,
        "candidate_test": None,
        "sanitizer": None,
        "deterministic": None,
        "capacity": None,
        "metadata_probe": None,
    }
    source = progress.get("source_receipt")
    source_hash = source.get("receipt_sha256") if isinstance(source, dict) else None
    scope = progress.get("scope")
    scope_record = (
        {**asdict(scope), "changed_paths": list(scope.changed_paths)}
        if isinstance(scope, ScopeFacts)
        else None
    )
    contract = progress.get("contract")
    return {
        "schema_version": 1,
        "receipt_version": 1,
        "rung": "r0",
        "base_commit": base,
        "candidate_commit": candidate,
        "policy": policy,
        "source_receipt_path": str(source_receipt),
        "source_receipt_sha256": source_hash,
        "checks": checks,
        "candidate_tree": progress.get("candidate_tree"),
        "scope": scope_record,
        "declared_metadata": (
            _contract_receipt(contract)
            if isinstance(contract, PolicyContract)
            else None
        ),
        "measured_metadata": None,
        "complexity_audit": "pending_independent_review",
        "synthetic_trace": None,
        "commands": [],
        "probes": None,
        "errors": [_bounded(error)],
        "timings": {"total_wall_ns": max(0, time.monotonic_ns() - started)},
    }


def evaluate_r0(
    *,
    checkout: Path,
    base: str,
    candidate: str,
    policy: str,
    source_receipt: Path,
    output: Path,
    run: Run = run_child,
) -> dict[str, object]:
    started = time.monotonic_ns()
    checkout_root = _checkout_path(Path(checkout))
    final = _output_path(Path(output), checkout_root)
    preflight_progress: dict[str, object] = {}
    try:
        source, candidate_tree, scope, contract, binding = _preflight(
            checkout_root,
            base,
            candidate,
            policy,
            Path(source_receipt),
            preflight_progress,
        )
        changed_path_sha256 = {
            path: hashlib.sha256(
                _git_bytes(checkout_root, "show", f"{candidate}:{path}")
            ).hexdigest()
            for path in scope.changed_paths
        }
        policy_source = f"libCacheSim/cache/eviction/{policy}.c"
        policy_source_sha256 = hashlib.sha256(
            _git_bytes(checkout_root, "show", f"{candidate}:{policy_source}")
        ).hexdigest()
        candidate_test_sha256 = (
            changed_path_sha256[f"test/test_{policy}.c"] if candidate != base else None
        )
        contract_sha256 = (
            changed_path_sha256["commissioning/cache_policy_contract.json"]
            if contract is not None
            else None
        )
        evaluator_hashes = {
            "evaluate_sha256": sha256_file(Path(__file__)),
            "scope_sha256": sha256_file(Path(__file__).with_name("scope.py")),
        }
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        stage, stage_identity = _stage_directory(final)
        published = False
        try:
            failure = _failure_receipt(
                base=base,
                candidate=candidate,
                policy=policy,
                source_receipt=Path(source_receipt),
                error=error,
                started=started,
                progress=preflight_progress,
            )
            write_new_record(stage / "receipt.json", failure, "receipt_sha256")
            _publish_stage(stage, stage_identity, final)
            published = True
        finally:
            if not published and os.path.lexists(stage):
                _cleanup_owned(stage, stage_identity)
        raise EvaluationError(_bounded(error)) from error

    root = Path(checkout).resolve(strict=True)
    stage, stage_identity = _stage_directory(final)
    published = False
    build_owners: dict[Path, tuple[int, int]] = {}
    errors: list[str] = []
    commands: list[_Invocation] = []

    def invoke(
        label: str, argv: Sequence[str], *, command_cwd: Path = root
    ) -> _Invocation:
        item = _command_record(
            stage, len(commands) + 1, label, argv, command_cwd, run
        )
        commands.append(item)
        if not item.ok:
            errors.append(f"{label}: command did not complete successfully")
        return item

    try:
        trace_path = stage / "synthetic.oracleGeneral.bin"
        synthetic = generate_synthetic_trace(trace_path)
        trace_evidence = _capture_expected_evidence(trace_path, stage)
        cache_bytes = int(synthetic["working_set_bytes"]) // 10

        release_config = invoke(
            "release-configure",
            [
                "cmake",
                "-S",
                ".",
                "-B",
                "_build-release",
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DENABLE_TESTS=ON",
            ],
        )
        release_root = root / "_build-release"
        if os.path.lexists(release_root):
            try:
                build_owners[release_root] = _directory_identity(release_root)
            except EvaluationError as error:
                errors.append(_bounded(error))
        release_build = invoke(
            "release-build", ["cmake", "--build", "_build-release", "-j", "8"]
        )
        release_tests = invoke(
            "release-full-tests",
            ["ctest", "--test-dir", "_build-release", "--output-on-failure"],
        )
        candidate_test: _Invocation | None = None
        if candidate != base:
            candidate_test = invoke(
                "candidate-test",
                [
                    "ctest",
                    "--test-dir",
                    "_build-release",
                    "--output-on-failure",
                    "-R",
                    f"^test_{policy}$",
                    "--no-tests=error",
                ],
            )
            if not _candidate_ctest_passed(candidate_test, policy):
                errors.append("candidate CTest did not run the exact registered test")
        sanitize_config = invoke(
            "sanitize-configure",
            [
                "cmake",
                "-S",
                ".",
                "-B",
                "_build-sanitize",
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                "-DENABLE_TESTS=ON",
                "-DCMAKE_C_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer",
                "-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined -fno-omit-frame-pointer",
                "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined",
            ],
        )
        sanitize_root = root / "_build-sanitize"
        if os.path.lexists(sanitize_root):
            try:
                build_owners[sanitize_root] = _directory_identity(sanitize_root)
            except EvaluationError as error:
                errors.append(_bounded(error))
        sanitize_build = invoke(
            "sanitize-build", ["cmake", "--build", "_build-sanitize", "-j", "8"]
        )
        sanitize_tests = invoke(
            "sanitize-full-tests",
            ["ctest", "--test-dir", "_build-sanitize", "--output-on-failure"],
        )

        binary = release_root / "bin/cachesim"
        binary_sha256 = _executable_hash(binary)
        release_commands_state = _combined_outcome(
            _command_outcome(release_config),
            _command_outcome(release_build),
        )
        build_state = (
            binary_sha256 is not None
            if release_commands_state is True
            else release_commands_state
        )
        full_tests_state = (
            _command_outcome(release_tests) if build_state is True else None
        )
        candidate_test_state = None
        if build_state is True and candidate_test is not None:
            candidate_test_outcome = _command_outcome(candidate_test)
            if candidate_test_outcome is not None:
                candidate_test_state = _candidate_ctest_passed(candidate_test, policy)
        simulator_result_path = stage / "simulator-results.cachesim"
        simulation_argv = [
            str(binary),
            str(trace_path),
            "oracleGeneral",
            policy,
            str(cache_bytes),
            "--num-thread=1",
            f"--num-req={_REQUEST_COUNT}",
            "--warmup-sec=0",
            "--consider-obj-metadata=true",
            "--print-head-req=false",
            f"--output={simulator_result_path}",
        ]
        simulation_one = invoke(
            "determinism-run-1", simulation_argv, command_cwd=stage
        )
        simulation_two = invoke(
            "determinism-run-2", simulation_argv, command_cwd=stage
        )
        deterministic: bool | None = None
        simulation_receipts: list[dict[str, object]] = []
        if build_state is True and simulation_one.ok and simulation_two.ok:
            try:
                parsed_one = parse_cachesim_output(simulation_one.stdout.decode("ascii"))
                parsed_two = parse_cachesim_output(simulation_two.stdout.decode("ascii"))
                scientific_one = (
                    parsed_one.request_count,
                    parsed_one.object_miss_ratio,
                    parsed_one.byte_miss_ratio,
                )
                scientific_two = (
                    parsed_two.request_count,
                    parsed_two.object_miss_ratio,
                    parsed_two.byte_miss_ratio,
                )
                deterministic = (
                    scientific_one == scientific_two
                    and parsed_one.request_count == _REQUEST_COUNT - 1
                )
                for parsed in (parsed_one, parsed_two):
                    simulation_receipts.append(
                        {
                            "request_count": parsed.request_count,
                            "object_miss_ratio": str(parsed.object_miss_ratio),
                            "byte_miss_ratio": str(parsed.byte_miss_ratio),
                            "simulator_throughput_mqps": str(
                                parsed.simulator_throughput_mqps
                            ),
                        }
                    )
            except (UnicodeError, ValueError) as error:
                errors.append(f"determinism parser: {_bounded(error)}")
        simulator_result_receipt: dict[str, object] | None = None
        simulator_result_identity: tuple[int, int] | None = None
        try:
            simulator_result_raw = _regular_bytes(simulator_result_path)
            simulator_result_identity = _regular_identity(simulator_result_path)
            simulator_result_receipt = {
                "path": str(simulator_result_path.relative_to(stage)),
                "size_bytes": len(simulator_result_raw),
                "sha256": hashlib.sha256(simulator_result_raw).hexdigest(),
            }
            if simulator_result_raw != simulation_one.stdout + simulation_two.stdout:
                raise EvaluationError("simulator result file differs from raw stdout")
        except (OSError, ValueError) as error:
            errors.append(f"simulator result binding: {_bounded(error)}")
            deterministic = None
        simulator_result_evidence = _capture_expected_evidence(
            simulator_result_path, stage
        )

        compiler = source["compilers"]["c"]["path"]
        probe_toolchain_clean = True
        probe_include_flags: list[str] = []
        probe_link_flags: list[str] = ["-lm", "-ldl", "-pthread"]
        release_cache_sha256: str | None = None
        try:
            (
                probe_include_flags,
                probe_link_flags,
                release_cache_sha256,
            ) = _probe_build_flags(release_root / "CMakeCache.txt", source)
        except (OSError, ValueError) as error:
            errors.append(f"probe toolchain binding: {_bounded(error)}")
            probe_toolchain_clean = False

        capacity_source = stage / "capacity_probe.c"
        capacity_source_raw = _probe_source(_CAPACITY_TEMPLATE, policy)
        _write_private(capacity_source, capacity_source_raw)
        capacity_source_evidence = _capture_expected_evidence(capacity_source, stage)
        capacity_binary = stage / "capacity-probe"
        capacity_compile = invoke(
            "capacity-compile",
            [
                compiler,
                "-std=c11",
                "-O2",
                "-I",
                str(root / "libCacheSim/include"),
                *probe_include_flags,
                "-o",
                str(capacity_binary),
                str(capacity_source),
                str(release_root / "liblibCacheSim.a"),
                *probe_link_flags,
            ],
        )
        capacity_binary_receipt, capacity_binary_identity = _capture_executable(
            capacity_binary, stage
        )
        capacity_binary_evidence = _capture_expected_evidence(capacity_binary, stage)
        capacity_run = invoke(
            "capacity-run",
            [str(capacity_binary), str(trace_path), policy, str(cache_bytes)],
            command_cwd=stage,
        )
        capacity: bool | None = None
        capacity_values: dict[str, int | bool] | None = None
        capacity_prerequisites = (
            build_state is True
            and probe_toolchain_clean
            and capacity_compile.ok
            and capacity_binary_identity is not None
        )
        capacity_diagnostic = capacity_run.stdout + capacity_run.stderr
        if capacity_prerequisites and b"capacity violation" in capacity_diagnostic.lower():
            capacity = False
        elif capacity_prerequisites and capacity_run.ok:
            try:
                capacity_values = parse_capacity_probe(capacity_run.stdout.decode("ascii"))
                capacity = capacity_values["cache_size_bytes"] == cache_bytes
            except (UnicodeError, ValueError) as error:
                errors.append(f"capacity probe: {_bounded(error)}")
                if "capacity violation" in str(error):
                    capacity = False

        metadata_source = stage / "metadata_probe.c"
        metadata_source_raw = _probe_source(_METADATA_TEMPLATE, policy)
        _write_private(metadata_source, metadata_source_raw)
        metadata_source_evidence = _capture_expected_evidence(metadata_source, stage)
        metadata_binary = stage / "metadata-probe"
        metadata_compile = invoke(
            "metadata-compile",
            [
                compiler,
                "-std=c11",
                "-O2",
                "-I",
                str(root / "libCacheSim/include"),
                *probe_include_flags,
                "-o",
                str(metadata_binary),
                str(metadata_source),
                str(release_root / "liblibCacheSim.a"),
                "-Wl,--wrap=malloc",
                "-Wl,--wrap=calloc",
                "-Wl,--wrap=realloc",
                "-Wl,--wrap=free",
                *probe_link_flags,
            ],
        )
        metadata_binary_receipt, metadata_binary_identity = _capture_executable(
            metadata_binary, stage
        )
        metadata_binary_evidence = _capture_expected_evidence(metadata_binary, stage)
        metadata_run = invoke(
            "metadata-run",
            [str(metadata_binary), str(trace_path), policy, str(cache_bytes)],
            command_cwd=stage,
        )
        measured: tuple[Decimal, int] | None = None
        metadata_state: bool | None = None
        metadata_prerequisites = (
            build_state is True
            and probe_toolchain_clean
            and metadata_compile.ok
            and metadata_binary_identity is not None
        )
        if metadata_prerequisites and metadata_run.result is not None:
            try:
                parsed_metadata = parse_metadata_probe(
                    metadata_run.stdout.decode("ascii")
                )
                if metadata_run.result.returncode == 0:
                    measured = parsed_metadata
                    metadata_state = True
            except (UnicodeError, ValueError) as error:
                errors.append(f"metadata probe: {_bounded(error)}")
                if metadata_run.result.returncode == 0 or metadata_run.stdout.strip():
                    metadata_state = False

        expected_evidence = [
            trace_evidence,
            simulator_result_evidence,
            capacity_source_evidence,
            capacity_binary_evidence,
            metadata_source_evidence,
            metadata_binary_evidence,
            *_command_evidence_expectations(commands),
        ]

        evidence_binding_clean = _revalidate_command_evidence(stage, commands)
        if simulator_result_receipt is not None and simulator_result_identity is not None:
            evidence_binding_clean = (
                _refresh_file_record(
                    simulator_result_path,
                    simulator_result_receipt,
                    simulator_result_identity,
                )
                and evidence_binding_clean
            )
        if capacity_binary_identity is not None:
            evidence_binding_clean = (
                _refresh_file_record(
                    capacity_binary,
                    capacity_binary_receipt,
                    capacity_binary_identity,
                )
                and evidence_binding_clean
            )
        if metadata_binary_identity is not None:
            evidence_binding_clean = (
                _refresh_file_record(
                    metadata_binary,
                    metadata_binary_receipt,
                    metadata_binary_identity,
                )
                and evidence_binding_clean
            )
        if release_cache_sha256 is not None:
            try:
                evidence_binding_clean = (
                    sha256_file(release_root / "CMakeCache.txt")
                    == release_cache_sha256
                    and evidence_binding_clean
                )
            except OSError:
                evidence_binding_clean = False
        unexpected_stage_entries = _unexpected_stage_entries(stage, commands)
        if unexpected_stage_entries:
            evidence_binding_clean = False
            errors.append("candidate created an unregistered stage entry")
        if not evidence_binding_clean:
            errors.append("retained process or binary evidence changed after capture")

        sanitizer_finding = any(
            _SANITIZER.search(item.stdout) is not None
            or _SANITIZER.search(item.stderr) is not None
            for item in (sanitize_config, sanitize_build, sanitize_tests)
        )
        sanitizer_commands_state = _combined_outcome(
            _command_outcome(sanitize_config),
            _command_outcome(sanitize_build),
            _command_outcome(sanitize_tests),
        )
        sanitizer_state = (
            False
            if sanitizer_finding
            else True
            if sanitizer_commands_state is True
            else None
        )
        checks: dict[str, bool | None] = {
            "source_binding": True,
            "evidence_binding": evidence_binding_clean,
            "build": build_state,
            "full_tests": full_tests_state,
            "candidate_test": candidate_test_state,
            "sanitizer": sanitizer_state,
            "deterministic": deterministic,
            "capacity": capacity,
            "metadata_probe": metadata_state,
        }
        if not probe_toolchain_clean:
            checks["capacity"] = None
            checks["metadata_probe"] = None
            measured = None
        if not evidence_binding_clean:
            _clear_unavailable_checks(checks)
            measured = None
        binary_post_run_sha256 = _executable_hash(binary)
        if binary_post_run_sha256 != binary_sha256:
            errors.append("candidate binary binding mutated during evaluation")
            checks["evidence_binding"] = False
            _clear_unavailable_checks(checks)
            measured = None
        try:
            if _regular_bytes(capacity_source) != capacity_source_raw:
                raise EvaluationError("capacity probe source changed")
        except (OSError, ValueError) as error:
            errors.append(f"capacity probe binding: {_bounded(error)}")
            checks["evidence_binding"] = False
            checks["capacity"] = None
        try:
            if _regular_bytes(metadata_source) != metadata_source_raw:
                raise EvaluationError("metadata probe source changed")
        except (OSError, ValueError) as error:
            errors.append(f"metadata probe binding: {_bounded(error)}")
            checks["evidence_binding"] = False
            checks["metadata_probe"] = None
            measured = None
        apparatus_binding_clean = True
        try:
            if sha256_file(Path(source["_resolved_path"])) != source["_file_sha256"]:
                raise EvaluationError("source receipt changed")
            if sha256_file(Path(__file__)) != evaluator_hashes["evaluate_sha256"]:
                raise EvaluationError("R0 evaluator changed")
            if (
                sha256_file(Path(__file__).with_name("scope.py"))
                != evaluator_hashes["scope_sha256"]
            ):
                raise EvaluationError("R0 scope evaluator changed")
        except (OSError, ValueError) as error:
            errors.append(f"apparatus binding: {_bounded(error)}")
            apparatus_binding_clean = False
        if not apparatus_binding_clean:
            checks["source_binding"] = False
            _clear_unavailable_checks(checks)
            measured = None
        try:
            observed_trace_sha256 = sha256_file(trace_path)
        except OSError as error:
            observed_trace_sha256 = None
            errors.append(f"synthetic trace revalidation: {_bounded(error)}")
        synthetic["post_run_sha256"] = observed_trace_sha256
        if observed_trace_sha256 != synthetic["sha256"]:
            errors.append("synthetic trace binding mutated during evaluation")
            checks["evidence_binding"] = False
            checks["deterministic"] = None
            checks["capacity"] = None
            checks["metadata_probe"] = None
            measured = None

        cleanup_clean = True
        for path, identity in build_owners.items():
            if not os.path.lexists(path):
                errors.append(f"apparatus build directory disappeared: {path.name}")
                cleanup_clean = False
                continue
            try:
                _cleanup_owned(path, identity)
            except (OSError, ValueError) as error:
                errors.append(_bounded(error))
                cleanup_clean = False
        try:
            _post_binding(root, binding, allowed_roots=(stage,))
        except (OSError, ValueError) as error:
            errors.append(_bounded(error))
            cleanup_clean = False
        if not cleanup_clean:
            checks["source_binding"] = False
            _clear_unavailable_checks(checks)
            measured = None

        evidence_inventory, final_inventory_intact = _evidence_inventory(
            stage, expected_evidence
        )
        if not final_inventory_intact:
            errors.append("expected evidence inventory is missing or changed")
            checks["evidence_binding"] = False
            if any(
                item["identity"] is not None and item["binding_intact"] is False
                for item in evidence_inventory
            ):
                _clear_unavailable_checks(checks)
            measured = None

        measured_receipt = (
            {
                "bytes_per_object": str(measured[0]),
                "global_bytes": measured[1],
                "measurement_sha256": metadata_run.result.stdout_sha256,
                "within_budget": None,
            }
            if measured is not None and metadata_run.result is not None
            else None
        )
        synthetic["path"] = str(trace_path.relative_to(stage))
        synthetic["cache_fraction"] = "0.10"
        synthetic["cache_size_bytes"] = cache_bytes
        receipt: dict[str, object] = {
            "schema_version": 1,
            "receipt_version": 1,
            "rung": "r0",
            "source_receipt_path": str(source["_resolved_path"]),
            "source_receipt_sha256": source["receipt_sha256"],
            "source_receipt_file_sha256": source["_file_sha256"],
            "repository_url": SOURCE_LOCK["repository_url"],
            "base_commit": base,
            "base_tree": SOURCE_LOCK["tree"],
            "candidate_commit": candidate,
            "candidate_tree": candidate_tree,
            "candidate_diff_sha256": scope.diff_sha256,
            "changed_path_sha256": changed_path_sha256,
            "policy": policy,
            "policy_source_sha256": policy_source_sha256,
            "candidate_test_sha256": candidate_test_sha256,
            "contract_sha256": contract_sha256,
            "binary": str(binary.relative_to(root)),
            "binary_sha256": binary_sha256,
            "binary_post_run_sha256": binary_post_run_sha256,
            "checks": checks,
            "scope": {**asdict(scope), "changed_paths": list(scope.changed_paths)},
            "declared_metadata": _contract_receipt(contract),
            "measured_metadata": measured_receipt,
            "complexity_audit": "pending_independent_review",
            "synthetic_trace": synthetic,
            "simulations": simulation_receipts,
            "simulator_result": simulator_result_receipt,
            "capacity_measurement": capacity_values,
            "commands": [item.record for item in commands],
            "probes": {
                "release_cmake_cache_sha256": release_cache_sha256,
                "include_flags": probe_include_flags,
                "link_flags": probe_link_flags,
                "capacity": {
                    "source_path": str(capacity_source.relative_to(stage)),
                    "source_sha256": hashlib.sha256(capacity_source_raw).hexdigest(),
                    "binary": capacity_binary_receipt,
                },
                "metadata": {
                    "source_path": str(metadata_source.relative_to(stage)),
                    "source_sha256": hashlib.sha256(metadata_source_raw).hexdigest(),
                    "binary": metadata_binary_receipt,
                },
            },
            "evaluator": evaluator_hashes,
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": sys.version,
            },
            "timings": {
                "total_wall_ns": max(0, time.monotonic_ns() - started),
                "command_wall_ns": sum(
                    item.result.wall_ns for item in commands if item.result is not None
                ),
                "command_cpu_ns": sum(
                    item.result.cpu_ns for item in commands if item.result is not None
                ),
            },
            "errors": errors,
            "evidence_inventory": evidence_inventory,
            "unexpected_stage_entries": unexpected_stage_entries,
        }
        write_new_record(stage / "receipt.json", receipt, "receipt_sha256")
        _verify_final_stage(stage, expected_evidence, evidence_inventory, receipt)
        _publish_stage(stage, stage_identity, final)
        published = True
        return receipt
    except BaseException:
        for path, identity in build_owners.items():
            if os.path.lexists(path):
                try:
                    _cleanup_owned(path, identity)
                except (OSError, ValueError):
                    pass
        raise
    finally:
        if not published and os.path.lexists(stage):
            _cleanup_owned(stage, stage_identity)
