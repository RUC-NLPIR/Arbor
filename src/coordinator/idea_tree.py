"""Idea Tree — simplified data model and persistence.

Each node carries only essential information: hypothesis, status, insight.
Depth is configurable via max_depth (None = unlimited).

Storage: JSON (canonical) + auto-generated Markdown (human-readable).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

log = logging.getLogger(__name__)

NodeStatus = Literal["pending", "running", "done", "needs_retry", "merged", "pruned"]


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """A single node in the Idea Tree."""

    id: str  # e.g. "ROOT", "1", "1.1", "1.1.1"
    parent_id: str | None
    children_ids: list[str] = field(default_factory=list)
    depth: int = 0

    hypothesis: str = ""
    status: NodeStatus = "pending"

    insight: str = ""  # What was learned (both direct and propagated)
    result: str = ""  # Factual description of experiment outcome
    score: float | None = None  # Absolute score (e.g. 45.2%)
    score_split: str = "dev"  # which split `score` came from: "dev" | "test"
    test_score: float | None = None  # verified B_test score, set at merge
    code_ref: str | None = None  # Git branch name
    related_work: str = ""  # SearchAgent / web-search annotation (Markdown)
    grounding: str = ""  # grounding-lane citations that shaped this idea (Markdown)

    # Outcome metadata (set when an executor finishes). eval_status classifies
    # why there is/isn't a score; stop_reason mirrors Agent.stop_reason; attempt
    # counts dispatches (incremented by ResumeExecutor). See _classify_executor_outcome.
    eval_status: str | None = None  # "scored" | "skipped" | "failed_to_run"
    stop_reason: str | None = None  # "finished" | "max_turns" | None
    attempt: int = 1  # 1 for the first run, +1 per resume

    # Fields that may be mutated via update_node / async_update_node.
    # Centralised so the whitelist lives in one place.
    MUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "hypothesis",
        "status",
        "insight",
        "result",
        "score",
        "score_split",
        "test_score",
        "code_ref",
        "related_work",
        "grounding",
        "eval_status",
        "stop_reason",
        "attempt",
    })

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
            "depth": self.depth,
            "hypothesis": self.hypothesis,
            "status": self.status,
        }
        if self.insight:
            d["insight"] = self.insight
        if self.result:
            d["result"] = self.result
        if self.score is not None:
            d["score"] = self.score
            if self.score_split != "dev":
                d["score_split"] = self.score_split
        if self.test_score is not None:
            d["test_score"] = self.test_score
        if self.code_ref is not None:
            d["code_ref"] = self.code_ref
        if self.related_work:
            d["related_work"] = self.related_work
        if self.grounding:
            d["grounding"] = self.grounding
        if self.eval_status:
            d["eval_status"] = self.eval_status
        if self.stop_reason:
            d["stop_reason"] = self.stop_reason
        if self.attempt and self.attempt != 1:
            d["attempt"] = self.attempt
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=data["id"],
            parent_id=data.get("parent_id"),
            children_ids=data.get("children_ids", []),
            depth=data.get("depth", 0),
            hypothesis=data.get("hypothesis", ""),
            status=data.get("status", "pending"),
            insight=data.get("insight", ""),
            result=data.get("result", ""),
            score=data.get("score", data.get("score_delta")),  # backward compat
            score_split=data.get("score_split", "dev"),
            test_score=data.get("test_score"),
            code_ref=data.get("code_ref"),
            related_work=data.get("related_work", ""),
            grounding=data.get("grounding", ""),
            eval_status=data.get("eval_status"),
            stop_reason=data.get("stop_reason"),
            attempt=data.get("attempt", 1),
        )


# ---------------------------------------------------------------------------
# IdeaTree
# ---------------------------------------------------------------------------

class IdeaTree:
    """Hierarchical idea tree with JSON persistence + auto-generated Markdown."""

    VERSION = 3

    def __init__(
        self,
        root: Node,
        json_path: Path | None = None,
        md_path: Path | None = None,
        max_depth: int | None = None,
        bus: "Any" = None,
    ):
        from ..events import NullBus

        self._nodes: dict[str, Node] = {root.id: root}
        self.root_id = root.id
        self.json_path = json_path
        self.md_path = md_path
        self.max_depth = max_depth
        self._save_lock = asyncio.Lock()
        self.meta: dict[str, Any] = self._default_meta()
        self.bus = bus or NullBus()

    @staticmethod
    def _default_meta() -> dict[str, Any]:
        return {
            "baseline_score": None,
            "trunk_score": None,
            "test_baseline_score": None,
            "test_trunk_score": None,
            "eval_cmd": None,
            "eval_cmd_test": None,
            "eval_timeout": None,
            "eval_retries": None,
            "eval_retry_base_delay": None,
            "eval_retry_max_delay": None,
            "dataset_info": None,
            "metric_direction": "maximize",
            "submission_path": None,
            "sample_submission_path": None,
        }

    # ── Accessors ────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def get_root(self) -> Node:
        return self._nodes[self.root_id]

    def get_children(self, node_id: str) -> list[Node]:
        node = self._nodes.get(node_id)
        if node is None:
            return []
        return [self._nodes[cid] for cid in node.children_ids if cid in self._nodes]

    def get_all_nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def get_nodes_by_status(self, status: str) -> list[Node]:
        return [n for n in self._nodes.values() if n.status == status]

    def get_pending_leaves(self) -> list[Node]:
        return [
            n for n in self._nodes.values()
            if n.status == "pending"
            and not n.children_ids
            and n.depth > 0
        ]

    def get_path_to_root(self, node_id: str) -> list[Node]:
        path: list[Node] = []
        current = self._nodes.get(node_id)
        while current is not None:
            path.append(current)
            if current.parent_id is None:
                break
            current = self._nodes.get(current.parent_id)
        return path

    def get_best_done_node(self) -> Node | None:
        candidates = [
            n for n in self._nodes.values()
            if n.status in ("done", "merged") and n.score is not None
        ]
        if not candidates:
            return None
        direction = self.meta.get("metric_direction", "maximize")
        if direction == "minimize":
            return min(candidates, key=lambda n: n.score)
        return max(candidates, key=lambda n: n.score)

    def is_improvement(self, new_score: float, old_score: float) -> bool:
        """Check if new_score is better than old_score, respecting metric_direction."""
        direction = self.meta.get("metric_direction", "maximize")
        if direction == "minimize":
            return new_score < old_score
        return new_score > old_score

    # ── Mutations ────────────────────────────────────────────────────

    def add_node(self, node: Node) -> None:
        if node.id in self._nodes:
            raise ValueError(f"Node {node.id!r} already exists")
        if node.parent_id is not None and node.parent_id not in self._nodes:
            raise ValueError(f"Parent {node.parent_id!r} does not exist")
        self._nodes[node.id] = node
        if node.parent_id is not None:
            parent = self._nodes[node.parent_id]
            if node.id not in parent.children_ids:
                parent.children_ids.append(node.id)
        self.save()
        from ..events.types import IDEA_PROPOSED
        self.bus.emit(IDEA_PROPOSED, {
            "node_id": node.id,
            "hypothesis": getattr(node, "hypothesis", None),
            "parent_id": node.parent_id,
        })

    def update_node(self, node_id: str, **updates: Any) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError(f"Node {node_id!r} not found")
        for key, value in updates.items():
            if key not in Node.MUTABLE_FIELDS:
                raise ValueError(f"Invalid field: {key!r}")
            setattr(node, key, value)
        self.save()
        if "status" in updates:
            status = updates["status"]
            if status == "done":
                # needs_retry is intentionally NOT a completion event — it is an
                # incomplete outcome. Executor-originated transitions surface via
                # EXECUTOR_END (which carries the status); manual ones update the
                # tree and refresh on the next dashboard re-ingest.
                from ..events.types import IDEA_COMPLETED
                self.bus.emit(IDEA_COMPLETED, {
                    "node_id": node_id,
                    "status": status,
                    "score": getattr(node, "score", None),
                })
            elif status == "merged":
                from ..events.types import IDEA_MERGED
                self.bus.emit(IDEA_MERGED, {
                    "node_id": node_id,
                    "from_score": self.meta.get("trunk_score"),
                    "to_score": getattr(node, "score", None),
                    "branch": getattr(node, "code_ref", None),
                })

    def prune_node(self, node_id: str, reason: str = "") -> None:
        node = self._nodes.get(node_id)
        if node is None:
            raise ValueError(f"Node {node_id!r} not found")

        pruned_ids: list[str] = []

        def _prune(nid: str) -> None:
            n = self._nodes.get(nid)
            if n is None:
                return
            n.status = "pruned"
            if reason and nid == node_id:
                n.insight = (n.insight + f"\n[Pruned: {reason}]").strip()
            pruned_ids.append(nid)
            for child_id in n.children_ids:
                _prune(child_id)

        _prune(node_id)
        self.save()
        from ..events.types import IDEA_PRUNED
        # One event per pruned node: every idea.* consumer keys off a single node_id.
        for nid in pruned_ids:
            self.bus.emit(IDEA_PRUNED, {"node_id": nid, "reason": reason})

    # ── Async-safe mutations (for parallel executor scenarios) ───────

    async def async_update_node(self, node_id: str, **updates: Any) -> None:
        """Lock-protected update_node for concurrent access."""
        async with self._save_lock:
            node = self._nodes.get(node_id)
            if node is None:
                raise ValueError(f"Node {node_id!r} not found")
            for key, value in updates.items():
                if key not in Node.MUTABLE_FIELDS:
                    raise ValueError(f"Invalid field: {key!r}")
                setattr(node, key, value)
            self.save()

    # ── ID Generation ────────────────────────────────────────────────

    def next_child_id(self, parent_id: str) -> str:
        """Generate the next child ID.

        ROOT's children: 1, 2, 3, ...
        1's children: 1.1, 1.2, 1.3, ...
        1.1's children: 1.1.1, 1.1.2, ...
        """
        parent = self._nodes.get(parent_id)
        if parent is None:
            raise ValueError(f"Parent {parent_id!r} not found")

        existing_nums: list[int] = []
        for cid in parent.children_ids:
            if parent_id == self.root_id:
                m = re.fullmatch(r"(\d+)", cid)
            else:
                m = re.fullmatch(re.escape(parent_id) + r"\.(\d+)", cid)
            if m:
                existing_nums.append(int(m.group(1)))

        next_num = max(existing_nums, default=0) + 1
        if parent_id == self.root_id:
            return str(next_num)
        return f"{parent_id}.{next_num}"

    # ── Persistence ──────────────────────────────────────────────────

    def save(self) -> None:
        """Synchronous save — safe when only one coroutine mutates at a time.

        For concurrent executor scenarios, prefer ``async_save()``.
        """
        if self.json_path:
            self._save_json(self.json_path)
        if self.md_path:
            self._save_markdown(self.md_path)

    async def async_save(self) -> None:
        """Lock-protected save for concurrent access (e.g. parallel executors)."""
        async with self._save_lock:
            self.save()

    def _save_json(self, path: Path) -> None:
        data = {
            "version": self.VERSION,
            "meta": self.meta,
            "root_id": self.root_id,
            "max_depth": self.max_depth,
            "nodes": {nid: node.to_dict() for nid, node in self._nodes.items()},
        }
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))

    def _save_markdown(self, path: Path) -> None:
        _atomic_write(path, self.to_markdown())

    @classmethod
    def load_json(cls, path: Path) -> IdeaTree:
        data = json.loads(path.read_text(encoding="utf-8"))
        root_id = data["root_id"]
        root_node = Node.from_dict(data["nodes"][root_id])
        tree = cls(
            root=root_node,
            json_path=path,
            md_path=path.with_suffix(".md"),
            max_depth=data.get("max_depth"),
        )
        loaded_meta = data.get("meta", {})
        tree.meta = {**tree._default_meta(), **loaded_meta}
        for nid, ndata in data["nodes"].items():
            if nid != root_id:
                tree._nodes[nid] = Node.from_dict(ndata)
        return tree

    # ── Rendering ────────────────────────────────────────────────────

    def get_constraints_block(self) -> str:
        """Return tree shape + prior insights for IDEATE.

        Includes a TREE SHAPE summary so the model can decide breadth vs depth,
        followed by ROOT insight, pruned lessons, and validated findings.
        """
        lines: list[str] = []

        # ── Tree shape summary ──────────────────────────────────────────
        depth_counts: dict[int, dict[str, int]] = {}
        for n in self._nodes.values():
            if n.depth == 0:
                continue
            bucket = depth_counts.setdefault(n.depth, {})
            bucket[n.status] = bucket.get(n.status, 0) + 1

        if depth_counts:
            depth_str = f"max_depth: {self.max_depth or 'unlimited'}"
            parts: list[str] = [depth_str]
            for d in sorted(depth_counts):
                counts = depth_counts[d]
                total = sum(counts.values())
                detail = ", ".join(f"{c} {s}" for s, c in sorted(counts.items()))
                parts.append(f"depth-{d}: {total} nodes ({detail})")
            lines.append("## TREE SHAPE")
            lines.append(" | ".join(parts))
            lines.append("")

        root = self.get_root()
        if root.insight:
            lines.append("## ROOT INSIGHT (current best global understanding — your priors)")
            lines.append(root.insight.strip())
            lines.append("")

        pruned = [
            n for n in self._nodes.values()
            if n.status == "pruned" and (n.insight or n.hypothesis)
        ]
        if pruned:
            lines.append(
                f"## PRUNED LESSONS ({len(pruned)} — these directions FAILED. "
                f"Do NOT re-propose any idea that shares the same hidden "
                f"assumption or mechanism class without explicitly explaining "
                f"how it counters the lesson.)"
            )
            for n in pruned:
                hyp = _short(n.hypothesis.replace("\n", " "), 100)
                ins = _short((n.insight or "(no insight recorded)").replace("\n", " "), 200)
                lines.append(f"- [{n.id}] {hyp}")
                lines.append(f"  → {ins}")
            lines.append("")

        validated = [
            n for n in self._nodes.values()
            if n.status in ("merged", "done") and n.insight
        ]
        if validated:
            lines.append(
                f"## VALIDATED FINDINGS ({len(validated)} — these are now part "
                f"of the trunk's working assumptions. Build on them; don't "
                f"re-derive them.)"
            )
            for n in validated:
                tag = "merged" if n.status == "merged" else "done"
                score = f" {n.score:.1f}%" if n.score is not None else ""
                hyp = _short(n.hypothesis.replace("\n", " "), 100)
                ins = _short(n.insight.replace("\n", " "), 200)
                lines.append(f"- [{tag} {n.id}{score}] {hyp}")
                lines.append(f"  → {ins}")
            lines.append("")

        if not lines:
            return (
                "No prior insights yet — this is an early-stage tree. "
                "Focus on understanding the task and proposing diverse "
                "initial directions."
            )
        return "\n".join(lines).rstrip()

    def to_compact_summary(self) -> str:
        lines: list[str] = []
        baseline = self.meta.get("baseline_score")
        trunk = self.meta.get("trunk_score")

        # Count nodes by status (excluding root)
        counts: dict[str, int] = {}
        for n in self._nodes.values():
            if n.depth == 0:
                continue
            counts[n.status] = counts.get(n.status, 0) + 1

        best = self.get_best_done_node()
        best_str = ""
        if best and best.score is not None:
            best_str = f", best={best.id} score={best.score:.1f}%"

        status_parts = " ".join(f"{s}={c}" for s, c in sorted(counts.items()))
        lines.append(
            f"TREE (baseline={_fmt(baseline)}, trunk={_fmt(trunk)}, "
            f"nodes={len(self._nodes)}, {status_parts}{best_str}):"
        )

        trunk = self.meta.get("trunk_score")
        self._render_compact(self.get_root(), lines, indent=0, trunk_score=trunk)

        pending = self.get_pending_leaves()
        if pending:
            lines.append("")
            lines.append(f"PENDING LEAVES ({len(pending)}):")
            for n in pending:
                lines.append(f"  {n.id}: {_short(n.hypothesis)}")

        return "\n".join(lines)

    def _render_compact(self, node: Node, lines: list[str], indent: int, trunk_score: float | None = None) -> None:
        prefix = "  " * indent
        score_str = ""
        if node.score is not None:
            score_str = f" ({node.score:.1f}%)"
            if trunk_score is not None:
                delta = node.score - trunk_score
                score_str = f" ({node.score:.1f}%, Δ{delta:+.1f})"
        insight_short = ""
        if node.insight:
            text = node.insight.replace("\n", " ")
            insight_short = f" | {text[:80]}..." if len(text) > 80 else f" | {text}"
        lines.append(
            f"{prefix}{node.id} [{node.status}]{score_str}: "
            f"{_short(node.hypothesis)}{insight_short}"
        )
        for child in self.get_children(node.id):
            self._render_compact(child, lines, indent + 1, trunk_score=trunk_score)

    def to_markdown(self) -> str:
        lines: list[str] = []
        baseline = self.meta.get("baseline_score")
        trunk = self.meta.get("trunk_score")
        lines.append("# Idea Tree\n")
        lines.append(f"**Baseline**: {_fmt(baseline)} | **Trunk**: {_fmt(trunk)}\n")
        self._render_md(self.get_root(), lines, heading_level=2)
        return "\n".join(lines)

    def _render_md(self, node: Node, lines: list[str], heading_level: int) -> None:
        h = "#" * min(heading_level, 6)
        score_str = f" (score: {node.score:.1f}%)" if node.score is not None else ""
        lines.append(f"{h} {node.id}: {node.hypothesis} [{node.status.upper()}]{score_str}\n")
        if node.insight:
            lines.append(f"**Insight**: {node.insight}\n")
        if node.related_work:
            lines.append(f"**Related work**:\n\n{node.related_work}\n")
        if node.grounding:
            lines.append(f"**Grounding**:\n\n{node.grounding}\n")
        if node.result:
            lines.append(f"**Result**: {node.result}\n")
        if node.code_ref:
            lines.append(f"**Branch**: `{node.code_ref}`\n")
        for child in self.get_children(node.id):
            self._render_md(child, lines, heading_level + 1)

    def node_detail(self, node_id: str) -> str:
        node = self._nodes.get(node_id)
        if node is None:
            return f"Error: Node {node_id!r} not found"
        lines = [
            f"Node: {node.id} (depth={node.depth}, status={node.status})",
            f"  Hypothesis: {node.hypothesis}",
        ]
        if node.insight:
            lines.append(f"  Insight: {node.insight}")
        if node.related_work:
            lines.append(f"  Related work: {node.related_work}")
        if node.grounding:
            lines.append(f"  Grounding: {node.grounding}")
        if node.result:
            lines.append(f"  Result: {node.result}")
        if node.score is not None:
            lines.append(f"  Score: {node.score:.1f}%")
        if node.code_ref:
            lines.append(f"  Branch: {node.code_ref}")
        if node.children_ids:
            lines.append(f"  Children: {', '.join(node.children_ids)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(val: Any) -> str:
    if val is None:
        return "N/A"
    if isinstance(val, float):
        return f"{val:.1f}%"
    return str(val)


def _short(text: str, max_len: int = 60) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
