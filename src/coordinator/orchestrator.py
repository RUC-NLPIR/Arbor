"""CoordinatorOrchestrator — single persistent ReAct loop.

Instead of creating a fresh Agent per arbor cycle, runs one continuous
Agent whose context is managed by the built-in 4-layer compression.
The Idea Tree (saved to disk on every mutation) serves as persistent
memory across context compressions.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core import Agent, AgentConfig
from ..core.llm.base import LLMProvider
from .checkpoint import (
    CacheAnchor,
    Checkpoint,
    GitState,
    InflightExecutor,
    read_checkpoint,
    read_messages,
    seal_interrupted_tail,
    write_checkpoint,
    write_messages,
)
from .config import CoordinatorConfig
from .context_prune import prune_ideate_context
from .idea_tree import IdeaTree, Node
from .prompts import build_coordinator_system_prompt
from .tools import get_coordinator_tools
from .tools.executor_run import _completed_cycles
from .tools.tree_ops import TreeAddNodeTool
from .tools.worktree import _compute_branch_name

if TYPE_CHECKING:
    from ..events import EventBus, NullBus

log = logging.getLogger(__name__)


def _resume_pending_user_note(pending_user: dict[str, Any] | None) -> str:
    """Resume-prompt section reminding the agent it was paused awaiting a human
    answer, if it was (mirrors the checkpoint's ``pending_user`` / AWAIT_USER
    payload). Empty string when nothing was pending.
    """
    if not pending_user:
        return ""
    question = str(pending_user.get("prompt") or "").strip()
    if not question:
        return ""
    node_id = str(pending_user.get("node_id") or "").strip()
    scope = f" (about node {node_id})" if node_id else ""
    quoted = question.replace("\n", "\n> ")  # keep every line inside the blockquote
    return (
        "## Pending question to the user\n\n"
        f"When the run was interrupted you were waiting for the user's answer{scope} to:\n"
        f"> {quoted}\n\n"
        "Their answer was not received. If you still need it, ask again with "
        "AskUser before proceeding; otherwise continue."
    )


def _git_output(cwd: str, *args: str) -> str | None:
    """Return git command output, or None when cwd is not a usable git repo."""
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None


async def _run_lifecycle_script(
    script_path: str, cwd: str, timeout: int = 120,
    workspace_dir: str | None = None,
) -> str | None:
    """Run a lifecycle hook script if it exists. Returns output or None."""
    raw_path = Path(script_path)
    if raw_path.is_absolute():
        candidates = [raw_path]
    else:
        repo_root = Path(__file__).resolve().parents[2]
        candidates = [Path(cwd) / raw_path, repo_root / raw_path]

    full_path = next((p for p in candidates if p.exists()), None)
    if full_path is None:
        return None
    env = dict(os.environ)
    if workspace_dir:
        env["WORKSPACE_DIR"] = workspace_dir
    proc = await asyncio.create_subprocess_exec(
        "bash", str(full_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            log.warning("Lifecycle script %s exited with code %d: %s", script_path, proc.returncode, output[:500])
        return output
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("Lifecycle script %s timed out after %ds", script_path, timeout)
        return None


class CoordinatorOrchestrator:
    """Orchestrates arbor-guided research via a single persistent Agent.

    The Agent runs one continuous ReAct loop.  The built-in ContextManager
    handles compression when the conversation grows long.  The Idea Tree
    (auto-saved to JSON + Markdown on every mutation) is the durable memory
    that survives context compressions.
    """

    def __init__(
        self,
        config: CoordinatorConfig,
        provider: LLMProvider,
        bus: "EventBus | NullBus | None" = None,
    ):
        from ..events import NullBus  # local import to keep events optional

        self.config = config
        self.provider = provider
        self.tree: IdeaTree | None = None
        self.bus = bus or NullBus()
        self._system_hash: str | None = None
        self._resume_checkpoint: Checkpoint | None = None
        # Mirrors the live AWAIT_USER payload so a checkpoint can capture a run
        # suspended on an ask-back (#10 → #1). Cleared once the user replies (or
        # the ask times out, which emits USER_INPUT_RECEIVED with value=None).
        self._pending_user: dict[str, Any] | None = None
        on = getattr(self.bus, "on", None)
        if callable(on):
            from ..events.types import AWAIT_USER, USER_INPUT_RECEIVED
            on(AWAIT_USER, self._on_await_user)
            on(USER_INPUT_RECEIVED, self._on_user_reply)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    def _preflight_git_check(self) -> None:
        """Validate git repo state before starting. Fail fast if dirty."""
        cwd = self.config.cwd

        try:
            dirty = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=cwd, stderr=subprocess.DEVNULL, text=True,
            ).strip()
        except subprocess.CalledProcessError:
            _print_status("Warning: not a git repository, skipping pre-flight checks")
            return

        if dirty:
            _print_status("ERROR: Repository has uncommitted changes:")
            for line in dirty.splitlines()[:10]:
                _print_status(f"  {line}")
            raise RuntimeError(
                "Repository is dirty. Commit or stash changes before starting "
                "a research run to ensure main stays clean."
            )

        current_branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True,
        ).strip()

        expected_branches = (
            (self.config.base_branch,)
            if self.config.base_branch
            else ("main", "master")
        )
        if current_branch not in expected_branches:
            expected = self.config.base_branch or "main/master"
            if self.config.require_base_branch:
                _print_status(
                    f"ERROR: currently on branch '{current_branch}', expected clean base branch {expected}."
                )
                raise RuntimeError(
                    "Refusing to create a research trunk from a non-base branch. "
                    "Checkout the base branch first, or launch with --allow-non-base-branch "
                    "if this is intentional."
                )
            _print_status(
                f"Warning: currently on branch '{current_branch}', expected 'main' or 'master'. "
                f"The trunk branch will be created from this HEAD."
            )

        _print_status("Pre-flight checks passed.")

    # ------------------------------------------------------------------
    # .gitignore enforcement
    # ------------------------------------------------------------------

    _GITIGNORE_ENTRIES = (
        ".arbor/",
        ".autoresearch/",  # legacy — keep one release for projects with old session dirs
        ".coordinator/",
        "results/",
        "cache_old_*/",
    )

    def _ensure_gitignore(self) -> None:
        """Ensure target repo's .gitignore excludes agent artifacts.

        Adds missing entries and commits the change so the working tree
        stays clean for the trunk branch checkout. Safe to call even when
        the cwd isn't a git repository — it silently returns in that case.
        """
        cwd = Path(self.config.cwd)
        inside = _git_output(str(cwd), "rev-parse", "--is-inside-work-tree")
        if inside != "true":
            return
        top = _git_output(str(cwd), "rev-parse", "--show-toplevel")
        if top:
            cwd = Path(top)
        gi_path = cwd / ".gitignore"

        existing = gi_path.read_text(encoding="utf-8") if gi_path.exists() else ""
        existing_lines = set(existing.splitlines())

        missing = [e for e in self._GITIGNORE_ENTRIES if e not in existing_lines]
        if not missing:
            return

        addition = "\n".join(missing)
        new_content = existing.rstrip("\n") + "\n" + addition + "\n" if existing else addition + "\n"
        gi_path.write_text(new_content, encoding="utf-8")

        try:
            subprocess.check_call(
                ["git", "add", ".gitignore"], cwd=str(cwd),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.check_call(
                [
                    "git",
                    "-c", "user.name=AutoResearch",
                    "-c", "user.email=autoresearch@example.com",
                    "commit", "-m", "chore: gitignore research agent artifacts",
                ],
                cwd=str(cwd),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            _print_status(f"Warning: failed to commit .gitignore update ({exc})")
            return
        _print_status(f"Added {missing} to .gitignore and committed.")

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> str:
        """Run the coordinator."""
        from ..events.types import SESSION_START, SESSION_END
        try:
            from ..core.agent import AgentStats
            AgentStats.reset()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        _print_banner(self.config)

        self.bus.emit(SESSION_START, {
            "task": self.config.task,
            "cwd": str(self.config.cwd),
            "provider": self.config.provider,
            "model": self.config.model,
        })
        _session_t0 = time.monotonic()

        # Order matters: ensure our own artifact dirs are gitignored *before*
        # checking for dirtiness, so the session dir the CLI just created
        # under .autoresearch/ doesn't trip the dirty check.
        self._ensure_gitignore()
        self._preflight_git_check()

        # Checkout working trunk branch if specified (keeps main clean)
        if self.config.trunk_branch:
            try:
                subprocess.check_call(
                    ["git", "checkout", self.config.trunk_branch],
                    cwd=self.config.cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _print_status(f"Checked out working trunk: {self.config.trunk_branch}")
            except subprocess.CalledProcessError as e:
                _print_status(f"Warning: Could not checkout trunk branch {self.config.trunk_branch}: {e}")

        # ── on_workspace_setup lifecycle hook ────────────────────────────
        await self._run_workspace_setup_hook()

        # Initialize or load tree
        self._initialize_tree()

        # ── Prefill eval_contract from plugin ────────────────────────────
        self._apply_eval_contract()
        await self._assess_contamination()

        # Create the single persistent agent
        agent = self._create_coordinator()
        self._wire_ideate_prune(agent)

        # On resume, replay the prior message history so the reasoning chain
        # survives a process restart, then nudge the agent to continue (#1).
        resumed = False
        if self.config.resume:
            replayed = read_messages(self._messages_path)
            if replayed:
                agent.messages.extend(seal_interrupted_tail(replayed))
                self._append_resume_nudge(agent, self._build_resume_prompt())
                # The pending question is now in the replayed message history;
                # drop the live copy so it isn't re-persisted and re-surfaced on
                # every subsequent resume (a ghost question that never clears).
                self._pending_user = None
                resumed = True
                _print_status(
                    f"Resumed {len(replayed)} prior message(s) from checkpoint."
                )
            else:
                _print_status(
                    "No saved message history found; resuming from the idea tree only."
                )
        # When resumed, the nudge is already in agent.messages; pass "" so
        # Agent.run continues without appending a second user turn.
        prompt = "" if resumed else self._build_initial_prompt()

        _print_status("Starting coordinator (single persistent ReAct loop)...")
        t0 = time.monotonic()

        # ── Run with optional time budget ────────────────────────────────
        emergency_timeout = False
        budget = self.config.time_budget
        if budget is not None:
            finalization_fraction = self.config.budget_policy.normalized_finalization_fraction()
            cutoff = int(budget * (1.0 - finalization_fraction))
            _print_status(
                f"Time budget: {budget}s total, "
                f"agent cutoff at {cutoff}s ({1.0 - finalization_fraction:.0%}), "
                f"finalization buffer: {budget - cutoff}s"
            )
            try:
                result = await asyncio.wait_for(agent.run(prompt), timeout=cutoff)
            except asyncio.TimeoutError:
                emergency_timeout = True
                elapsed_so_far = time.monotonic() - t0
                _print_status(
                    f"EMERGENCY TIMEOUT at {elapsed_so_far:.0f}s — "
                    f"forcing finalization"
                )
                result = (
                    f"[Emergency timeout after {elapsed_so_far:.0f}s / "
                    f"{budget}s budget]"
                )
        else:
            result = await agent.run(prompt)

        # Wait for any background SearchAgents to flush their results onto
        # the tree before we exit. They run concurrently with the coordinator
        # by design (see coordinator/tools/search_ctx.py); if the loop closes
        # while one is still in flight, its task gets cancelled and the
        # node's related_work is left empty.
        from .tools.search_ctx import (
            pending_search_count,
            wait_for_pending_searches,
        )
        n_pending = pending_search_count()
        if n_pending:
            _print_status(
                f"Waiting for {n_pending} background SearchAgent(s) to finish..."
            )
            await wait_for_pending_searches()
            _print_status("All background SearchAgents flushed.")

        # Persist a final checkpoint so the next --resume picks up the latest
        # message history and tree state (#1).
        self._write_checkpoint(reason="shutdown", messages=getattr(agent, "messages", None))

        elapsed = time.monotonic() - t0
        try:
            from ..core.agent import AgentStats
            stats_global = AgentStats.snapshot()
        except Exception:  # pylint: disable=broad-exception-caught
            stats_global = {}
        _print_status(
            f"Coordinator completed in {elapsed:.0f}s: "
            f"{agent.total_turns} turns, "
            f"coordinator {agent.total_input_tokens} in / "
            f"{agent.total_output_tokens} out tokens; "
            f"all arbor LLM "
            f"{stats_global.get('total_input_tokens', 0)} in / "
            f"{stats_global.get('total_output_tokens', 0)} out tokens"
        )

        # ── on_finalize lifecycle hook ───────────────────────────────────
        await self._run_finalize_hook()

        # ── Emergency: recover best submission ───────────────────────────
        if emergency_timeout:
            self._recover_best_submission()

        # ── Persist run stats to workspace ───────────────────────────────
        self._write_run_stats(
            elapsed_seconds=elapsed,
            coordinator_turns=agent.total_turns,
            meta_input_tokens=agent.total_input_tokens,
            meta_uncached_input_tokens=agent.total_uncached_input_tokens,
            meta_cache_read_tokens=agent.total_cache_read_tokens,
            meta_cache_creation_tokens=agent.total_cache_creation_tokens,
            meta_output_tokens=agent.total_output_tokens,
            emergency_timeout=emergency_timeout,
        )

        self.bus.emit(SESSION_END, {
            "duration": time.monotonic() - _session_t0,
            "exit_reason": "timeout" if emergency_timeout else "ok",
            "turns": agent.total_turns,
            "input_tokens": stats_global.get("total_input_tokens", agent.total_input_tokens),
            "output_tokens": stats_global.get("total_output_tokens", agent.total_output_tokens),
            "meta_input_tokens": agent.total_input_tokens,
            "meta_output_tokens": agent.total_output_tokens,
        })

        return self._build_final_report(result)

    # ------------------------------------------------------------------
    # Plugin lifecycle hooks
    # ------------------------------------------------------------------

    async def _run_workspace_setup_hook(self) -> None:
        """Run on_workspace_setup lifecycle hook before INIT."""
        plugin = self.config.plugin
        if not plugin or not plugin.lifecycle_hooks:
            return
        hook = plugin.lifecycle_hooks.get("on_workspace_setup")
        if not hook:
            return
        script = hook.get("script") if isinstance(hook, dict) else None
        if script:
            _print_status(f"Running on_workspace_setup script: {script}")
            output = await _run_lifecycle_script(
                script,
                self.config.cwd,
                timeout=self.config.lifecycle_script_timeout,
                workspace_dir=self.config.workspace_dir,
            )
            if output:
                _print_status(f"  Setup script output: {output[:200]}")

    async def _run_finalize_hook(self) -> None:
        """Run on_finalize lifecycle hook after STOP or timeout."""
        plugin = self.config.plugin
        if not plugin or not plugin.lifecycle_hooks:
            return
        hook = plugin.lifecycle_hooks.get("on_finalize")
        if not hook:
            return
        script = hook.get("script") if isinstance(hook, dict) else None
        prompt = hook.get("prompt") if isinstance(hook, dict) else None
        if script:
            _print_status(f"Running on_finalize script: {script}")
            output = await _run_lifecycle_script(
                script,
                self.config.cwd,
                timeout=self.config.lifecycle_script_timeout,
                workspace_dir=self.config.workspace_dir,
            )
            if output:
                _print_status(f"  Finalize script output: {output[:200]}")
        if prompt:
            _print_status(f"Finalize prompt (for reference): {prompt[:200]}")

    def _apply_eval_contract(self) -> None:
        """Prefill tree.meta with eval_contract values from plugin."""
        plugin = self.config.plugin
        if not plugin or not plugin.eval_contract:
            return
        for key in ("metric_direction", "eval_cmd", "submission_path", "sample_submission_path"):
            if key in plugin.eval_contract and plugin.eval_contract[key] is not None:
                if self.tree.meta.get(key) is None:
                    self.tree.meta[key] = plugin.eval_contract[key]
        self.tree.save()
        _print_status(f"Applied eval_contract from plugin '{plugin.name}': {plugin.eval_contract}")

    async def _assess_contamination(self) -> None:
        """Run the contamination probe once and record it in meta + an event.

        Non-blocking: the probe itself never raises, and this wrapper swallows
        anything unexpected so INIT cannot be derailed by a contamination check.
        """
        if not getattr(self.config, "contamination_probe", True):
            return
        from .contamination import ContaminationProbe
        from ..events import types as ev

        plugin = self.config.plugin
        eval_contract = dict(getattr(plugin, "eval_contract", {}) or {})
        # The contract comes from the plugin only. meta["contamination"] holds
        # *our own* report from a prior run; feeding it back as an input would
        # re-assess a report-shaped dict and downgrade the result to "unknown".
        if not eval_contract.get("contamination"):
            return  # nothing declared — skip silently
        try:
            report = await ContaminationProbe().assess(
                dataset_info=self.tree.meta.get("dataset_info"),
                eval_contract=eval_contract,
                model=getattr(self.config, "model", None),
                provider=None,  # active probe stays stubbed for now
                timeout=getattr(self.config, "contamination_timeout", 60),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("contamination: assessment failed: %s", exc)
            return
        self.tree.meta["contamination"] = report.to_dict()
        self.tree.save()
        self.bus.emit(ev.CONTAMINATION_ASSESSED, {
            "status": report.status, "reasons": report.reasons,
        })
        if report.status in {"warn", "contaminated"}:
            _print_status(
                f"  Contamination: {report.status} — {'; '.join(report.reasons)}"
            )

    def _recover_best_submission(self) -> None:
        """On emergency timeout, ensure the best submission is at the workspace root.

        Priority:
        1. submission.csv already on trunk (current best merged version)
        2. Best snapshot from submissions/ directory (scored via tree nodes)
        """
        import shutil

        plugin = self.config.plugin
        submission_rel = "submission.csv"
        if plugin and plugin.eval_contract:
            submission_rel = plugin.eval_contract.get("submission_path", "submission.csv")

        cwd = Path(self.config.cwd)
        trunk_submission = cwd / submission_rel

        if trunk_submission.exists():
            _print_status(f"Emergency recovery: trunk already has {submission_rel}")
            return

        snapshot_root = Path(self.config.workspace_dir) if self.config.workspace_dir else cwd
        snapshot_dir = snapshot_root / "submissions"
        if not snapshot_dir.is_dir():
            _print_status("Emergency recovery: no submissions/ directory found")
            return

        best_node = self.tree.get_best_done_node()
        if best_node is not None:
            ext = Path(submission_rel).suffix or ".csv"
            candidate = snapshot_dir / f"{best_node.id}{ext}"
            if candidate.exists():
                shutil.copy2(candidate, trunk_submission)
                _print_status(
                    f"Emergency recovery: copied best submission "
                    f"from {candidate.name} (score={best_node.score})"
                )
                return

        snapshots = sorted(snapshot_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if snapshots:
            shutil.copy2(snapshots[0], trunk_submission)
            _print_status(
                f"Emergency recovery: copied most recent submission "
                f"from {snapshots[0].name}"
            )
        else:
            _print_status("Emergency recovery: no submission snapshots found")

    # ------------------------------------------------------------------
    # Agent factory — called once
    # ------------------------------------------------------------------

    def _create_coordinator(self) -> Agent:
        """Create the single persistent Agent with coordinator tools."""
        tools = get_coordinator_tools(self.tree, self.config, self.provider)
        system_prompt = build_coordinator_system_prompt(self.config)
        # Anchor for KV-cache stability across a resume (#13/#1): the stable
        # system prefix hash is recorded in the checkpoint.
        self._system_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()

        # On resume, if the coordinator's system prefix changed since the
        # checkpoint (different model/plugin/config), the provider's KV cache is
        # cold — note it so a slower, costlier first turn isn't a surprise.
        if self._resume_checkpoint is not None:
            prior_hash = self._resume_checkpoint.cache.stable_system_hash
            if prior_hash and prior_hash != self._system_hash:
                _print_status(
                    "  Note: coordinator system prompt changed since the checkpoint "
                    "— prompt cache will be cold for the first resumed turn."
                )

        # Share the coordinator config's llm/timeout/context subgroups; override only
        # the coordinator's own model (effective_meta_model) and runtime knobs.
        agent_config = AgentConfig(
            llm=self.config.llm.model_copy(deep=True, update={"model": self.config.effective_meta_model}),
            timeout=self.config.timeout.model_copy(deep=True),
            context=self.config.context.model_copy(deep=True),
            cwd=self.config.cwd,
            max_turns=self.config.max_turns,
            max_tool_concurrency=5,
            auto_git=False,
            verbose=self.config.verbose,
            workspace_dir=self.config.workspace_dir,
            agent_label="coordinator",
            token_trace_path=(
                str(Path(self.config.workspace_dir) / "tokens.jsonl")
                if getattr(self.config, "token_trace", False) and self.config.workspace_dir
                else None
            ),
            inter_turn_user_messages=_drain_dashboard_messages,
            checkpoint_hook=lambda msgs, turn: self._write_checkpoint(
                reason="turn", messages=msgs
            ),
            event_bus=self.bus,
        )

        return Agent(
            provider=self.provider,
            tools=tools,
            system_prompt=system_prompt,
            config=agent_config,
        )

    # ------------------------------------------------------------------
    # IDEATE context pruning hookup
    # ------------------------------------------------------------------

    def _wire_ideate_prune(self, agent: Agent) -> None:
        """After Agent exists, wire TreeAddNode to elide IDEATE scratch work
        (skill bodies + reasoning) from ``agent.messages`` on each successful
        commit. See context_prune.py for rewrite rules.

        No-op when skills are disabled — prune anchors on the skill-driven
        IDEATE flow, so without skills there is nothing meaningful to elide.
        """
        if not self.config.skills_enabled:
            return
        messages_ref = agent.messages  # list is captured by reference
        for tool in agent.tools.values():
            if isinstance(tool, TreeAddNodeTool):
                tool.set_prune_hook(lambda: prune_ideate_context(messages_ref))

    # ------------------------------------------------------------------
    # Initial prompt
    # ------------------------------------------------------------------

    def _eval_info_parts(self, header: str) -> list[str]:
        """Render eval commands / dataset info from tree.meta, if any.

        Surfaced in prompts so the eval contract survives context compression
        and a process restart.
        """
        lines: list[str] = []
        if self.tree.meta.get("eval_cmd"):
            lines.append(f"- B_dev eval: `{self.tree.meta['eval_cmd']}`")
        if self.tree.meta.get("eval_cmd_test"):
            lines.append(f"- B_test eval: `{self.tree.meta['eval_cmd_test']}`")
        if self.tree.meta.get("dataset_info"):
            lines.append(f"- Datasets: {self.tree.meta['dataset_info']}")
        if not lines:
            return []
        return [f"## {header}\n\n" + "\n".join(lines)]

    def _build_resume_prompt(self) -> str:
        """Short continuation message for a resumed run (#1).

        The full prior conversation has already been replayed into the agent's
        messages, so this only re-orients it: skip INIT, note re-queued nodes,
        and continue the loop.
        """
        parts: list[str] = []
        if self.config.task:
            parts.append(f"## Research Task\n\n{self.config.task}")

        pending = [n.id for n in self.tree.get_nodes_by_status("pending") if n.depth > 0]
        requeued = (
            f"\n\nThese idea node(s) are pending (some were re-queued after being "
            f"interrupted mid-experiment): {', '.join(pending)}. Re-dispatch them "
            f"with RunExecutor if they are still promising."
            if pending else ""
        )
        parts.append(
            "## Resuming Interrupted Session\n\n"
            "You are resuming a research run that was interrupted. Your full prior "
            "conversation (reasoning, tool calls, and results) has been restored "
            "above, and the idea tree is loaded from disk. **Do NOT restart from "
            "INIT** — the baseline and setup are already done." + requeued + "\n\n"
            "Call TreeView to refresh your view, then continue the iterative "
            "research loop from where you left off."
        )
        pending_note = _resume_pending_user_note(self._pending_user)
        if pending_note:
            parts.append(pending_note)
        parts.extend(self._eval_info_parts("Evaluation Info (from previous session)"))
        return "\n\n".join(parts)

    @staticmethod
    def _append_resume_nudge(agent: Agent, nudge: str) -> None:
        """Add the resume nudge while preserving user/assistant alternation.

        The replayed history usually ends on a user turn (checkpoints are taken
        at a turn boundary), and most providers reject two user turns in a row.
        So fold the nudge into a trailing user message instead of appending a
        new one; only start a fresh user turn when the tail is an assistant turn.
        """
        msgs = agent.messages
        if msgs and msgs[-1].get("role") == "user":
            last = msgs[-1]
            content = last.get("content")
            if isinstance(content, str):
                last["content"] = f"{content}\n\n{nudge}"
            elif isinstance(content, list):
                last["content"] = content + [{"type": "text", "text": nudge}]
            else:
                msgs.append({"role": "user", "content": nudge})
        else:
            msgs.append({"role": "user", "content": nudge})

    def _build_initial_prompt(self) -> str:
        """Build the single initial user message for a fresh run."""
        parts: list[str] = []

        # Task
        if self.config.task:
            parts.append(f"## Research Task\n\n{self.config.task}")

        baseline = self.tree.meta.get("baseline_score")

        if baseline is not None:
            # New tree but baseline loaded from cache
            parts.append(
                f"## Cached Baseline (from a previous run)\n\n"
                f"Baseline score: **{baseline:.1f}%** (loaded from baseline cache)\n\n"
                f"The baseline evaluation was already run in a previous session with the "
                f"same model. You do NOT need to re-run the baseline evaluation.\n\n"
                f"You still need to:\n"
                f"1. Explore the codebase to understand its structure\n"
                f"2. Verify the evaluation commands are correct\n"
                f"3. Record the metadata via TreeSetMeta if not already set "
                f"(baseline_score, trunk_score, eval_cmd, etc.)\n"
                f"4. Then begin the iterative research loop"
            )
            parts.extend(self._eval_info_parts("Evaluation Info (from cache)"))
        else:
            parts.append(
                "## Getting Started\n\n"
                "No previous tree exists. You MUST follow Step 0 (INIT) first:\n"
                "1. Explore the codebase to understand its structure\n"
                "2. Find the evaluation scripts and datasets (B_dev and B_test)\n"
                "3. Run the baseline evaluation on B_dev\n"
                "4. Record baseline_score and trunk_score via TreeSetMeta\n"
                "5. Then begin the iterative research loop"
            )

        # Guidelines
        depth_note = ""
        if self.config.max_tree_depth is not None:
            depth_note = f" (max depth: {self.config.max_tree_depth})"
        parts.append(
            f"## Guidelines\n\n"
            f"- Hard cap: {self.config.max_cycles} arbor cycles{depth_note} "
            f"— RunExecutor / RunExecutorParallel will refuse once "
            f"done+merged+pruned+failed nodes reach this number\n"
            f"- Merge threshold: ~{self.config.merge_threshold}% improvement "
            f"(confirmed on B_test before merging)\n"
            f"- The Idea Tree is auto-saved to disk on every change — "
            f"it survives context compressions\n"
            f"- Use TreeView to refresh your view of the tree at any time\n"
            f"- You have Bash/Read/Grep/Glob tools — use them to understand "
            f"the codebase, run evaluations, and verify results directly\n"
            f"- When dispatching Executors, always include the eval command "
            f"and B_dev path in additional_context"
        )

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Tree initialization
    # ------------------------------------------------------------------

    def _initialize_tree(self) -> None:
        """Load an existing run (``--resume``) or create a fresh tree.

        Single track (#1): resume is gated on ``config.resume``. A fresh run
        refuses to clobber existing state, and a resume requires it — so there
        is exactly one way to continue a previous run.
        """
        json_path = self.config.tree_json_path
        md_path = self.config.tree_md_path

        if self.config.resume:
            if not json_path.exists():
                raise RuntimeError(
                    f"--resume was given but no idea tree was found at {json_path}. "
                    f"Nothing to resume. Start a fresh run without --resume."
                )
            _print_status("Resuming previous run: loading idea tree...")
            try:
                self.tree = IdeaTree.load_json(json_path)
            except (ValueError, KeyError, OSError) as exc:
                # The idea tree IS the run's state — if it's corrupt or
                # truncated (e.g. a crash mid-write) we genuinely can't
                # resume. Surface a clear, actionable message instead of a
                # cryptic JSONDecodeError repr in the "run failed" panel.
                raise RuntimeError(
                    f"the saved idea tree at {json_path} is corrupt or truncated "
                    f"({type(exc).__name__}: {exc}) — cannot resume this run. "
                    f"Start a fresh run without --resume."
                ) from exc
            self.tree.bus = self.bus
            if self.config.max_tree_depth is not None:
                self.tree.max_depth = self.config.max_tree_depth

            # Re-queue executors that were in flight at interruption: a node
            # left "running" never completed, so reset it to "pending".
            for node in self.tree.get_nodes_by_status("running"):
                _print_status(f"  Re-queuing interrupted node {node.id} -> pending")
                self.tree.update_node(node.id, status="pending")

            # The checkpoint is optional metadata (cache anchors, git state,
            # pending_user) — the tree + messages are the real state. A corrupt
            # checkpoint must not abort an otherwise-resumable run.
            try:
                self._resume_checkpoint = read_checkpoint(self._checkpoint_path)
            except Exception as exc:
                _print_status(
                    f"  checkpoint at {self._checkpoint_path} unreadable "
                    f"({type(exc).__name__}) — resuming without it"
                )
                self._resume_checkpoint = None

            # Restore a suspended human-in-the-loop question so the resumed run
            # knows it was paused mid-question (surfaced in the resume prompt).
            if self._resume_checkpoint is not None:
                pending = self._resume_checkpoint.pending_user
                self._pending_user = copy.deepcopy(pending) if pending else None
                if self._pending_user:
                    _print_status(
                        "  Restored a pending user question from the checkpoint"
                    )
            return

        if json_path.exists():
            raise RuntimeError(
                f"Existing run state found at {json_path}. Pass --resume to continue "
                f"it, or point --workspace-dir at a clean location for a fresh run."
            )

        _print_status("Creating new idea tree...")
        root = Node(
            id="ROOT",
            parent_id=None,
            depth=0,
            hypothesis=self.config.task or "Research optimization",
            status="done",
        )
        self.tree = IdeaTree(
            root=root,
            json_path=json_path,
            md_path=md_path,
            max_depth=self.config.max_tree_depth,
            bus=self.bus,
        )
        self.tree.save()

        # Try loading cached baseline from a previous run
        self._load_baseline_cache()

    def _load_baseline_cache(self) -> None:
        """Load baseline from .research_baseline.json or results/init/metrics.json."""
        cwd = Path(self.config.cwd)

        # Try workspace cache first, then the committed repo cache.
        cache_paths: list[Path] = []
        if self.config.workspace_dir:
            cache_paths.append(Path(self.config.workspace_dir) / ".coordinator" / "baseline_cache.json")
        cache_paths.append(cwd / ".research_baseline.json")

        for cache_path in cache_paths:
            if not cache_path.exists():
                continue
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                _print_status(f"Warning: failed to read baseline cache {cache_path}: {e}")
                cache = None

            if cache:
                for key in (
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
                ):
                    if cache.get(key) is not None:
                        self.tree.meta[key] = cache[key]
                self.tree.save()
                _print_status(
                    f"Loaded cached baseline from {cache_path}: score={cache.get('baseline_score')}"
                )
                return

        # Fallback: check for committed baseline result files
        metrics_path = cwd / "results" / "init" / "metrics.json"
        if metrics_path.exists():
            try:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                score = metrics.get("accuracy")
                if score is not None:
                    score_pct = score * 100 if score <= 1.0 else score
                    self.tree.meta["baseline_score"] = score_pct
                    self.tree.meta["trunk_score"] = score_pct
                    self.tree.save()
                    _print_status(
                        f"Loaded baseline from results/init/metrics.json: "
                        f"score={score_pct:.1f}%"
                    )
            except (json.JSONDecodeError, OSError) as e:
                _print_status(f"Warning: failed to read results/init/metrics.json: {e}")

    # ------------------------------------------------------------------
    # Checkpoint / resume (#1)
    # ------------------------------------------------------------------

    @property
    def _checkpoint_path(self) -> Path:
        return self.config.coordinator_dir / "checkpoint.json"

    @property
    def _messages_path(self) -> Path:
        return self.config.coordinator_dir / "messages.jsonl"

    def _on_await_user(self, event: Any) -> None:
        """Record the live ask-back so a checkpoint can capture it (#10 → #1)."""
        self._pending_user = copy.deepcopy(dict(getattr(event, "data", None) or {}))

    def _on_user_reply(self, event: Any) -> None:
        """The pending ask-back was answered (or timed out) — clear it."""
        self._pending_user = None

    @property
    def _run_name(self) -> str:
        if self.config.workspace_dir:
            return Path(self.config.workspace_dir).name
        return self.config.git_branch_prefix or "run"

    def _collect_git_state(self) -> GitState:
        """Best-effort git topology for the checkpoint (never raises)."""
        cwd = self.config.cwd
        active: list[str] = []
        worktrees: list[str] = []
        try:
            prefix = self.config.git_branch_prefix
            branches = _git_output(cwd, "branch", "--list", f"{prefix}/*") or ""
            # `git branch` lines carry a 2-char status prefix ("  ", "* ", "+ ").
            active = [
                ln[2:].strip()
                for ln in branches.splitlines()
                if ln.strip()
            ]
            wt = _git_output(cwd, "worktree", "list", "--porcelain") or ""
            worktrees = [
                ln[len("worktree "):].strip()
                for ln in wt.splitlines()
                if ln.startswith("worktree ")
            ]
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        return GitState(
            trunk_branch=self.config.trunk_branch,
            active_branches=active,
            worktrees=worktrees,
        )

    def _build_checkpoint(self) -> Checkpoint:
        """Snapshot the live run state into a :class:`Checkpoint`."""
        tree = self.tree
        cycle_num = _completed_cycles(tree)
        baseline = tree.meta.get("baseline_score")
        inflight = [
            InflightExecutor(
                node_id=node.id,
                branch=node.code_ref or _compute_branch_name(self.config, node.id, node.hypothesis),
            )
            for node in tree.get_nodes_by_status("running")
        ]
        return Checkpoint(
            run_name=self._run_name,
            cycle_num=cycle_num,
            phase="research" if baseline is not None else "init",
            tree_path="idea_tree.json",
            messages_path="messages.jsonl",
            git=self._collect_git_state(),
            inflight_executors=inflight,
            cache=CacheAnchor(stable_system_hash=self._system_hash),
            pending_user=self._pending_user,
        )

    def _write_checkpoint(
        self, *, reason: str, messages: list[dict[str, Any]] | None = None
    ) -> None:
        """Persist messages + checkpoint atomically; emits CHECKPOINT_SAVED.

        Defensive: a checkpoint failure must never abort a research run, and
        the orchestrator may be used in tests without a tree.
        """
        if self.tree is None:
            return
        try:
            if messages is not None:
                write_messages(self._messages_path, messages)
            write_checkpoint(
                self._checkpoint_path,
                self._build_checkpoint(),
                reason=reason,
                bus=self.bus,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.warning("Failed to write checkpoint (%s): %s", reason, exc)

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------

    def _write_run_stats(
        self,
        *,
        elapsed_seconds: float,
        coordinator_turns: int,
        meta_input_tokens: int,
        meta_uncached_input_tokens: int,
        meta_cache_read_tokens: int,
        meta_cache_creation_tokens: int,
        meta_output_tokens: int,
        emergency_timeout: bool,
    ) -> None:
        """Dump aggregated exploration stats to ``<workspace>/run_stats.json``.

        Captures wall-clock time, total tokens used across every Agent
        instance (meta + executors + search-agents + context compaction),
        LLM call count, and node-level iteration counts from the idea tree.
        """
        if not self.config.workspace_dir:
            return
        try:
            from ..core.agent import AgentStats
            stats_global = AgentStats.snapshot()
        except Exception:  # pylint: disable=broad-exception-caught
            stats_global = {}

        node_counts: dict[str, int] = {}
        scored_nodes = 0
        best_score: float | None = None
        try:
            for n in self.tree.get_all_nodes():
                if n.depth == 0:
                    continue
                node_counts[n.status] = node_counts.get(n.status, 0) + 1
                if n.score is not None:
                    scored_nodes += 1
                    if best_score is None or n.score > best_score:
                        best_score = n.score
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        meta = {}
        try:
            meta = dict(self.tree.meta)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        stats = {
            "schema_version": 1,
            "token_scope": (
                "research_agent_process_llm_only; excludes LLM calls made by "
                "external Bash/RunTraining/eval subprocesses"
            ),
            "duration_seconds": int(elapsed_seconds),
            "duration_human": _fmt_duration(int(elapsed_seconds)),
            "emergency_timeout": emergency_timeout,
            "iterations": {
                "total_nodes": sum(node_counts.values()),
                "scored_nodes": scored_nodes,
                "by_status": node_counts,
                "best_score": best_score,
                "baseline_score": meta.get("baseline_score"),
                "trunk_score": meta.get("trunk_score"),
                "test_baseline_score": meta.get("test_baseline_score"),
                "test_trunk_score": meta.get("test_trunk_score"),
            },
            "coordinator": {
                "turns": coordinator_turns,
                "input_tokens": meta_input_tokens,
                "uncached_input_tokens": meta_uncached_input_tokens,
                "cache_read_tokens": meta_cache_read_tokens,
                "cache_creation_tokens": meta_cache_creation_tokens,
                "output_tokens": meta_output_tokens,
                "total_tokens": meta_input_tokens + meta_output_tokens,
            },
            "all_agents": stats_global,
            "model": getattr(self.provider, "model", None),
            "provider": getattr(self.provider, "__class__", type(self.provider)).__name__,
        }
        try:
            out = Path(self.config.workspace_dir) / "run_stats.json"
            out.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
            _print_status(f"Wrote run stats: {out}")
        except OSError as e:
            _print_status(f"Warning: failed to write run_stats.json: {e}")

        # Self-evolution line 1: dump a training-ready decision trace alongside the
        # stats. Best-effort — never fail a finished run over an export. Off via
        # `evolution.export_trajectory: false`.
        if getattr(self.config, "export_trajectory", True):
            try:
                from ..trajectory import write_trajectory
                _print_status(f"Wrote trajectory: {write_trajectory(self.config.workspace_dir)}")
            except Exception as e:  # pylint: disable=broad-exception-caught
                _print_status(f"Warning: failed to write trajectory: {e}")

        # Self-evolution line 2: distill insights into a reusable cross-run skill.
        if getattr(self.config, "distill_skills", False):
            try:
                from ..distill import distill_to_session
                prov = self.provider if getattr(self.config, "distill_abstract", False) else None
                p = distill_to_session(self.config.workspace_dir, provider=prov)
                _print_status(f"Distilled experience: {p}" if p else "Distill: nothing to learn")
            except Exception as e:  # pylint: disable=broad-exception-caught
                _print_status(f"Warning: failed to distill skill: {e}")

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------

    def _build_final_report(self, agent_result: str) -> str:
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("COORDINATOR FINAL REPORT")
        lines.append("=" * 60)

        baseline = self.tree.meta.get("baseline_score")
        trunk = self.tree.meta.get("trunk_score")
        test_baseline = self.tree.meta.get("test_baseline_score")
        test_trunk = self.tree.meta.get("test_trunk_score")

        # Test scores (primary metric)
        if test_baseline is not None or test_trunk is not None:
            lines.append("\n--- TEST SET (primary) ---")
            lines.append(f"  Baseline (test): {_fmt_score(test_baseline)}")
            lines.append(f"  Final (test):    {_fmt_score(test_trunk)}")
            if test_baseline is not None and test_trunk is not None:
                lines.append(f"  Improvement:     {test_trunk - test_baseline:+.1f}%")

        # Dev scores (for reference)
        lines.append("\n--- DEV SET (iteration) ---")
        lines.append(f"  Baseline (dev): {_fmt_score(baseline)}")
        lines.append(f"  Final (dev):    {_fmt_score(trunk)}")

        if baseline is not None and trunk is not None:
            lines.append(f"  Improvement:    {trunk - baseline:+.1f}%")

        # Node stats
        all_nodes = self.tree.get_all_nodes()
        by_status: dict[str, int] = {}
        for n in all_nodes:
            if n.depth == 0:
                continue
            by_status[n.status] = by_status.get(n.status, 0) + 1

        if by_status:
            lines.append("\nNodes:")
            for status, count in sorted(by_status.items()):
                lines.append(f"  {status}: {count}")

        # Best results
        done_nodes = [
            n for n in all_nodes
            if n.status in ("done", "merged") and n.score is not None
        ]
        if done_nodes:
            direction = self.tree.meta.get("metric_direction", "maximize")
            done_nodes.sort(
                key=lambda n: n.score or 0,
                reverse=(direction != "minimize"),
            )
            lines.append("\nTop results:")
            for n in done_nodes[:5]:
                lines.append(f"  {n.id}: {n.hypothesis} (score={n.score:.1f}%, {n.status})")

        # Root insight
        root = self.tree.get_root()
        if root.insight:
            lines.append(f"\nGlobal insight:\n  {root.insight}")

        lines.append("\nArtifacts:")
        lines.append(f"  Tree (JSON): {self.config.tree_json_path}")
        lines.append(f"  Tree (MD):   {self.config.tree_md_path}")

        # Full idea tree
        lines.append(f"\n{'=' * 60}")
        lines.append("IDEA TREE")
        lines.append("=" * 60)
        lines.append(self.tree.to_compact_summary())

        lines.append(f"\n{'=' * 60}")
        lines.append("Agent's final message:")
        lines.append(agent_result[:2000])

        return "\n".join(lines)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _print_banner(config: CoordinatorConfig) -> None:
    """Deprecated. The launch panel is now rendered by ``cli/commands/run.py``
    before the orchestrator starts, so keeping a banner here would just
    duplicate it. Left as a no-op for any direct callers."""
    return


def _print_status(msg: str) -> None:
    from ..cli.style import render_status
    render_status(msg, style="dim", glyph="·")


def _drain_dashboard_messages() -> list[str]:
    """Inter-turn hook: hand the coordinator any messages the user typed
    into the live dashboard since the last turn. Returns [] when no
    dashboard is mounted."""
    try:
        from ..cli import run_state as rs
        state = rs.CURRENT
        if state is None:
            return []
        return state.drain_user_messages()
    except Exception:
        return []


def _fmt_score(val: Any) -> str:
    if val is None:
        return "N/A"
    return f"{val:.1f}%"


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
