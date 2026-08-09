from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .cachesim import ChildResult
from .evidence import (
    OutputParentBinding,
    publish_stage_in_parent,
    read_bound_json_object,
    regular_bytes,
    regular_identity,
)
from .records import (
    NewRecordReceipt,
    quarantine_unlink,
    record_sha256,
    write_new_record,
)


class PortfolioEvidenceError(ValueError):
    pass


class PortfolioIntegrityError(PortfolioEvidenceError):
    pass


class RecordCollision(PortfolioIntegrityError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"exclusive record collision: {path.name}")
        self.path = path


class BindingMutationError(PortfolioIntegrityError):
    pass


class PublicationBindingError(PortfolioEvidenceError):
    def __init__(self, message: str, *, renamed: bool) -> None:
        super().__init__(message)
        self.renamed = renamed


@dataclass(frozen=True)
class FileBinding:
    path: Path
    identity: tuple[int, int]
    size_bytes: int
    sha256: str
    mode: int


@dataclass(frozen=True)
class PublicationFileBinding:
    relative_path: str
    identity: tuple[int, int]
    mode: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class PublicationDirectoryBinding:
    relative_path: str
    identity: tuple[int, int]
    mode: int


@dataclass(frozen=True)
class PublicationSnapshot:
    files: tuple[PublicationFileBinding, ...]
    directories: tuple[PublicationDirectoryBinding, ...]


def bounded_error(value: object, limit: int = 512) -> str:
    message = " ".join(str(value).split()) or value.__class__.__name__
    return message if len(message) <= limit else message[: limit - 3] + "..."


def file_binding(path: Path, *, expected_mode: int | None = None) -> FileBinding:
    raw_path = Path(path).absolute()
    descriptor = os.open(raw_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PortfolioEvidenceError(f"expected a regular file: {raw_path}")
        mode = stat.S_IMODE(before.st_mode)
        if expected_mode is not None and mode != expected_mode:
            raise PortfolioEvidenceError(f"file mode binding mismatch: {raw_path.name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        after = raw_path.stat(follow_symlinks=False)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stat.S_ISLNK(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or size != before.st_size
    ):
        raise PortfolioEvidenceError(f"file changed while hashing: {raw_path.name}")
    return FileBinding(
        raw_path.resolve(strict=True),
        (before.st_dev, before.st_ino),
        size,
        digest.hexdigest(),
        mode,
    )


def revalidate_file(expected: FileBinding) -> None:
    observed = file_binding(expected.path, expected_mode=expected.mode)
    if observed != expected:
        raise BindingMutationError(f"bound file changed: {expected.path.name}")


def write_owned_record(
    path: Path, value: dict[str, object], hash_field: str
) -> FileBinding:
    if os.path.lexists(path):
        raise RecordCollision(path)
    try:
        receipt = write_new_record(path, value, hash_field)
    except (OSError, ValueError) as error:
        if os.path.lexists(path):
            raise RecordCollision(path) from error
        raise
    if not isinstance(receipt, NewRecordReceipt):
        raise PortfolioEvidenceError("record publisher returned no ownership receipt")
    try:
        observed = file_binding(path, expected_mode=receipt.mode)
    except (OSError, ValueError) as error:
        if os.path.lexists(path):
            raise RecordCollision(path) from error
        raise PortfolioEvidenceError("published record disappeared") from error
    if (
        observed.identity != receipt.identity
        or observed.size_bytes != receipt.size_bytes
        or observed.sha256 != receipt.sha256
    ):
        raise RecordCollision(path)
    return observed


def revalidate_owned_record(
    path: Path, receipt: NewRecordReceipt, label: str
) -> None:
    try:
        observed = file_binding(path, expected_mode=receipt.mode)
    except (OSError, ValueError) as error:
        if os.path.lexists(path):
            raise RecordCollision(path) from error
        raise PortfolioEvidenceError(f"{label} disappeared") from error
    if (
        observed.identity != receipt.identity
        or observed.size_bytes != receipt.size_bytes
        or observed.sha256 != receipt.sha256
    ):
        raise RecordCollision(path)


def write_failure_record(
    stage: Path, preferred: Path, failure: dict[str, object]
) -> tuple[Path, FileBinding, dict[str, object]]:
    try:
        binding = write_owned_record(preferred, failure, "failure_sha256")
        return preferred, binding, failure
    except RecordCollision:
        fallback = {key: item for key, item in failure.items() if key != "failure_sha256"}
        fallback["record_collision"] = {
            "path": str(preferred.relative_to(stage)),
            "state": "foreign_preserved",
        }
        for _attempt in range(16):
            path = stage / f"failure-{secrets.token_hex(16)}.json"
            try:
                binding = write_owned_record(path, fallback, "failure_sha256")
                return path, binding, fallback
            except RecordCollision:
                continue
        raise PortfolioEvidenceError("cannot allocate a private failure receipt")


def retain_side_effect(
    source: Path, destination: Path, expected_raw: bytes
) -> FileBinding:
    source_binding = file_binding(source)
    if regular_bytes(source) != expected_raw:
        raise BindingMutationError("simulator side-effect output differs from stdout")
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as error:
        raise RecordCollision(destination) from error
    destination_binding = file_binding(destination)
    if (
        destination_binding.identity != source_binding.identity
        or destination_binding.size_bytes != source_binding.size_bytes
        or destination_binding.sha256 != source_binding.sha256
    ):
        raise BindingMutationError("simulator side-effect publication changed")
    descriptor = os.open(
        source.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        quarantine_unlink(
            descriptor,
            source.name,
            source_binding.identity,
            sha256=source_binding.sha256,
        )
    finally:
        os.close(descriptor)
    revalidate_file(destination_binding)
    return destination_binding


def process_record(
    result: ChildResult,
    *,
    stage: Path,
    label: str,
    timeout_seconds: float,
    max_output_bytes: int,
) -> dict[str, object]:
    stdout = regular_bytes(result.stdout_path)
    stderr = regular_bytes(result.stderr_path)
    if (
        result.stdout_path != result.stderr_path.parent / "stdout.raw"
        or result.stderr_path.name != "stderr.raw"
        or len(stdout) != result.stdout_bytes
        or len(stderr) != result.stderr_bytes
        or hashlib.sha256(stdout).hexdigest() != result.stdout_sha256
        or hashlib.sha256(stderr).hexdigest() != result.stderr_sha256
    ):
        raise BindingMutationError("child process raw evidence receipt mismatch")
    return {
        "label": label,
        "argv": list(result.argv),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "returncode": result.returncode,
        "wall_ns": result.wall_ns,
        "cpu_ns": result.cpu_ns,
        "stdout": {
            "path": str(result.stdout_path.relative_to(stage)),
            "size_bytes": result.stdout_bytes,
            "sha256": result.stdout_sha256,
            "identity": dict(zip(("device", "inode"), regular_identity(result.stdout_path))),
        },
        "stderr": {
            "path": str(result.stderr_path.relative_to(stage)),
            "size_bytes": result.stderr_bytes,
            "sha256": result.stderr_sha256,
            "identity": dict(zip(("device", "inode"), regular_identity(result.stderr_path))),
        },
    }


def failed_process_record(
    *,
    label: str,
    argv: Sequence[str],
    result: ChildResult | None,
    error: BaseException,
    timeout_seconds: float,
    max_output_bytes: int,
) -> dict[str, object]:
    def raw_record(path: Path | None) -> dict[str, object]:
        if path is None:
            return {"path": None, "size_bytes": None, "sha256": None}
        try:
            binding = file_binding(path)
        except (OSError, ValueError):
            return {"path": str(path), "size_bytes": None, "sha256": None}
        return {
            "path": str(path),
            "size_bytes": binding.size_bytes,
            "sha256": binding.sha256,
            "identity": {"device": binding.identity[0], "inode": binding.identity[1]},
        }

    return {
        "label": label,
        "argv": list(argv),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "returncode": result.returncode if result is not None else None,
        "wall_ns": result.wall_ns if result is not None else None,
        "cpu_ns": result.cpu_ns if result is not None else None,
        "error": bounded_error(error),
        "stdout": raw_record(result.stdout_path if result is not None else None),
        "stderr": raw_record(result.stderr_path if result is not None else None),
    }


def inventory(stage: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(stage.rglob("*")):
        relative = path.relative_to(stage).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise PortfolioEvidenceError(f"unexpected evidence entry: {relative}")
        if relative == "receipt.json":
            continue
        records.append(inventory_record(stage, file_binding(path)))
    return records


def inventory_record(stage: Path, binding: FileBinding) -> dict[str, object]:
    return {
        "path": binding.path.relative_to(stage).as_posix(),
        "identity": {"device": binding.identity[0], "inode": binding.identity[1]},
        "mode": binding.mode,
        "size_bytes": binding.size_bytes,
        "sha256": binding.sha256,
    }


def tree_bindings(root: Path) -> list[FileBinding]:
    return [
        file_binding(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]


def verify_root(stage: Path, receipt: dict[str, object]) -> None:
    if inventory(stage) != receipt["evidence_inventory"]:
        raise PortfolioEvidenceError("portfolio evidence inventory changed")
    observed = read_bound_json_object(
        stage / "receipt.json", max_bytes=64 * 1024 * 1024
    ).value
    if observed != receipt or receipt.get("receipt_sha256") != record_sha256(
        receipt, "receipt_sha256"
    ):
        raise PortfolioEvidenceError("portfolio root receipt changed")


def publication_snapshot(
    stage: Path,
    stage_identity: tuple[int, int],
    inventory_records: Sequence[Mapping[str, object]],
    root_receipt: NewRecordReceipt,
) -> PublicationSnapshot:
    files = [
        PublicationFileBinding(
            str(item["path"]),
            (int(item["identity"]["device"]), int(item["identity"]["inode"])),  # type: ignore[index]
            int(item["mode"]),
            int(item["size_bytes"]),
            str(item["sha256"]),
        )
        for item in inventory_records
    ]
    files.append(
        PublicationFileBinding(
            "receipt.json",
            root_receipt.identity,
            root_receipt.mode,
            root_receipt.size_bytes,
            root_receipt.sha256,
        )
    )
    root_metadata = stage.lstat()
    directories = [
        PublicationDirectoryBinding(".", stage_identity, stat.S_IMODE(root_metadata.st_mode))
    ]
    for path in sorted(stage.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(
                PublicationDirectoryBinding(
                    path.relative_to(stage).as_posix(),
                    (metadata.st_dev, metadata.st_ino),
                    stat.S_IMODE(metadata.st_mode),
                )
            )
    return PublicationSnapshot(tuple(files), tuple(directories))


def revalidate_publication(root: Path, snapshot: PublicationSnapshot) -> None:
    files: set[str] = set()
    directories = {"."}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise BindingMutationError("publication gained a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(relative)
        elif stat.S_ISREG(metadata.st_mode):
            files.add(relative)
        else:
            raise BindingMutationError("publication gained an unsupported entry")
    if files != {item.relative_path for item in snapshot.files}:
        raise BindingMutationError("publication file inventory changed")
    if directories != {item.relative_path for item in snapshot.directories}:
        raise BindingMutationError("publication directory inventory changed")
    for expected in snapshot.files:
        path = root.joinpath(*PurePosixPath(expected.relative_path).parts)
        observed = file_binding(path, expected_mode=expected.mode)
        if (
            observed.identity != expected.identity
            or observed.size_bytes != expected.size_bytes
            or observed.sha256 != expected.sha256
        ):
            raise BindingMutationError(f"publication file changed: {expected.relative_path}")
    for expected in snapshot.directories:
        path = root if expected.relative_path == "." else root.joinpath(
            *PurePosixPath(expected.relative_path).parts
        )
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected.identity
            or stat.S_IMODE(metadata.st_mode) != expected.mode
        ):
            raise BindingMutationError(f"publication directory changed: {expected.relative_path}")


def publish_and_verify(
    *,
    parent: OutputParentBinding,
    stage: Path,
    stage_identity: tuple[int, int],
    output: Path,
    receipt: dict[str, object],
    inventory: Sequence[Mapping[str, object]],
    root_receipt: NewRecordReceipt,
) -> None:
    renamed = False
    try:
        verify_root(stage, receipt)
        revalidate_owned_record(stage / "receipt.json", root_receipt, "root receipt")
        snapshot = publication_snapshot(
            stage, stage_identity, inventory, root_receipt
        )
        revalidate_publication(stage, snapshot)
        publish_stage_in_parent(parent, stage, stage_identity)
        renamed = True
        revalidate_publication(output, snapshot)
        revalidate_owned_record(
            output / "receipt.json", root_receipt, "published root receipt"
        )
        revalidate_publication(output, snapshot)
    except (OSError, ValueError) as error:
        raise PublicationBindingError(
            f"publication binding verification failed: {bounded_error(error)}",
            renamed=renamed,
        ) from error
