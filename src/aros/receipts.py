"""Pure helpers for stable AROS receipt hashes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping

from .store import json_sha256


def record_sha256(record: Mapping[str, object], hash_field: str) -> str:
    payload = dict(record)
    payload.pop(hash_field, None)
    return json_sha256(payload)


def digest_chunks(chunks: Iterable[bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in chunks:
        byte_count += len(chunk)
        digest.update(chunk)
    return byte_count, digest.hexdigest()


def content_receipt(path: str, byte_count: int, sha256: str) -> dict[str, object]:
    return {"path": path, "bytes": byte_count, "sha256": sha256}
