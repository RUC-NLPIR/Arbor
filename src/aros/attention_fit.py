"""Pure deterministic bounding for ResearchAttentionPacket values."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType


_MAX_OMISSION_POINTERS = 12
_MAX_INSTITUTIONAL_OBLIGATIONS = 32


def packet_json(packet: dict[str, object]) -> str:
    return json.dumps(
        packet,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def add_omission(
    omitted: dict[str, int],
    pointer: str,
    count: int = 1,
) -> None:
    if count <= 0:
        return
    normalized = pointer[:160] if pointer else "aros boot"
    if normalized not in omitted and len(omitted) >= _MAX_OMISSION_POINTERS:
        normalized = "aros boot"
    omitted[normalized] = omitted.get(normalized, 0) + count


def item_pointer(item: object, fallback: str) -> str:
    if isinstance(item, dict):
        for key in ("path", "ref"):
            if isinstance(item.get(key), str):
                return str(item[key])
    return fallback


def bound_items(
    items: list[dict[str, object]],
    limit: int,
    pointer: str,
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    for item in items[limit:]:
        add_omission(omitted, item_pointer(item, pointer))
    return items[:limit]


def hashed_pointer(prefix: str, value: object) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}#sha256={hashlib.sha256(payload).hexdigest()}"


def freeze_json(value: object, field: str) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{field} keys must be strings")
        return MappingProxyType(
            {key: freeze_json(item, field) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, field) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"{field} must contain only finite JSON-compatible values")


def context_views(
    authority: Mapping[str, object],
    budget: Mapping[str, object],
    obligations: tuple[Mapping[str, object], ...],
    omitted: dict[str, int],
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    institutional: list[dict[str, object]] = []
    if len(obligations) > _MAX_INSTITUTIONAL_OBLIGATIONS:
        add_omission(
            omitted,
            "institutional obligations",
            len(obligations) - _MAX_INSTITUTIONAL_OBLIGATIONS,
        )
    for item in obligations[:_MAX_INSTITUTIONAL_OBLIGATIONS]:
        thawed = _thaw_json(item)
        assert isinstance(thawed, dict)
        institutional.append(thawed)
    return (
        _thaw_mapping(authority),
        _thaw_mapping(budget),
        institutional,
    )


def _thaw_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _thaw_json(item) for key, item in value.items()}


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def fit_packet(packet: dict[str, object], max_chars: int) -> None:
    if len(packet_json(packet)) <= max_chars:
        return
    warnings = packet["warnings"]
    omitted = packet["omitted"]
    assert isinstance(warnings, list)
    assert isinstance(omitted, dict)
    _warn(warnings, "truncated")

    for excerpt_limit in (256, 128, 64, 32, 16, 8, 4, 0):
        _shorten_excerpts(packet, excerpt_limit, omitted)
        if len(packet_json(packet)) <= max_chars:
            return

    for path in (
        ("hypotheses", "competing"),
        ("snapshot", "candidate", "worktrees", "items"),
        ("snapshot", "candidate", "pending_transition_refs"),
        ("snapshot", "candidate", "git_status", "dirty_paths"),
        ("current_obligations", "institutional"),
        ("pending_measurements",),
        ("unassimilated_returns",),
        ("current_uncertainty",),
        ("blocked_reasons",),
        ("hypotheses", "leading"),
        ("current_obligations", "scientific"),
    ):
        values = _nested(packet, path)
        if isinstance(values, list) and values:
            for item in values:
                add_omission(omitted, item_pointer(item, ".".join(path)))
            values.clear()
            if len(packet_json(packet)) <= max_chars:
                return

    _reduce_context_to_fit(packet, max_chars, omitted)
    if len(packet_json(packet)) <= max_chars:
        return

    retained = [
        warning
        for warning in warnings
        if warning in {"index_incomplete", "truncated"}
    ]
    for warning in warnings:
        if warning not in retained:
            add_omission(omitted, hashed_pointer("warning", warning))
    warnings[:] = retained
    if len(packet_json(packet)) <= max_chars:
        return

    _install_minimal_packet(packet)
    if len(packet_json(packet)) > max_chars:
        raise ValueError("max_chars is too small for the attention packet shape")


def _shorten_excerpts(
    value: object,
    limit: int,
    omitted: dict[str, int],
) -> None:
    if isinstance(value, dict):
        excerpt = value.get("excerpt")
        if isinstance(excerpt, str) and len(excerpt) > limit:
            pointer = f"{value.get('path', 'semantic')}#{value.get('heading', 'excerpt')}"
            if pointer not in omitted:
                add_omission(omitted, pointer)
            value["excerpt"] = excerpt[:limit]
        for key, item in tuple(value.items()):
            if key != "omitted":
                _shorten_excerpts(item, limit, omitted)
    elif isinstance(value, list):
        for item in value:
            _shorten_excerpts(item, limit, omitted)


def _reduce_context_to_fit(
    packet: dict[str, object],
    max_chars: int,
    omitted: dict[str, int],
) -> None:
    candidates: list[tuple[int, str, str, dict[str, object]]] = []
    for key in ("authority", "remaining_budget"):
        mapping = packet[key]
        assert isinstance(mapping, dict)
        for field, value in mapping.items():
            if field == "state":
                continue
            encoded_size = len(packet_json({field: value})) - 2
            candidates.append((encoded_size, key, field, mapping))
    deficit = len(packet_json(packet)) - max_chars
    removed = 0
    selected = 0
    ordered = sorted(
        candidates,
        key=lambda item: (item[0], item[1], item[2]),
        reverse=True,
    )
    for encoded_size, key, field, mapping in ordered:
        add_omission(omitted, hashed_pointer(f"{key}.{field}", mapping[field]))
        del mapping[field]
        removed += encoded_size
        selected += 1
        if removed >= deficit:
            break
    if len(packet_json(packet)) <= max_chars:
        return
    for _size, key, field, mapping in ordered[selected:]:
        add_omission(omitted, hashed_pointer(f"{key}.{field}", mapping[field]))
        del mapping[field]


def _install_minimal_packet(packet: dict[str, object]) -> None:
    snapshot = packet["snapshot"]
    authority = packet["authority"]
    budget = packet["remaining_budget"]
    omitted = packet["omitted"]
    assert isinstance(snapshot, dict)
    assert isinstance(authority, dict)
    assert isinstance(budget, dict)
    assert isinstance(omitted, dict)
    dropped = (
        _fact_count({key: value for key, value in snapshot.items() if key != "canonical"})
        + _fact_count(packet["active_question"])
        + _fact_count(packet["current_uncertainty"])
        + _fact_count(packet["hypotheses"])
        + _fact_count(packet["pending_measurements"])
        + _fact_count(packet["unassimilated_returns"])
        + _fact_count(packet["current_obligations"])
        + _fact_count(packet["blocked_reasons"])
    )
    total = sum(int(count) for count in omitted.values()) + dropped
    packet["snapshot"] = {
        "canonical": snapshot.get("canonical"),
        "candidate": {},
    }
    packet["active_question"] = None
    packet["current_uncertainty"] = []
    packet["hypotheses"] = {"leading": [], "competing": []}
    packet["pending_measurements"] = []
    packet["unassimilated_returns"] = []
    packet["current_obligations"] = {"scientific": [], "institutional": []}
    packet["authority"] = {"state": authority.get("state")}
    packet["remaining_budget"] = {"state": budget.get("state")}
    packet["blocked_reasons"] = []
    packet["warnings"] = ["index_incomplete", "truncated"]
    packet["omitted"] = {"aros boot": max(1, total)}


def _nested(packet: dict[str, object], path: tuple[str, ...]) -> object:
    value: object = packet
    for component in path:
        if not isinstance(value, dict):
            return None
        value = value.get(component)
    return value


def _fact_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(_fact_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_fact_count(item) for item in value)
    return 0 if value is None else 1


def _warn(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


__all__ = [
    "add_omission",
    "bound_items",
    "context_views",
    "fit_packet",
    "freeze_json",
    "hashed_pointer",
    "item_pointer",
    "packet_json",
]
