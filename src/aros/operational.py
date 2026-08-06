"""Pure in-memory intents for service-owned operational records."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_PREFIXES = ("tasks/", "runs/", "eval/")
MAX_OPERATIONAL_PATHS = 256
MAX_OPERATIONAL_PATH_BYTES = 1_024


class OperationalIntentError(ValueError):
    """An operational intent is unsafe or structurally ambiguous."""


@dataclass(frozen=True)
class OperationalIntent:
    schema_version: int
    workspace_paths: tuple[str, ...]
    record_sha256: str

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_paths": list(self.workspace_paths),
            "record_sha256": self.record_sha256,
        }


def build_operational_intent(
    workspace_paths: Iterable[str],
    record_sha256: str,
) -> OperationalIntent:
    if not isinstance(record_sha256, str) or _SHA256.fullmatch(record_sha256) is None:
        raise OperationalIntentError("record_sha256 must be 64 lowercase hex")
    if isinstance(workspace_paths, (str, bytes)):
        raise OperationalIntentError("workspace_paths must be an iterable of paths")
    normalized: set[str] = set()
    try:
        for count, path in enumerate(workspace_paths, start=1):
            if count > MAX_OPERATIONAL_PATHS:
                raise OperationalIntentError("workspace_paths exceeds 256 entries")
            if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
                raise OperationalIntentError("operational path is invalid")
            candidate = PurePosixPath(path)
            if (
                candidate.is_absolute()
                or candidate.as_posix() != path
                or any(part in {"", ".", ".."} for part in candidate.parts)
                or len(path.encode("utf-8")) > MAX_OPERATIONAL_PATH_BYTES
                or not path.startswith(_SERVICE_PREFIXES)
            ):
                raise OperationalIntentError(f"operational path is unsafe: {path}")
            normalized.add(path)
    except (TypeError, UnicodeError) as error:
        raise OperationalIntentError("workspace_paths are invalid") from error
    if not normalized:
        raise OperationalIntentError("workspace_paths must not be empty")
    return OperationalIntent(
        schema_version=1,
        workspace_paths=tuple(sorted(normalized)),
        record_sha256=record_sha256,
    )


__all__ = [
    "OperationalIntent",
    "OperationalIntentError",
    "build_operational_intent",
]
