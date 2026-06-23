"""Contract tests for the redesigned WebUI page (``src/webui/index.html``).

The page is a zero-dependency single file, driven entirely by the SSE snapshot
stream and the token-gated ``POST /input`` channel. There is no JS test runner
in this repo, so these tests pin the *contract* the page depends on — the
SSE/input endpoints, the snapshot field names it consumes, the set of nav
screens, and the interactive surfaces (review gate, steer) — and check that the
asset is served with its hardening headers. They guard against silently breaking
the wiring between ``server.py`` / ``snapshot.py`` and the page.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from arbor.webui.launcher import start_session_webui

# Read the page from this tree (robust to how ``arbor`` is installed).
INDEX_PATH = Path(__file__).resolve().parents[1] / "src" / "webui" / "index.html"
INDEX = INDEX_PATH.read_text(encoding="utf-8")


def test_page_wires_the_sse_and_input_contract() -> None:
    assert "EventSource('/events')" in INDEX          # live snapshot stream
    assert "/input" in INDEX and "X-Arbor-Token" in INDEX  # token-gated control channel
    assert "'snapshot'" in INDEX and "'event'" in INDEX     # frame kinds it branches on


def test_page_consumes_documented_snapshot_fields() -> None:
    # If snapshot.py renames any of these, the page silently loses that data.
    for field in ("run_name", "task", "model", "phase", "cycle_num", "total_cycles",
                  "best_score", "baseline_score", "metric_direction", "best_score_history",
                  "tokens", "cache", "hit_rate", "tree", "counters", "thinking",
                  "companion", "gate", "interactive"):
        assert field in INDEX, f"page no longer references snapshot field {field!r}"


def test_nav_has_exactly_the_five_screens() -> None:
    for key in ("key:'home'", "key:'pipeline'", "key:'ideas'", "key:'branches'", "key:'ask'"):
        assert key in INDEX
    # The Runs / System / Settings pages were removed — no nav keys, view fns, or state.
    for gone in ("key:'runs'", "key:'system'", "key:'settings'",
                 "viewRuns", "viewSystem", "viewSettings",
                 "ST.toggles", "ST.interactionMode", "ST.screen==='system'"):
        assert gone not in INDEX, f"leftover reference to a removed feature: {gone!r}"


def test_review_gate_and_steer_surfaces_present() -> None:
    # Review-gate banner -> POST /input {type:'gate'}, with approve/edit controls.
    assert "renderGate" in INDEX
    assert "type:'gate'" in INDEX
    assert 'data-act="gate-edit"' in INDEX
    # Steer affordance in Ask Arbor (inject a message into the research agent).
    assert "askmode" in INDEX and "'steer'" in INDEX


def test_cache_hitrate_is_surfaced() -> None:
    assert "cacheHitStr" in INDEX and "hit_rate" in INDEX


def test_modal_backdrop_guard_present() -> None:
    # Clicking inside a modal must not fire the backdrop's close action.
    assert "data-stop" in INDEX and "t.contains(stop)" in INDEX


def test_uses_the_arbor_logo_not_a_placeholder() -> None:
    # The real Arbor mark is inlined for the sidebar logo and the favicon.
    assert 'rel="icon"' in INDEX                      # favicon (no more /favicon.ico 404)
    assert "data:image/png;base64," in INDEX          # inlined mark, self-contained
    assert 'alt="Arbor"' in INDEX


def test_index_is_served_with_hardening_headers(tmp_path: Path) -> None:
    # Minimal on-disk session so the file-backed server has something to serve.
    coord = tmp_path / ".arbor" / "sessions" / "r" / ".coordinator"
    coord.mkdir(parents=True)
    (coord / "idea_tree.json").write_text(
        json.dumps({"root_id": "ROOT", "nodes": {}, "meta": {}}), encoding="utf-8")
    server = start_session_webui(coord.parent, run_name="r", preferred=8850, scan=40)
    if server is None:
        pytest.skip("no free port to bind the WebUI in this environment")
    try:
        with urllib.request.urlopen(f"{server.url}/", timeout=5) as resp:
            body = resp.read().decode("utf-8")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert "text/html" in resp.headers.get("Content-Type", "")
        # Served body is the real page, with its live wiring intact.
        assert "<title>Arbor</title>" in body
        assert "EventSource('/events')" in body
    finally:
        server.stop()
