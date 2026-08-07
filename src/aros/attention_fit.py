from __future__ import annotations

import json
import math
from collections.abc import Mapping
from types import MappingProxyType


def packet_json(packet: dict[str, object]) -> str:
    return json.dumps(
        packet,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def add_omission(omitted: dict[str, int], pointer: str, count: int = 1) -> None:
    if count > 0:
        key = pointer[:160] if pointer else "aros boot"
        if key not in omitted and len(omitted) >= 12:
            key = "aros boot"
        omitted[key] = omitted.get(key, 0) + count


def bound_items(
    items: list[dict[str, object]],
    limit: int,
    pointer: str,
    omitted: dict[str, int],
) -> list[dict[str, object]]:
    for item in items[limit:]:
        value = item.get("path") or item.get("ref") or pointer
        add_omission(omitted, str(value))
    return items[:limit]


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
    raise TypeError(f"{field} must contain finite JSON-compatible values")


def context_views(
    authority: Mapping[str, object],
    budget: Mapping[str, object],
    obligations: tuple[Mapping[str, object], ...],
    omitted: dict[str, int],
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    if len(obligations) > 32:
        add_omission(omitted, "institutional obligations", len(obligations) - 32)
    return _thaw(authority), _thaw(budget), [_thaw(item) for item in obligations[:32]]


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def fit_packet(packet: dict[str, object], max_chars: int) -> None:
    if len(packet_json(packet)) <= max_chars:
        return
    warnings = packet["warnings"]
    omitted = packet["omitted"]
    assert isinstance(warnings, list) and isinstance(omitted, dict)
    _warn(warnings, "truncated")

    for limit in (256, 128, 32, 0):
        _shorten(packet, limit, omitted)
        if len(packet_json(packet)) <= max_chars:
            return
    _minimal(packet)
    if len(packet_json(packet)) > max_chars:
        raise ValueError("max_chars is too small for the attention packet shape")


def _shorten(value: object, limit: int, omitted: dict[str, int]) -> None:
    if isinstance(value, dict):
        excerpt = value.get("excerpt")
        if isinstance(excerpt, str) and len(excerpt) > limit:
            add_omission(omitted, f"{value.get('path', 'semantic')}#excerpt")
            value["excerpt"] = excerpt[:limit]
        for key, item in value.items():
            if key != "omitted":
                _shorten(item, limit, omitted)
    elif isinstance(value, list):
        for item in value:
            _shorten(item, limit, omitted)


def _minimal(packet: dict[str, object]) -> None:
    snapshot = packet["snapshot"]
    authority = packet["authority"]
    budget = packet["remaining_budget"]
    assert isinstance(snapshot, dict)
    assert isinstance(authority, dict) and isinstance(budget, dict)
    packet.update(
        {
            "snapshot": {"canonical": snapshot.get("canonical"), "candidate": {}},
            "active_question": None,
            "current_uncertainty": [],
            "recent_evidence_delta": [],
            "hypotheses": {"leading": [], "competing": []},
            "pending_measurements": [],
            "unread_returns": [],
            "current_obligations": {"scientific": [], "institutional": []},
            "authority": {"state": authority.get("state")},
            "remaining_budget": {"state": budget.get("state")},
            "blocked_reasons": [],
            "warnings": ["truncated"],
            "omitted": {"aros boot": 1},
        }
    )


def _warn(warnings: list[str], value: str) -> None:
    if value not in warnings:
        warnings.append(value)


__all__ = ["add_omission", "bound_items", "context_views", "fit_packet", "freeze_json", "packet_json"]
