"""Tree manipulation tools for the coordinator."""

# pylint: disable=broad-exception-caught

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from ...core.tools.base import Tool
from ..hitl import await_user_decision

if TYPE_CHECKING:
    from ..config import CoordinatorConfig
    from ..idea_tree import IdeaTree
    from ...core.llm.base import LLMProvider

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TreeView
# ---------------------------------------------------------------------------


class TreeViewTool(Tool):
    """View the current idea tree state."""

    name = "TreeView"
    description = (
        "View the current idea tree.\n\n"
        "Formats:\n"
        "- 'compact': Overview with status and insights (default)\n"
        "- 'full': Markdown rendering of the full tree\n"
        "- 'node': Detailed view of a single node (requires node_id)\n"
        "- 'pending': List pending leaf nodes\n"
        "- 'constraints': Root insight + pruned lessons + validated findings, "
        "formatted as hard constraints for the next IDEATE round. Call this "
        "FIRST in every IDEATE step — it tells you what's already been ruled "
        "out and what's already been won."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["compact", "full", "node", "pending", "constraints"],
                "description": "View format (default: compact)",
            },
            "node_id": {
                "type": "string",
                "description": "Node ID (required when format='node')",
            },
        },
        "required": [],
    }
    is_read_only = True

    def __init__(self, *, cwd: str, tree: IdeaTree, **kwargs: Any):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree

    async def execute(self, **kwargs: Any) -> str:
        fmt = kwargs.get("format", "compact")

        if fmt == "compact":
            return self._tree.to_compact_summary()
        elif fmt == "full":
            return self._tree.to_markdown()
        elif fmt == "node":
            node_id = kwargs.get("node_id")
            if not node_id:
                return "Error: node_id is required when format='node'"
            return self._tree.node_detail(node_id)
        elif fmt == "pending":
            pending = self._tree.get_pending_leaves()
            if not pending:
                return "No pending leaf nodes."
            lines = ["Pending leaf nodes:\n"]
            for n in pending:
                lines.append(f"  {n.id} (depth={n.depth}): {n.hypothesis}")
            return "\n".join(lines)
        elif fmt == "constraints":
            return self._tree.get_constraints_block()
        else:
            return f"Error: Unknown format {fmt!r}."


# ---------------------------------------------------------------------------
# TreeAddNode
# ---------------------------------------------------------------------------

class TreeAddNodeTool(Tool):
    """Add a new node to the idea tree."""

    name = "TreeAddNode"
    description = (
        "Add a new node to the idea tree.\n\n"
        "Set parent_id to the parent node's ID (e.g. 'ROOT', '1', '1.1'). "
        "The node ID is generated automatically.\n\n"
        "The hypothesis should describe the idea clearly enough for a "
        "Executor to implement it."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "parent_id": {
                "type": "string",
                "description": "Parent node ID (e.g. 'ROOT', '1', '1.1').",
            },
            "hypothesis": {
                "type": "string",
                "description": (
                    "The idea to explore. Should describe what to change "
                    "and why it might help. Be specific enough for a "
                    "Executor to implement, but leave room for "
                    "implementation decisions."
                ),
            },
            "grounding": {
                "type": "string",
                "description": (
                    "Optional. When a ResearchSearch result shaped this idea, "
                    "record the source citation(s) here (Markdown, e.g. the "
                    "relevant links from the digest). Stored on the node's "
                    "`grounding` field — separate from the post-experiment "
                    "`related_work` novelty audit. Leave empty for ideas not "
                    "derived from an external search."
                ),
            },
        },
        "required": ["parent_id", "hypothesis"],
    }
    is_read_only = False

    def __init__(
        self,
        *,
        cwd: str,
        tree: IdeaTree,
        config: CoordinatorConfig | None = None,
        provider: "LLMProvider | None" = None,
        prune_hook: Callable[[], None] | None = None,
        **kwargs: Any,
    ):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree
        self._config = config
        self._provider = provider
        self._prune_hook = prune_hook

    def set_prune_hook(self, hook: Callable[[], None] | None) -> None:
        """Wire the post-commit context prune callback (called by orchestrator
        after the Agent — and therefore its messages list — exists)."""
        self._prune_hook = hook

    async def execute(self, **kwargs: Any) -> str:
        from ..idea_tree import Node

        parent_id: str = kwargs["parent_id"]
        hypothesis: str = kwargs["hypothesis"]
        grounding: str = (kwargs.get("grounding") or "").strip()

        parent = self._tree.get_node(parent_id)
        if parent is None:
            return f"Error: Parent node {parent_id!r} not found."

        new_depth = parent.depth + 1
        if self._tree.max_depth is not None and new_depth > self._tree.max_depth:
            return (
                f"Error: Cannot add node at depth {new_depth}. "
                f"Max depth is {self._tree.max_depth}."
            )

        review = await self._review_proposed_idea(parent_id, hypothesis)
        if review[0] == "skip":
            return (
                "Proposed idea was skipped by the user during idea-stage review; "
                "no node was added. Propose a different idea or ask for direction."
            )
        if review[1]:
            hypothesis = review[1]

        node_id = self._tree.next_child_id(parent_id)
        node = Node(
            id=node_id,
            parent_id=parent_id,
            depth=new_depth,
            hypothesis=hypothesis,
            grounding=grounding,
        )
        self._tree.add_node(node)

        # On a successful commit, elide the IDEATE scratch work
        # (skill bodies + reasoning) from the coordinator's context.
        # See coordinator/context_prune.py for the anchor / rewrite rules.
        if self._prune_hook is not None:
            try:
                self._prune_hook()
            except Exception as exc:  # noqa: BLE001
                log.warning("IDEATE context prune hook failed: %s", exc)

        msg = f"Added node {node_id} (depth={new_depth}) under {parent_id}: {hypothesis}"

        # Pre-experiment novelty check (opt-in via search.auto_search_on_add).
        # Dispatched in the background; the verdict lands in node.related_work.
        if self._config is not None and self._provider is not None:
            from .search_ctx import dispatch_auto_search

            if dispatch_auto_search(
                self._tree, self._config, self._provider, node_id
            ):
                msg += " [pre-experiment novelty check dispatched → related_work]"

        return msg

    async def _review_proposed_idea(self, parent_id: str, hypothesis: str) -> tuple[str, str | None]:
        """Pause before committing an idea when idea-stage review is enabled."""
        config = self._config
        mode = (getattr(getattr(config, "ui", None), "interaction_mode", "auto") or "auto").lower()
        if mode not in ("review", "collaborative"):
            return ("approve", None)

        timeout = max(1, int(getattr(config.ui, "idea_review_timeout", config.ui.review_timeout)))
        prompt = (
            f"Review proposed idea under parent {parent_id}:\n\n"
            f"{hypothesis}\n\n"
            "Approve it, skip it, or type a revised hypothesis to use instead."
        )
        reply = await await_user_decision(
            self._tree.bus,
            kind="idea_proposal_review",
            prompt=prompt,
            node_id="",
            options=["approve", "skip", "<revised hypothesis>"],
            timeout=timeout,
        )
        if reply is None:
            return ("approve", None)
        text = reply.strip()
        low = text.lower()
        if low in ("", "approve", "approved", "yes", "y", "ok", "go"):
            return ("approve", None)
        if low in ("skip", "no", "n", "reject"):
            return ("skip", None)
        if low.startswith("edit "):
            text = text[5:].strip()
        return ("approve", text or None)


# ---------------------------------------------------------------------------
# TreeUpdateNode
# ---------------------------------------------------------------------------

class TreeUpdateNodeTool(Tool):
    """Update fields on an existing node."""

    name = "TreeUpdateNode"
    description = (
        "Update fields on a tree node.\n\n"
        "Updatable fields: status, insight, result, score, code_ref, hypothesis, "
        "related_work."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "ID of the node to update.",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "running", "done", "needs_retry", "merged", "pruned"],
                "description": (
                    "New status. Use 'needs_retry' for an incomplete/unscored "
                    "attempt that should be resumed or retried (it is NOT a success)."
                ),
            },
            "insight": {
                "type": "string",
                "description": "What was learned from this experiment or direction.",
            },
            "result": {
                "type": "string",
                "description": "Factual description of the experiment outcome.",
            },
            "score": {
                "type": "number",
                "description": "Absolute score achieved on the benchmark (e.g. 45.2 means 45.2%).",
            },
            "code_ref": {
                "type": "string",
                "description": "Git branch name for this experiment.",
            },
            "hypothesis": {
                "type": "string",
                "description": "Updated hypothesis text.",
            },
            "related_work": {
                "type": "string",
                "description": (
                    "Markdown block summarising related work / prior art for "
                    "this node, written by the coordinator (Phase 1) or by a "
                    "dedicated SearchAgent (Phase 2)."
                ),
            },
        },
        "required": ["node_id"],
    }
    is_read_only = False

    def __init__(self, *, cwd: str, tree: IdeaTree, **kwargs: Any):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree

    async def execute(self, **kwargs: Any) -> str:
        node_id: str = kwargs["node_id"]
        node = self._tree.get_node(node_id)
        if node is None:
            return f"Error: Node {node_id!r} not found."

        updates = {
            k: v for k, v in kwargs.items()
            if k != "node_id" and v is not None
        }
        if not updates:
            return f"No updates provided for {node_id}."

        self._tree.update_node(node_id, **updates)

        # Live experience capture (self-evolution): log lessons as the research
        # advances, not only at finalize. Best-effort — never break a tool call.
        if updates.get("insight") or updates.get("status"):
            try:
                from ...experience import append_experience
                # Use the node's current insight (it may have been set by a prior
                # update), not just this call's fields, so notes aren't empty.
                node = self._tree.get_node(node_id)
                enriched = dict(updates)
                if not enriched.get("insight") and node is not None and getattr(node, "insight", ""):
                    enriched["insight"] = node.insight
                append_experience(getattr(self._tree, "json_path", None),
                                   node_id=node_id, updates=enriched)
            except Exception:  # pylint: disable=broad-exception-caught
                pass

        parts = [f"Updated {node_id}:"]
        for k, v in updates.items():
            parts.append(f"  {k} = {v}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# TreePrune
# ---------------------------------------------------------------------------

class TreePruneTool(Tool):
    """Prune a node and its subtree."""

    name = "TreePrune"
    description = (
        "Mark a node and all its descendants as 'pruned'.\n"
        "Pruned nodes remain in the tree for reference but are no longer explored."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "ID of the node to prune.",
            },
            "reason": {
                "type": "string",
                "description": "Why this direction is being abandoned.",
            },
        },
        "required": ["node_id", "reason"],
    }
    is_read_only = False

    def __init__(self, *, cwd: str, tree: IdeaTree, **kwargs: Any):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree

    async def execute(self, **kwargs: Any) -> str:
        node_id: str = kwargs["node_id"]
        reason: str = kwargs.get("reason", "")

        node = self._tree.get_node(node_id)
        if node is None:
            return f"Error: Node {node_id!r} not found."

        self._tree.prune_node(node_id, reason=reason)
        return f"Pruned {node_id} and its subtree. Reason: {reason}"


# ---------------------------------------------------------------------------
# TreeSetMeta
# ---------------------------------------------------------------------------

class TreeSetMetaTool(Tool):
    """Set tree-level metadata (baseline_score, trunk_score)."""

    name = "TreeSetMeta"
    description = (
        "Set tree-level metadata such as baseline_score and trunk_score.\n\n"
        "Use this to record the initial baseline score after running the first "
        "evaluation, and to update the trunk score after merging a successful branch.\n\n"
        "These values appear in TreeView summaries and the final report."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "baseline_score": {
                "type": "number",
                "description": "The baseline score of the unmodified codebase on B_dev. Set once at the start.",
            },
            "trunk_score": {
                "type": "number",
                "description": "The current trunk/main branch score on B_dev after merging improvements.",
            },
            "test_baseline_score": {
                "type": "number",
                "description": "The baseline score on B_test. Set once after running B_test on the unmodified codebase.",
            },
            "test_trunk_score": {
                "type": "number",
                "description": "The current trunk score on B_test. Set after running B_test on the final trunk.",
            },
            "eval_cmd": {
                "type": "string",
                "description": "Exact command to run B_dev evaluation. Supports {cwd} and {node_id} template variables. Stored in tree metadata and auto-injected into Executor prompts.",
            },
            "eval_cmd_test": {
                "type": "string",
                "description": "Exact command to run B_test evaluation (if different from eval_cmd). Supports {cwd} and {node_id} template variables.",
            },
            "eval_timeout": {
                "type": "integer",
                "description": "Timeout in seconds for each automatic B_test verification attempt. Overrides --eval-timeout for this run tree.",
            },
            "eval_retries": {
                "type": "integer",
                "description": "Number of extra automatic B_test retries after a transient failure or timeout. Overrides --eval-retries for this run tree.",
            },
            "eval_retry_base_delay": {
                "type": "number",
                "description": "Base delay for automatic B_test retry backoff in seconds. Overrides --eval-retry-base-delay for this run tree.",
            },
            "eval_retry_max_delay": {
                "type": "number",
                "description": "Max delay for automatic B_test retry backoff in seconds. Overrides --eval-retry-max-delay for this run tree.",
            },
            "dataset_info": {
                "type": "string",
                "description": "Brief description of dataset paths and structure.",
            },
            "metric_direction": {
                "type": "string",
                "enum": ["maximize", "minimize"],
                "description": "Whether higher or lower scores are better. Affects merge decisions and best-node selection.",
            },
            "submission_path": {
                "type": "string",
                "description": "Path to the submission file relative to workspace root (e.g. 'submission.csv').",
            },
            "sample_submission_path": {
                "type": "string",
                "description": "Path to the sample submission file (e.g. 'data/sample_submission.csv').",
            },
        },
        "required": [],
    }
    is_read_only = False

    def __init__(self, *, cwd: str, tree: IdeaTree, config: CoordinatorConfig, **kwargs: Any):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree
        self._config = config

    async def execute(self, **kwargs: Any) -> str:
        had_baseline = self._tree.meta.get("baseline_score") is not None
        updated: list[str] = []
        cache_keys = {
            "baseline_score",
            "trunk_score",
            "test_baseline_score",
            "test_trunk_score",
            "eval_cmd",
            "eval_cmd_test",
            "eval_timeout",
            "eval_retries",
            "eval_retry_base_delay",
            "eval_retry_max_delay",
            "dataset_info",
            "metric_direction",
        }

        # Numeric fields
        for key in (
            "baseline_score", "trunk_score", "test_baseline_score", "test_trunk_score",
            "eval_timeout", "eval_retries", "eval_retry_base_delay", "eval_retry_max_delay",
        ):
            if key in kwargs and kwargs[key] is not None:
                self._tree.meta[key] = kwargs[key]
                updated.append(f"{key} = {kwargs[key]}")

        # String fields
        for key in ("eval_cmd", "eval_cmd_test", "dataset_info", "metric_direction", "submission_path", "sample_submission_path"):
            if key in kwargs and kwargs[key] is not None:
                self._tree.meta[key] = kwargs[key]
                updated.append(f"{key} = {kwargs[key]}")

        if not updated:
            return "No updates provided."
        self._tree.save()

        # Keep the baseline cache aligned when evaluation metadata changes.
        cache_relevant_update = any(k in kwargs and kwargs[k] is not None for k in cache_keys)
        if self._tree.meta.get("baseline_score") is not None and (not had_baseline or cache_relevant_update):
            self._write_baseline_cache()

        return "Updated tree metadata:\n  " + "\n  ".join(updated)

    def _write_baseline_cache(self) -> None:
        """Write baseline cache into the agent workspace for future runs."""
        meta = self._tree.meta
        cache = {
            "baseline_score": meta.get("baseline_score"),
            "trunk_score": meta.get("trunk_score"),
            "test_baseline_score": meta.get("test_baseline_score"),
            "test_trunk_score": meta.get("test_trunk_score"),
            "eval_cmd": meta.get("eval_cmd"),
            "eval_cmd_test": meta.get("eval_cmd_test"),
            "eval_timeout": meta.get("eval_timeout"),
            "eval_retries": meta.get("eval_retries"),
            "eval_retry_base_delay": meta.get("eval_retry_base_delay"),
            "eval_retry_max_delay": meta.get("eval_retry_max_delay"),
            "dataset_info": meta.get("dataset_info"),
            "metric_direction": meta.get("metric_direction"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if self._config.workspace_dir:
            cache_path = Path(self._config.workspace_dir) / ".coordinator" / "baseline_cache.json"
        else:
            cache_path = Path(self._config.cwd) / ".research_baseline.json"
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            log.info("Wrote baseline cache to %s", cache_path)
        except Exception as e:
            log.warning("Failed to write baseline cache: %s", e)


# ---------------------------------------------------------------------------
# Propagation (standalone function + tool wrapper)
# ---------------------------------------------------------------------------

async def propagate_insights(
    tree: "IdeaTree",
    provider: "LLMProvider",
    node_id: str,
) -> str:
    """Walk from node's parent to root (bottom-up), synthesizing children insights at each level."""
    node = tree.get_node(node_id)
    if node is None:
        return f"Error: Node {node_id!r} not found."

    path = tree.get_path_to_root(node_id)
    ancestors = path[1:]  # skip the node itself; order: parent → … → root
    if not ancestors:
        return f"Node {node_id} has no ancestors to propagate to."

    updated: list[str] = []

    for ancestor in ancestors:
        children = tree.get_children(ancestor.id)
        child_parts: list[str] = []
        for child in children:
            if child.insight:
                score_str = f" (score={child.score:.1f}%)" if child.score is not None else ""
                child_parts.append(f"- [{child.id}, {child.status}{score_str}]: {child.insight}")

        if not child_parts:
            continue

        joined = "\n".join(child_parts)
        context = (
            "This is the ROOT node — produce a global research insight summary."
            if ancestor.parent_id is None
            else f"This is node {ancestor.id} (hypothesis: {ancestor.hypothesis})."
        )

        try:
            response = await provider.create(
                system=(
                    "You are a research insight synthesizer. "
                    "Given insights from child experiments, produce a concise "
                    "summary that captures the key learnings, patterns, and "
                    "actionable conclusions. Be specific about what works and "
                    "what doesn't. Keep it under 200 words."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"{context}\n\n"
                        f"Children insights:\n{joined}\n\n"
                        f"Synthesize these into a concise research insight."
                    ),
                }],
                max_tokens=1024,
            )
            try:
                from ...core.agent import record_llm_usage
                record_llm_usage(
                    response,
                    bus=tree.bus,
                    model=getattr(provider, "model", None),
                    source="propagate_insights",
                )
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            summary = response.get_text().strip()
        except Exception as exc:
            log.warning("LLM call failed during propagation for %s: %s", ancestor.id, exc)
            continue

        await tree.async_update_node(ancestor.id, insight=summary)
        short = summary[:100] + "..." if len(summary) > 100 else summary
        updated.append(f"  {ancestor.id}: {short}")

    if not updated:
        return f"Propagation from {node_id}: no ancestors had children with insights."

    return (
        f"Propagated insights from {node_id} upward through {len(updated)} node(s):\n"
        + "\n".join(updated)
    )


class TreePropagateTool(Tool):
    """Propagate insights from a completed node up to the root."""

    name = "TreePropagate"
    description = (
        "After a leaf node experiment completes, propagate insights upward "
        "through the tree to the root.\n\n"
        "For each ancestor from parent to root: gathers all children's insights, "
        "calls the LLM to abstract/summarize them, and updates the ancestor's "
        "insight field. The root's insight becomes the global research insight.\n\n"
        "Note: RunExecutor calls this automatically. Use this tool manually only "
        "if you updated a node's insight via TreeUpdateNode and want to re-propagate."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "The node ID to propagate from (typically a just-completed leaf).",
            },
        },
        "required": ["node_id"],
    }
    is_read_only = False

    def __init__(self, *, cwd: str, tree: "IdeaTree", provider: "LLMProvider", **kwargs: Any):
        super().__init__(cwd=cwd, **kwargs)
        self._tree = tree
        self._provider = provider

    async def execute(self, **kwargs: Any) -> str:
        node_id: str = kwargs["node_id"]
        return await propagate_insights(self._tree, self._provider, node_id)
