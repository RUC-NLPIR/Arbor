# AROS Live Agent Cooperative Commissioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the direct-tool commissioning driver with one clean-wheel run in which the real Arbor `Agent.run` loop completes Task→Eval→Assimilation→restart and an independent verifier proves semantic bytes came from Agent file-tool calls.

**Architecture:** Keep the existing Principal, Research/Task/Run/Eval tools, gateway interface, Git CAS, Task adapter, and evaluator. Add one commissioning-only `LLMProvider` test double, inject the existing cooperative gateway from the host, and replace the old driver/verifier in place; add no compatibility reader, fallback, migration, dependency, scheduler, or configuration layer.

**Tech Stack:** Python 3.11, existing Arbor `Agent`/`LLMProvider`, Typer, pytest, Git, tmux, and current AROS validators.

---

## File map

- Create `commissioning/principal_loop/provider.py`: deterministic, reality-blind provider test double.
- Modify `src/cli/commands/aros_cmd.py`: explicit cooperative `aros start` host injection.
- Modify `scripts/commission_aros_principal_loop.py`: delete direct orchestration and run native Principal twice.
- Modify `scripts/verify_aros_principal_loop_commissioning.py`: verify only current live-Agent evidence.
- Modify `tests/test_aros_public_entry.py` and `tests/test_aros_principal_loop_commissioning_scripts.py`: RED/GREEN coverage.
- Modify `docs/analysis/aros-principal-loop-core-smoke.md`, `docs/aros/README.md`, and `memory/NOW.md`: replace obsolete evidence.

### Task 1: Explicit cooperative authority for `aros start`

**Files:**
- Modify: `src/cli/commands/aros_cmd.py:16-40,756-803`
- Test: `tests/test_aros_public_entry.py`

- [ ] **Step 1: Write failing host-wiring tests**

Add a patched start harness and two tests. The harness captures arguments to the existing `build_principal_agent` seam:

```python
def _capture_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(aros_cmd, "llm_defaults", lambda: {})
    monkeypatch.setattr(aros_cmd, "create_provider", lambda config: object())
    monkeypatch.setattr(aros_cmd, "boot_workspace", lambda root: "exact boot")

    def build(provider: object, root: Path, boot: str, **kwargs: object) -> object:
        captured.update(root=root, boot=boot, kwargs=kwargs)
        return object()

    async def run(agent: object, request: str) -> str:
        captured.update(agent=agent, request=request)
        return "done"

    monkeypatch.setattr(aros_cmd, "build_principal_agent", build)
    monkeypatch.setattr(aros_cmd, "run_principal", run)
    return captured
```

`test_start_has_no_checkpoint_authority_by_default` invokes `aros start` and requires `admission_gateway is None` and `attention_context is None`. `test_start_explicit_cooperative_mode_injects_host_owned_context` invokes `--cooperative-human-direct` and requires a `HumanDirectGateway`, authority state `available`, budget state `not_configured`, and `enforcement_class=cooperative` in both mappings.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_public_entry.py -k 'start_has_no_checkpoint or start_explicit_cooperative'
```

Expected: failure because the option and injected arguments do not exist.

- [ ] **Step 3: Add the minimal option and direct construction**

Import `AttentionAuthorityContext`; add:

```python
cooperative_human_direct: bool = typer.Option(
    False,
    "--cooperative-human-direct",
    help=(
        "Allow explicitly cooperative same-UID checkpoints for this local "
        "Principal session; this is not protected authority."
    ),
),
```

Before `build_principal_agent`, construct existing objects without a settings layer:

```python
gateway = HumanDirectGateway() if cooperative_human_direct else None
attention_context = (
    AttentionAuthorityContext(
        authority={
            "state": "available",
            "enforcement_class": "cooperative",
            "issuer": "human-direct",
        },
        remaining_budget={
            "state": "not_configured",
            "enforcement_class": "cooperative",
        },
        institutional_obligations=(),
    )
    if cooperative_human_direct
    else None
)
```

Pass `admission_gateway=gateway` and `attention_context=attention_context`. Do not alter the Research schema.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_public_entry.py tests/test_aros_principal.py tests/test_aros_research_tool.py
/workspace/Arbor/.venv/bin/ruff check src/cli/commands/aros_cmd.py tests/test_aros_public_entry.py
git diff --check
git add src/cli/commands/aros_cmd.py tests/test_aros_public_entry.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'feat(aros): enable explicit cooperative principal sessions'
```

Expected: all checks exit 0 and commit succeeds.

### Task 2: Deterministic provider test double

**Files:**
- Create: `commissioning/principal_loop/provider.py`
- Test: `tests/test_aros_principal_loop_commissioning_scripts.py`

- [ ] **Step 1: Write failing provider isolation and transition tests**

Add `PROVIDER` and load it with `importlib.util`. Parse its AST and reject imports of `os`, `pathlib`, `shutil`, `subprocess`, or `arbor.aros`. Assert the first response is exactly:

```python
call = response.get_tool_calls()[0]
assert (call.name, call.input) == ("Research", {"action": "attention"})
```

Feed exact single `tool_result` blocks and assert this sequence:

```text
Research.attention -> Task.create -> Task.start -> Task.status+
-> Task.collect -> Eval.run -> Research.attention
-> Read Claim -> Read NOW -> Write Claim -> Write NOW -> Write proposal
-> Research.transition_audit -> Research.checkpoint -> final text
```

Require `ValueError` for an error result, malformed JSON, wrong tool-result ID, failed Task, unrelated Eval candidate, non-valid measurement, missing pending refs, invalid audit, or failed checkpoint. Restart mode must call only `Research.attention`, require no pending returns plus the admitted transition delta, then return final text.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py -k provider
```

Expected: failure because the provider file does not exist.

- [ ] **Step 3: Implement the single provider class**

The module imports only `json`, `Any`, and current `LLMResponse`, `TextBlock`, `ToolUseBlock`, `Usage`. Implement:

```python
class PrincipalLoopProvider:
    model = "aros-principal-loop-fixture"
    base_url = None

    def __init__(self, *, restart: bool = False) -> None:
        self.restart = restart
        self.step = 0
        self.task_id: str | None = None
        self.child_commit: str | None = None
        self.return_commit: str | None = None
        self.eval_id: str | None = None
        self.collected_ref: str | None = None
        self.eval_ref: str | None = None
        self.base_commit: str | None = None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 16_384,
    ) -> LLMResponse:
        del system, tools, max_tokens
        return self._restart(messages) if self.restart else self._primary(messages)
```

Private pure helpers create deterministic `commission-{step:02d}` tool IDs, require one non-error final tool-result block, decode JSON objects, and render complete Claim/NOW/proposal contents. Status `running` repeats `Task.status` without advancing; any other unexpected state fails.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py -k provider
/workspace/Arbor/.venv/bin/ruff check \
  commissioning/principal_loop/provider.py tests/test_aros_principal_loop_commissioning_scripts.py
git diff --check
git add commissioning/principal_loop/provider.py tests/test_aros_principal_loop_commissioning_scripts.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): add deterministic principal provider'
```

### Task 3: Replace direct orchestration with native `Agent.run`

**Files:**
- Modify: `scripts/commission_aros_principal_loop.py`
- Test: `tests/test_aros_principal_loop_commissioning_scripts.py`

- [ ] **Step 1: Write failing source-boundary and real-Agent tests**

Parse the driver and require `build_principal_agent` plus `run_principal`; reject `Driver.task_tool`, `Driver.eval_tool`, `Driver.cooperative_checkpoint`, direct `Tool.execute`, `_claim`, `_now`, and semantic `write_text` after Agent start. Add an integration test that uses the real `Agent` class with fixture tools and asserts `agent.tool_uses` contains the ordered provider sequence ending in `Research.checkpoint`.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py \
  -k 'driver_uses_native_agent or real_agent'
```

Expected: failure against the direct driver.

- [ ] **Step 3: Delete obsolete code and run the primary Agent**

Delete `_checkpoint_service`, `_record_tool_result`, `task_tool`, `eval_tool`, `cooperative_checkpoint`, `_claim`, and `_now`. Preserve initial repository/apparatus setup. After evaluator registration, construct the existing repository binding, cooperative context, `HumanDirectGateway`, provider, and Principal:

```python
provider = PrincipalLoopProvider()
agent = build_principal_agent(
    provider,
    driver.project,
    boot_workspace(driver.project),
    max_turns=32,
    canonical_repository=repository,
    canonical_ref=canonical_ref,
    admission_gateway=HumanDirectGateway(),
    attention_context=context,
)
primary_result = asyncio.run(
    run_principal(agent, "Complete the commissioned research transition.")
)
```

Require `stop_reason == "finished"`; deep-copy tool uses/messages and object IDs. Delete Agent/provider references. Build a new repository observation, `PrincipalLoopProvider(restart=True)`, and Agent; run `"Recover the admitted research state."`. The driver may read exact Git records after Agent completion, but may not write semantic or service-owned paths.

- [ ] **Step 4: Replace the evidence object without a legacy branch**

Use exactly these top-level keys:

```python
evidence = {
    "schema_version": 1,
    "enforcement_class": "cooperative",
    "project": str(driver.project),
    "task": task_evidence,
    "eval": eval_evidence,
    "checkpoint": checkpoint_evidence,
    "agent": primary_agent_evidence,
    "restart": restart_agent_evidence,
    "commands": driver.commands,
}
```

`agent` contains class name, Agent/provider object IDs, stop reason, result, ordered tool uses, and message hash. `restart` contains different IDs, its one attention call, result, message hash, and decoded packet. Remove old complete/missing/rebuilt cache packet fields entirely.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py tests/test_aros_principal.py
/workspace/Arbor/.venv/bin/ruff check \
  commissioning/principal_loop/provider.py scripts/commission_aros_principal_loop.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git diff --check
git add scripts/commission_aros_principal_loop.py tests/test_aros_principal_loop_commissioning_scripts.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'feat(aros): commission through native agent loop'
```

### Task 4: Replace verifier with live-Agent checks

**Files:**
- Modify: `scripts/verify_aros_principal_loop_commissioning.py`
- Test: `tests/test_aros_principal_loop_commissioning_scripts.py`

- [ ] **Step 1: Write failing tamper tests**

Build one valid live evidence fixture, then mutate and require failure for: missing/wrong Agent class; same primary/restart Agent or provider ID; missing initial attention, Task action, Eval action, semantic Write, audit, or checkpoint; changed Write content; unrelated Task/Eval commit; pending restart observation; missing evidence delta; and mediated/protected label.

- [ ] **Step 2: Verify RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py -k verifier
```

Expected: new tamper cases fail because current verifier ignores Agent provenance.

- [ ] **Step 3: Implement strict current-shape checks**

Delete old missing-cache/rebuilt handling. Extract validated tool uses and complete Write payloads:

```python
def _write_payloads(tool_uses: list[dict[str, object]]) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for item in tool_uses:
        if item.get("name") != "Write":
            continue
        value = item.get("input")
        if not isinstance(value, dict):
            raise VerificationError("semantic Write input is invalid")
        path = value.get("file_path")
        content = value.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise VerificationError("semantic Write payload is invalid")
        payloads[path] = content.encode("utf-8")
    return payloads
```

Require the exact ordered subsequence specified in Task 2. Normalize relative paths, reject escapes, and compare Claim/NOW/proposal Write bytes with `git show FINAL:PATH`. Reuse strict Task/Eval/admission hash and candidate-lineage checks. Require two explicit assimilations, fresh object IDs, restart tool calls equal one attention call, no pending refs, and latest evidence delta equal the checkpoint transition.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py
/workspace/Arbor/.venv/bin/ruff check \
  scripts/verify_aros_principal_loop_commissioning.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git diff --check
git add scripts/verify_aros_principal_loop_commissioning.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): verify live agent provenance'
```

### Task 5: Run replacement clean-wheel E2E and replace evidence

**Files:**
- Modify: `docs/analysis/aros-principal-loop-core-smoke.md`
- Modify: `docs/aros/README.md`
- Modify: `memory/NOW.md`

- [ ] **Step 1: Build and install a clean wheel without `uv`**

```bash
mkdir -p /workspace/Arbor/.worktree/commissioning/aros-live-agent-build/dist
/workspace/Arbor/.venv/bin/python -m pip wheel --no-deps \
  --wheel-dir /workspace/Arbor/.worktree/commissioning/aros-live-agent-build/dist \
  .
python3 -m venv /workspace/Arbor/.worktree/commissioning/aros-live-agent-build/venv
/workspace/Arbor/.worktree/commissioning/aros-live-agent-build/venv/bin/pip install \
  /workspace/Arbor/.worktree/commissioning/aros-live-agent-build/dist/*.whl
```

Expected: exit 0; use this venv's `aros`, never `/root/.local/bin/aros`.

- [ ] **Step 2: Run and independently verify once**

```bash
/workspace/Arbor/.worktree/commissioning/aros-live-agent-build/venv/bin/python \
  scripts/commission_aros_principal_loop.py \
  --aros /workspace/Arbor/.worktree/commissioning/aros-live-agent-build/venv/bin/aros \
  --runtime /workspace/Arbor/.worktree/commissioning/aros-live-agent-run
/workspace/Arbor/.worktree/commissioning/aros-live-agent-build/venv/bin/python \
  scripts/verify_aros_principal_loop_commissioning.py \
  /workspace/Arbor/.worktree/commissioning/aros-live-agent-run/evidence.json
```

Expected: both exit 0; verifier returns `state=verified` and `enforcement_class=cooperative`.

- [ ] **Step 3: Replace documentation and obsolete local evidence**

Rewrite the current smoke document with source/wheel hashes, Agent tool sequence, Task C/R, Eval/Run/Measurement IDs, Write-byte hashes, final commit, distinct restart identities, and exact verification output. Remove direct-driver claims and old reproduction paths. Update public guide and NOW. After the new verifier passes, remove only the three named obsolete roots:

```bash
rm -rf \
  /workspace/Arbor/.worktree/commissioning/aros-e2e-final-run-1 \
  /workspace/Arbor/.worktree/commissioning/aros-task9-final-run-1 \
  /workspace/Arbor/.worktree/commissioning/aros-task9-final-build-1
```

- [ ] **Step 4: Verify docs and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_document_registry.py tests/test_aros_public_entry.py \
  tests/test_aros_architecture_boundary.py
git diff --check
git add docs/analysis/aros-principal-loop-core-smoke.md docs/aros/README.md memory/NOW.md
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'docs(aros): commission live principal loop'
```

### Task 6: Full slice verification

**Files:**
- No planned production changes.

- [ ] **Step 1: Run focused suites**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_public_entry.py tests/test_aros_principal.py \
  tests/test_aros_research_tool.py tests/test_aros_task_tool.py \
  tests/test_aros_run_tool.py tests/test_aros_eval_tool.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
```

- [ ] **Step 2: Run full repository gates**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q
/workspace/Arbor/.venv/bin/ruff check \
  src/aros src/cli/aros_app.py src/cli/commands/aros_cmd.py \
  commissioning/principal_loop scripts/commission_aros_principal_loop.py \
  scripts/verify_aros_principal_loop_commissioning.py \
  tests/test_aros_public_entry.py tests/test_aros_principal_loop_commissioning_scripts.py
git diff --check
git status --short --branch
```

Expected: pytest and Ruff exit 0, diff check has no output, worktree is clean.

- [ ] **Step 3: Audit every design acceptance bullet**

Check Section 8 of `docs/superpowers/specs/2026-08-06-aros-live-agent-cooperative-commissioning-design.md` against retained evidence and exact Git objects. If any item lacks direct evidence, keep the slice incomplete. Do not claim protected authority, Source Gateway, Skills/MCP parity, or overall AROS completion.
