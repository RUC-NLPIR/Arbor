"""Unit tests for the Idea Tree — the core data structure of Arbor's
hypothesis-tree search. Covers node serialization, parent/child linking,
status/score queries, metric-direction-aware comparisons, mutations, recursive
pruning, child-ID generation, and JSON round-tripping.

Pure in-memory: trees are built with ``json_path=None`` so ``save()`` is a
no-op and the default ``NullBus`` makes every event emit a no-op.
"""

from __future__ import annotations

import pytest

from arbor.coordinator.idea_tree import IdeaTree, Node


def _tree(direction: str = "maximize", **meta) -> IdeaTree:
    """A fresh tree with only ROOT, no disk persistence."""
    t = IdeaTree(Node(id="ROOT", parent_id=None, depth=0))
    t.meta["metric_direction"] = direction
    t.meta.update(meta)
    return t


def _child(tree: IdeaTree, parent_id: str, **kw) -> Node:
    """Add and return a new child node under ``parent_id``."""
    parent = tree.get_node(parent_id)
    assert parent is not None
    nid = tree.next_child_id(parent_id)
    node = Node(id=nid, parent_id=parent_id, depth=parent.depth + 1, **kw)
    tree.add_node(node)
    return node


# ── Node serialization ───────────────────────────────────────────────

def test_node_to_dict_omits_empty_optionals() -> None:
    node = Node(id="1", parent_id="ROOT", depth=1, hypothesis="try X")
    d = node.to_dict()
    assert d["id"] == "1"
    assert d["hypothesis"] == "try X"
    # Empty/None optionals are omitted to keep the JSON compact.
    for absent in ("insight", "result", "score", "code_ref", "related_work",
                   "eval_status", "stop_reason", "attempt"):
        assert absent not in d


def test_node_to_dict_includes_set_fields() -> None:
    node = Node(id="1", parent_id="ROOT", depth=1, score=42.0,
                eval_status="scored", attempt=3)
    d = node.to_dict()
    assert d["score"] == 42.0
    assert d["eval_status"] == "scored"
    assert d["attempt"] == 3  # attempt is only serialized when != 1


def test_node_round_trip() -> None:
    node = Node(id="1.2", parent_id="1", depth=2, hypothesis="h",
                insight="learned", score=0.0, code_ref="branch-x", attempt=2)
    assert Node.from_dict(node.to_dict()) == node


def test_node_from_dict_score_delta_backward_compat() -> None:
    # Legacy trees stored the metric under "score_delta".
    node = Node.from_dict({"id": "1", "parent_id": "ROOT", "score_delta": 12.5})
    assert node.score == 12.5


# ── add_node + linking ───────────────────────────────────────────────

def test_add_node_links_parent() -> None:
    t = _tree()
    child = _child(t, "ROOT", hypothesis="a")
    assert child.id == "1"
    assert t.get_node("ROOT").children_ids == ["1"]
    assert [n.id for n in t.get_children("ROOT")] == ["1"]


def test_add_node_rejects_duplicate_id() -> None:
    t = _tree()
    _child(t, "ROOT")
    with pytest.raises(ValueError, match="already exists"):
        t.add_node(Node(id="1", parent_id="ROOT", depth=1))


def test_add_node_rejects_missing_parent() -> None:
    t = _tree()
    with pytest.raises(ValueError, match="does not exist"):
        t.add_node(Node(id="9", parent_id="nope", depth=1))


# ── Queries ──────────────────────────────────────────────────────────

def test_get_path_to_root() -> None:
    t = _tree()
    _child(t, "ROOT")          # "1"
    _child(t, "1")             # "1.1"
    path = [n.id for n in t.get_path_to_root("1.1")]
    assert path == ["1.1", "1", "ROOT"]


def test_get_nodes_by_status() -> None:
    t = _tree()
    _child(t, "ROOT", status="done")
    _child(t, "ROOT", status="pending")
    assert {n.id for n in t.get_nodes_by_status("done")} == {"1"}


def test_get_pending_leaves_excludes_root_and_parents() -> None:
    t = _tree()
    _child(t, "ROOT", status="pending")     # "1" — has a child, not a leaf
    _child(t, "1", status="pending")        # "1.1" — pending leaf
    _child(t, "ROOT", status="done")        # "2" — not pending
    leaves = {n.id for n in t.get_pending_leaves()}
    # ROOT excluded (depth 0); "1" excluded (has children); "2" excluded (done).
    assert leaves == {"1.1"}


# ── Metric-direction-aware comparisons ───────────────────────────────

def test_is_improvement_maximize() -> None:
    t = _tree("maximize")
    assert t.is_improvement(0.9, 0.5) is True
    assert t.is_improvement(0.5, 0.9) is False


def test_is_improvement_minimize() -> None:
    t = _tree("minimize")
    assert t.is_improvement(0.2, 0.5) is True
    assert t.is_improvement(0.5, 0.2) is False


def test_best_done_node_maximize() -> None:
    t = _tree("maximize")
    _child(t, "ROOT", status="done", score=10.0)
    _child(t, "ROOT", status="merged", score=30.0)
    _child(t, "ROOT", status="done", score=20.0)
    assert t.get_best_done_node().score == 30.0  # merged counts too


def test_best_done_node_minimize() -> None:
    t = _tree("minimize")
    _child(t, "ROOT", status="done", score=10.0)
    _child(t, "ROOT", status="done", score=5.0)
    assert t.get_best_done_node().score == 5.0


def test_best_done_node_none_when_unscored() -> None:
    t = _tree()
    _child(t, "ROOT", status="pending", score=99.0)  # not done
    _child(t, "ROOT", status="done")                 # done but no score
    assert t.get_best_done_node() is None


# ── Mutations ────────────────────────────────────────────────────────

def test_update_node_sets_whitelisted_field() -> None:
    t = _tree()
    _child(t, "ROOT")
    t.update_node("1", status="done", score=7.5)
    assert t.get_node("1").status == "done"
    assert t.get_node("1").score == 7.5


def test_update_node_rejects_non_mutable_field() -> None:
    t = _tree()
    _child(t, "ROOT")
    with pytest.raises(ValueError, match="Invalid field"):
        t.update_node("1", depth=99)


def test_update_node_missing_raises() -> None:
    t = _tree()
    with pytest.raises(ValueError, match="not found"):
        t.update_node("nope", status="done")


def test_prune_node_recurses_into_subtree() -> None:
    t = _tree()
    _child(t, "ROOT")          # "1"
    _child(t, "1")             # "1.1"
    _child(t, "1.1")           # "1.1.1"
    t.prune_node("1", reason="dead end")
    assert all(t.get_node(nid).status == "pruned" for nid in ("1", "1.1", "1.1.1"))
    assert "dead end" in t.get_node("1").insight       # reason on target only
    assert "dead end" not in t.get_node("1.1").insight


# ── prune cascade -> event contract (#62) ──────────────────────────────

def test_prune_node_emits_one_event_per_pruned_node() -> None:
    from arbor.events.bus import EventBus

    bus = EventBus()
    t = IdeaTree(Node(id="ROOT", parent_id=None, depth=0), bus=bus)
    _child(t, "ROOT")          # "1"
    _child(t, "1")             # "1.1"
    _child(t, "1.1")           # "1.1.1"
    _child(t, "ROOT")          # "2", untouched sibling, must not fire

    received: list[str] = []
    bus.on("idea.pruned", lambda e: received.append(e.data["node_id"]))

    t.prune_node("1", reason="dead end")

    assert sorted(received) == ["1", "1.1", "1.1.1"]


def test_prune_cascade_updates_stats_and_dashboard_for_every_descendant() -> None:
    from arbor.cli.run_state import RunState
    from arbor.events import types as ev
    from arbor.events.bus import EventBus
    from arbor.events.subscribers.stats_collector import StatsCollector

    bus = EventBus()
    stats = StatsCollector()
    stats.attach(bus)
    run_state = RunState()
    bus.on(ev.IDEA_PROPOSED, run_state.on_idea_proposed)
    bus.on(ev.IDEA_PRUNED, run_state.on_idea_pruned)

    t = IdeaTree(Node(id="ROOT", parent_id=None, depth=0), bus=bus)
    _child(t, "ROOT")          # "1"
    _child(t, "1")             # "1.1"
    _child(t, "1.1")           # "1.1.1"

    t.prune_node("1", reason="dead end")

    assert stats.stats.ideas_pruned == 3
    assert {run_state.ideas[nid].status for nid in ("1", "1.1", "1.1.1")} == {"pruned"}
    assert run_state.ideas_pruned == 3


# ── next_child_id ────────────────────────────────────────────────────

def test_next_child_id_sequence_and_nesting() -> None:
    t = _tree()
    assert t.next_child_id("ROOT") == "1"
    _child(t, "ROOT")                       # "1"
    assert t.next_child_id("ROOT") == "2"   # ROOT children are flat ints
    _child(t, "1")                          # "1.1"
    assert t.next_child_id("1") == "1.2"    # nested children are dotted


def test_next_child_id_fills_after_max() -> None:
    t = _tree()
    _child(t, "ROOT")   # "1"
    _child(t, "ROOT")   # "2"
    t.prune_node("1")   # pruning doesn't free the id
    assert t.next_child_id("ROOT") == "3"


# ── Persistence round-trip ───────────────────────────────────────────

def test_json_round_trip(tmp_path) -> None:
    path = tmp_path / "tree.json"
    t = IdeaTree(Node(id="ROOT", parent_id=None, depth=0), json_path=path)
    t.meta["metric_direction"] = "minimize"
    _child(t, "ROOT", hypothesis="h1", status="done", score=3.0)
    _child(t, "1", hypothesis="h2")

    loaded = IdeaTree.load_json(path)
    assert {n.id for n in loaded.get_all_nodes()} == {"ROOT", "1", "1.1"}
    assert loaded.get_node("1").score == 3.0
    assert loaded.get_node("1").hypothesis == "h1"
    assert loaded.meta["metric_direction"] == "minimize"
    assert [n.id for n in loaded.get_path_to_root("1.1")] == ["1.1", "1", "ROOT"]


def test_node_roundtrips_split_fields():
    n = Node(id="1", parent_id="ROOT", score=42.0, score_split="dev", test_score=40.0)
    d = n.to_dict()
    assert d.get("score_split", "dev") == "dev"  # omitted when default
    assert d["test_score"] == 40.0
    back = Node.from_dict(d)
    assert back.score_split == "dev"
    assert back.test_score == 40.0


def test_node_nondefault_split_is_written():
    n = Node(id="1", parent_id="ROOT", score=40.0, score_split="test")
    d = n.to_dict()
    assert d["score_split"] == "test"
    assert Node.from_dict(d).score_split == "test"


def test_old_node_dict_defaults_split_fields():
    # a node persisted before this feature has neither key
    back = Node.from_dict({"id": "1", "parent_id": "ROOT", "score": 10.0})
    assert back.score_split == "dev"
    assert back.test_score is None


def test_default_split_omitted_from_dict():
    n = Node(id="1", parent_id="ROOT")  # no score
    d = n.to_dict()
    assert "score_split" not in d  # omitted when there is no score
    assert "test_score" not in d
