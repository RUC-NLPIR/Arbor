"""Context window management and automatic multi-layer compression.

Implements a 4-layer compaction strategy mirroring Claude Code:
1. Truncate oversized tool results
2. Snip old tool results (keep reasoning, drop data)
3. Drop redundant read-only results
4. LLM-powered summarization of old messages
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .llm.base import LLMProvider

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compact prompt — ported from Claude Code's compact/prompt.ts
# ---------------------------------------------------------------------------

COMPACT_SYSTEM_PROMPT = """\
You are a conversation summarizer. Your task is to create a detailed summary \
of the conversation provided below.

Respond with TEXT ONLY. Do NOT call any tools.

Before providing your final summary, wrap your analysis in <analysis> tags to \
organize your thoughts chronologically.

Your summary should include the following sections:

1. **Primary Request and Intent**: What the user originally asked for and the \
   overall goal.
2. **Key Technical Concepts**: Important technical decisions, algorithms, or \
   approaches discussed.
3. **Files and Code Sections**: Full file paths and key code snippets that \
   were read or modified. Include enough code context so the next assistant \
   turn can continue without re-reading every file.
4. **Errors and Fixes**: Any errors encountered and how they were resolved. \
   Include the exact error messages if they were significant.
5. **Experiment Results**: Any experiment runs, commands used, and metrics \
   obtained. Include baseline vs current numbers.
6. **All User Messages**: Preserve EVERY user message verbatim — these \
   contain critical requirements and clarifications.
7. **Pending Tasks**: What remains to be done.
8. **Current Work**: The most recent thing being worked on and its state.
9. **Next Step**: A suggested next action based on the conversation.
"""


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_message_tokens(messages: list[dict[str, Any]], provider: LLMProvider) -> int:
    """Rough token estimate for a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += provider.count_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "") or block.get("content", "")
                    if isinstance(text, str):
                        total += provider.count_tokens(text)
                    inp = block.get("input")
                    if inp:
                        total += provider.count_tokens(json.dumps(inp, ensure_ascii=False))
                elif isinstance(block, str):
                    total += provider.count_tokens(block)
    return total


# ---------------------------------------------------------------------------
# Layer 1: Truncate oversized tool results
# ---------------------------------------------------------------------------

def _truncate_long_tool_results(
    messages: list[dict[str, Any]],
    max_chars_per_result: int = 15_000,
) -> list[dict[str, Any]]:
    """Truncate individual tool results that are excessively long.

    Keeps the first and last portions of long results (head + tail),
    which is more useful than just the head.
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > max_chars_per_result:
                        head_size = max_chars_per_result * 2 // 3
                        tail_size = max_chars_per_result // 3
                        block = {
                            **block,
                            "content": (
                                text[:head_size]
                                + f"\n\n...[truncated {len(text) - head_size - tail_size} chars]...\n\n"
                                + text[-tail_size:]
                            ),
                        }
                new_blocks.append(block)
            result.append({**msg, "content": new_blocks})
        else:
            result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Layer 2: Snip old tool results (keep tool call info, drop result data)
# ---------------------------------------------------------------------------

def _snip_old_tool_results(
    messages: list[dict[str, Any]],
    keep_recent_n: int = 10,
) -> list[dict[str, Any]]:
    """Replace old tool result content with a short placeholder.

    Keeps the tool_use blocks (so the LLM knows what was called) but
    replaces the result with "[snipped — old result]". Preserves the
    most recent N messages' tool results intact.
    """
    # Count messages from the end to find the boundary
    boundary = max(0, len(messages) - keep_recent_n)

    result: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i >= boundary:
            result.append(msg)
            continue

        content = msg.get("content")
        if isinstance(content, list):
            new_blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > 500:
                        # Snip: keep only a short preview
                        block = {
                            **block,
                            "content": text[:200] + "\n...[snipped — old tool result]",
                        }
                new_blocks.append(block)
            result.append({**msg, "content": new_blocks})
        else:
            result.append(msg)

    return result


# ---------------------------------------------------------------------------
# Layer 3: Drop redundant read-only results (if same file read multiple times)
# ---------------------------------------------------------------------------

def _drop_redundant_reads(
    messages: list[dict[str, Any]],
    keep_recent_n: int = 10,
) -> list[dict[str, Any]]:
    """If the same file was Read multiple times, keep only the latest result.

    Scans tool_use blocks for Read calls with the same file_path, and
    snips all but the latest occurrence (within the compactable region).
    """
    boundary = max(0, len(messages) - keep_recent_n)

    # First pass: find all Read calls and their positions
    read_positions: dict[str, list[int]] = {}  # file_path -> [msg indices]
    for i, msg in enumerate(messages):
        if i >= boundary:
            break
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("name") == "Read":
                    fp = (block.get("input") or {}).get("file_path", "")
                    if fp:
                        read_positions.setdefault(fp, []).append(i)

    # Find tool_use IDs that should be snipped (all but last for each file)
    snip_ids: set[str] = set()
    for fp, indices in read_positions.items():
        if len(indices) > 1:
            for idx in indices[:-1]:  # Keep the last one
                content = messages[idx].get("content")
                if isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "tool_use"
                                and block.get("name") == "Read"
                                and (block.get("input") or {}).get("file_path") == fp):
                            snip_ids.add(block.get("id", ""))

    if not snip_ids:
        return messages

    # Second pass: snip the corresponding tool_result blocks
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_blocks: list[Any] = []
            for block in content:
                if (isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and block.get("tool_use_id") in snip_ids):
                    block = {
                        **block,
                        "content": "[snipped — file was re-read later]",
                    }
                new_blocks.append(block)
            result.append({**msg, "content": new_blocks})
        else:
            result.append(msg)

    return result


# ---------------------------------------------------------------------------
# Layer 4: LLM-powered summarization
# ---------------------------------------------------------------------------

def _tool_safe_cut(messages: list[dict[str, Any]], cut: int) -> int:
    """Move a proposed cut back to the last completed tool exchange.

    A cut is safe only when every tool call before it has received a result.
    Tracking IDs also covers parallel calls, results spread over multiple
    messages, and user/background notes inserted in the middle of an exchange.
    Both normalized tool_use blocks and native Responses function calls occur
    in saved histories. Messages and their content blocks are left intact.
    """
    pending: set[str] = set()
    safe_cut = 0
    for i in range(max(0, min(cut, len(messages)))):
        content = messages[i].get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    call_id = block.get("id")
                elif block_type == "function_call":
                    call_id = block.get("call_id") or block.get("id")
                else:
                    call_id = None
                if isinstance(call_id, str) and call_id:
                    pending.add(call_id)
                if block_type in {"tool_result", "function_call_output"}:
                    result_id = block.get("tool_use_id") or block.get("call_id")
                    if isinstance(result_id, str):
                        pending.discard(result_id)
        if not pending:
            safe_cut = i + 1
    return safe_cut


def _format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Format messages into a human-readable transcript for the summarizer."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        inp_preview = json.dumps(block.get("input", {}), ensure_ascii=False)
                        if len(inp_preview) > 500:
                            inp_preview = inp_preview[:500] + "..."
                        text_parts.append(f"[Tool call: {block['name']}({inp_preview})]")
                    elif block.get("type") == "tool_result":
                        result_text = block.get("content", "")
                        if isinstance(result_text, str):
                            if len(result_text) > 2000:
                                result_text = result_text[:2000] + "...[truncated]"
                            text_parts.append(f"[Tool result: {result_text}]")
                elif isinstance(block, str):
                    text_parts.append(block)
            parts.append(f"[{role}]\n" + "\n".join(text_parts))
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main context manager
# ---------------------------------------------------------------------------

class ContextManager:
    """Manages context window size with automatic multi-layer compression.

    Implements Claude Code's compaction strategy:
    Layer 1: Truncate oversized individual tool results (head + tail)
    Layer 2: Snip old tool results (keep call info, drop data)
    Layer 3: Drop redundant reads (same file read multiple times)
    Layer 4: LLM-powered summarization of old messages (last resort)

    Each layer is progressively more aggressive. The manager applies
    layers in order until context fits within the threshold.
    """

    def __init__(
        self,
        provider: LLMProvider,
        context_window: int,
        compact_threshold: float,
        keep_recent: int,
        event_bus: Any | None = None,
        provider_name: str | None = None,
        agent_cwd: str | None = None,
    ):
        self.provider = provider
        self.context_window = context_window
        self.compact_threshold = compact_threshold
        self.keep_recent = keep_recent
        self.event_bus = event_bus
        self.provider_name = provider_name
        self.agent_cwd = agent_cwd
        self._compact_count = 0

    async def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        """Check context size and compress if needed.

        Returns the (possibly compacted) message list.
        """
        system_tokens = self.provider.count_tokens(system_prompt)
        threshold = int(self.context_window * self.compact_threshold)

        def _current_tokens() -> int:
            return system_tokens + _estimate_message_tokens(messages, self.provider)

        if _current_tokens() < threshold:
            return messages

        log.info(
            "Context at ~%d tokens (threshold %d). Starting multi-layer compaction...",
            _current_tokens(), threshold,
        )

        # Layer 1: Truncate oversized tool results
        messages = _truncate_long_tool_results(messages)
        if _current_tokens() < threshold:
            log.info("Layer 1 (truncate tool results) was sufficient.")
            return messages

        # Layer 2: Snip old tool results
        messages = _snip_old_tool_results(messages, keep_recent_n=self.keep_recent)
        if _current_tokens() < threshold:
            log.info("Layer 2 (snip old results) was sufficient.")
            return messages

        # Layer 3: Drop redundant reads
        messages = _drop_redundant_reads(messages, keep_recent_n=self.keep_recent)
        if _current_tokens() < threshold:
            log.info("Layer 3 (drop redundant reads) was sufficient.")
            return messages

        # Layer 4: LLM-powered summarization (most aggressive)
        messages = await self._summarize_old_messages(messages)
        self._compact_count += 1
        log.info("Layer 4 (LLM summarization) complete. Compaction #%d.", self._compact_count)

        # If STILL over threshold after summarization, do emergency truncation
        if _current_tokens() >= threshold:
            log.warning("Still over threshold after Layer 4. Emergency truncation.")
            # Keep a real summary prefix, if present, plus complete recent
            # exchanges. A failed/skipped summary may leave ordinary history;
            # retaining its first two messages could duplicate or split calls.
            prefix_end = (
                _tool_safe_cut(messages, 2)
                if messages and messages[0].get("_internal") == "context_summary"
                else 0
            )
            cut = _tool_safe_cut(messages, len(messages) - max(1, self.keep_recent // 2))
            if cut > prefix_end:
                messages = messages[:prefix_end] + messages[cut:]

        return messages

    async def _summarize_old_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Use the LLM to summarize the oldest messages, keeping recent ones."""
        cut = _tool_safe_cut(messages, len(messages) - max(1, self.keep_recent))
        if cut == 0:
            return messages

        to_summarize = messages[:cut]
        keep = messages[cut:]

        transcript = _format_messages_for_summary(to_summarize)

        # Guard against the transcript itself being too long for summarization
        max_transcript_tokens = self.context_window // 2
        transcript_tokens = self.provider.count_tokens(transcript)
        if transcript_tokens > max_transcript_tokens:
            # Truncate the transcript to fit
            ratio = max_transcript_tokens / transcript_tokens
            transcript = transcript[:int(len(transcript) * ratio)]
            log.info("Transcript truncated for summarization (%d -> %d tokens).",
                      transcript_tokens, max_transcript_tokens)

        try:
            response = await self.provider.create(
                system=COMPACT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": transcript}],
                tools=None,
                max_tokens=20_000,
            )
            summary_text = response.get_text()
            try:
                from .agent import record_llm_usage
                record_llm_usage(
                    response,
                    bus=self.event_bus,
                    provider=self.provider_name,
                    model=self.provider.model,
                    source="context_compaction",
                    agent_cwd=self.agent_cwd,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        except Exception as e:
            log.error("Compaction LLM call failed: %s. Falling back to hard truncation.", e)
            # Drop the oldest half only at a completed tool-exchange boundary.
            cut = _tool_safe_cut(messages, len(messages) // 2)
            return messages[cut:]

        # Replace old messages with summary
        return [
            {
                "role": "user",
                "_internal": "context_summary",
                "content": (
                    f"[Conversation Summary — compaction #{self._compact_count + 1}]\n\n"
                    f"{summary_text}"
                ),
            },
            {
                "role": "assistant",
                "content": (
                    "Understood. I have the full context from the summary above. "
                    "Continuing from where we left off."
                ),
            },
            *keep,
        ]
