"""Session-local refs returned to the Principal by reality tools."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath


_PATTERNS = (
    re.compile(r"tasks/TASK-[A-Za-z0-9-]+/collected\.json"),
    re.compile(r"runs/RUN-[A-Za-z0-9-]+/final\.json"),
    re.compile(r"eval/evaluations/EVAL-[A-Za-z0-9-]+/receipt\.json"),
)


class ObservedRefError(ValueError):
    pass


def validate_observed_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref or "\x00" in ref or "\\" in ref:
        raise ObservedRefError("observation ref is invalid")
    path = PurePosixPath(ref)
    if (
        path.is_absolute()
        or path.as_posix() != ref
        or any(part in {"", ".", ".."} for part in path.parts)
        or not any(pattern.fullmatch(ref) for pattern in _PATTERNS)
    ):
        raise ObservedRefError(f"observation ref is unsafe or nonterminal: {ref}")
    return ref


class ObservedRefs:
    def __init__(self) -> None:
        self._refs: set[str] = set()

    def record(self, ref: str) -> None:
        self._refs.add(validate_observed_ref(ref))

    def snapshot(self) -> tuple[str, ...]:
        return tuple(sorted(self._refs))

    def clear(self, refs: Iterable[str]) -> None:
        for ref in refs:
            self._refs.discard(validate_observed_ref(ref))


__all__ = ["ObservedRefError", "ObservedRefs", "validate_observed_ref"]
