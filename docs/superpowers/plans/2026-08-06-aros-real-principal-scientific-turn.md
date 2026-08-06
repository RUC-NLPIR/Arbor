# AROS Real Principal Scientific Turn Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Commission one real `gpt-5.6-luna` Principal that preregisters a Model/Idea, runs one Task/Eval, explicitly assimilates the measurement, and recovers the scientific state in a zero-message real-model restart.

**Architecture:** Keep the current native `aros start`, Agent, Task, Run, Eval, Research, and checkpoint services unchanged. Add AROS-owned provider/model/effort defaults, then build only commissioning fixtures, a one-attempt real-provider driver, an independent mechanical verifier, and a human review packet.

**Tech Stack:** Existing OpenAI Responses provider, Arbor Agent, Typer, Git, tmux, pytest, current AROS Task/Run/Eval/Research services.

---

## File map

- Modify `src/aros/principal.py`: AROS-owned provider/model/effort constants.
- Modify `src/cli/commands/aros_cmd.py`: stop reading legacy Arbor config and add exact reasoning override.
- Modify `tests/test_aros_cli.py`, `tests/test_aros_principal.py`, and architecture tests: default/override and no-legacy-import gates.
- Create `scripts/commission_aros_real_principal.py`: one-attempt real-model driver; no provider patching or semantic writes after Principal start.
- Create `scripts/verify_aros_real_principal_commissioning.py`: mechanical lineage/provenance verifier and bounded human-review packet.
- Create `tests/test_aros_real_principal_commissioning_scripts.py`: driver/verifier boundary and tamper tests.
- Reuse `commissioning/principal_loop/task_adapter.py` and `evaluation/score.py`; do not copy them.
- Create `docs/analysis/aros-real-principal-scientific-turn.md` only after retained evidence and human review.

### Task 1: AROS-owned `gpt-5.6-luna`/max defaults

**Files:**
- Modify: `src/aros/principal.py`
- Modify: `src/cli/commands/aros_cmd.py`
- Modify: `tests/test_aros_principal.py`
- Modify: `tests/test_aros_cli.py`
- Modify: `tests/test_aros_architecture_boundary.py`

- [ ] **Step 1: Write RED default and override tests**

Add assertions:

```python
assert AROS_DEFAULT_PROVIDER == "openai-responses"
assert AROS_DEFAULT_MODEL == "gpt-5.6-luna"
assert AROS_DEFAULT_REASONING_EFFORT == "max"
```

Patch provider creation in CLI tests and invoke initialized `aros start` with
no provider/model/effort flags. Require the captured AgentConfig to contain the
three defaults. A second test passes explicit values and requires exact
overrides. Parse `aros_cmd.py` and reject `user_config`, `llm_defaults`,
`load_user_defaults`, model alias maps, or fallback model logic.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal.py tests/test_aros_cli.py \
  tests/test_aros_architecture_boundary.py -k 'default or override or legacy_config'
```

Expected: missing constants/option and current legacy config dependency failures.

- [ ] **Step 3: Implement direct defaults**

Add only three constants in `principal.py`. In `aros_cmd.start_command`, build:

```python
config_values: dict[str, object] = {
    "provider": AROS_DEFAULT_PROVIDER,
    "model": AROS_DEFAULT_MODEL,
    "reasoning_effort": AROS_DEFAULT_REASONING_EFFORT,
}
```

Apply non-None CLI provider/model/reasoning overrides. Delete the local
`llm_defaults` function and its lazy `arbor.cli.user_config` import. Do not
change global Arbor defaults, setup wizard, provider aliases, or API clients.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal.py tests/test_aros_cli.py \
  tests/test_aros_public_entry.py tests/test_aros_architecture_boundary.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/principal.py src/cli/commands/aros_cmd.py \
  tests/test_aros_principal.py tests/test_aros_cli.py \
  tests/test_aros_architecture_boundary.py
git diff --check
git add src/aros/principal.py src/cli/commands/aros_cmd.py \
  tests/test_aros_principal.py tests/test_aros_cli.py \
  tests/test_aros_architecture_boundary.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'feat(aros): default principal to gpt 5.6 luna max'
```

### Task 2: Real scientific fixture and driver boundary

**Files:**
- Create: `scripts/commission_aros_real_principal.py`
- Create: `tests/test_aros_real_principal_commissioning_scripts.py`

- [ ] **Step 1: Write RED driver boundary tests**

Parse the driver and require:

- no deterministic provider import or `create_provider` patch;
- no `Write`, `Edit`, TaskTool, EvalTool, ResearchTool, or checkpoint direct
  execution;
- no semantic `write_text`/`write_bytes` after a named `principal_started`
  boundary;
- exactly one primary and one restart `aros start` invocation;
- primary limits 40 turns and restart limits 6;
- exact model/provider/effort recorded and no fallback/retry loop;
- reuse of current Task adapter and scorer paths rather than copied code.

Require `--aros`, `--runtime`, and `--human-review` command arguments.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_real_principal_commissioning_scripts.py -k driver
```

Expected: missing driver failure.

- [ ] **Step 3: Implement pre-Principal setup**

The driver:

1. requires an absent runtime and clean-wheel interpreter;
2. creates a local repo with `candidate-mode.txt=baseline` and copies the
   existing Task adapter/scorer under `commissioning/real_principal/`;
3. commits the fixture;
4. calls permanent `initialize_knowledge_bank` with the exact Question and
   local source;
5. writes a visible evaluator manifest binding the fixture commit and commits
   it as the apparatus commit;
6. marks `principal_started = True` and never writes project files afterward.

It wraps the real `build_principal_agent` only to capture Agent/messages/tool
uses and leaves `create_provider` untouched. It invokes public `aros start`
with `--provider openai-responses --model gpt-5.6-luna
--reasoning-effort max --cooperative-human-direct --max-turns 40`.

- [ ] **Step 4: Implement the bounded human instruction**

Embed the structural requirements from Design Sections 6–8, including:

- Read Question/source/repo/evaluator.
- Full-Write CURRENT and one `ideas/I-0001-real-principal.md` with prediction.
- Preregister those two paths with empty assimilations before Task.
- One Task using the existing adapter, one Eval using the visible manifest.
- Full-Write Question, Model, Idea, Claim, NOW, and final proposal.
- Exactly two final assimilations and one measurement EvidenceLink.
- Stop after final checkpoint.

The prompt includes file contracts and tool boundaries but no scientific prose,
mechanism, rival, interpretation, or answer.

- [ ] **Step 5: Add restart capture**

Destroy the primary Agent/provider, then invoke public `aros start` again with
the same model/effort, no cooperative checkpoint flag, and max 6 turns. The
instruction permits only Attention/Read and asks for Question, Model, evidence,
uncertainty, and next possibility. Record the final external-model explanation
and require no Write/Task/Eval/Research.checkpoint calls.

- [ ] **Step 6: Verify driver tests and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_real_principal_commissioning_scripts.py -k driver
/workspace/Arbor/.venv/bin/ruff check \
  scripts/commission_aros_real_principal.py \
  tests/test_aros_real_principal_commissioning_scripts.py
git diff --check
git add scripts/commission_aros_real_principal.py \
  tests/test_aros_real_principal_commissioning_scripts.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): add real principal scientific driver'
```

### Task 3: Independent verifier and human review packet

**Files:**
- Create: `scripts/verify_aros_real_principal_commissioning.py`
- Modify: `tests/test_aros_real_principal_commissioning_scripts.py`

- [ ] **Step 1: Write RED pure/tamper tests**

Build a minimal Git fixture and evidence object. Require rejection for:

- wrong provider/model/effort or turn caps;
- missing real-provider usage/token facts;
- more/less than one Task or Eval;
- missing preregistration or prediction commit not ancestral to Task C;
- Task C != measurement candidate;
- invalid/lost/underpowered measurement;
- semantic Git blob not equal to Agent Write payload;
- placeholder/missing Model, Idea, Question, Claim, NOW sections/refs;
- missing two assimilations/EvidenceLink;
- driver semantic write after principal_started;
- non-cooperative receipt;
- restart messages reused, mutating tool calls, or missing explanation fields.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_real_principal_commissioning_scripts.py -k verifier
```

Expected: missing verifier failure.

- [ ] **Step 3: Implement mechanical verification**

Reuse existing current Task/Eval/transition validators where possible; do not
reimplement an ontology. Compare complete Agent Write strings with exact Git
blobs. Verify preregistration/final commit ancestry and tool order. Inspect
required headings/refs only; never score prose or confidence.

- [ ] **Step 4: Emit a bounded human-review Markdown packet**

The verifier writes to the explicit `--human-review` path:

```text
Question and initial source
premeasurement Model/Idea/prediction diff
Task C/R and evaluator/measurement
postmeasurement Question/Model/Idea/Claim/NOW diff
remaining uncertainty and claimed next move
restart explanation
mechanical verifier result and cooperative boundary
```

The packet contains exact refs and bounded excerpts, not raw secrets or hidden
reasoning. It ends with unchecked `accept` / `reject with reason` fields for the
human; the verifier cannot fill them.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_real_principal_commissioning_scripts.py
/workspace/Arbor/.venv/bin/ruff check \
  scripts/verify_aros_real_principal_commissioning.py \
  tests/test_aros_real_principal_commissioning_scripts.py
git diff --check
git add scripts/verify_aros_real_principal_commissioning.py \
  tests/test_aros_real_principal_commissioning_scripts.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): verify real principal scientific turn'
```

### Task 4: Credential preflight and one real-model attempt

**Files:**
- Runtime only until verifier succeeds and human review is returned.

- [ ] **Step 1: Verify credential availability without revealing a secret**

Check only whether `OPENAI_API_KEY` is non-empty. Do not print, hash, persist,
or pass it through Task environment/evidence.

- [ ] **Step 2: Build a clean wheel and normal dependency venv**

Use an absent `.worktree/commissioning/aros-real-principal-build-1`, `pip wheel
--no-deps`, ordinary venv, and dependency-resolving wheel install. Record exact
source commit and wheel SHA-256. No `uv`, editable install, `.pth`, or
PYTHONPATH.

- [ ] **Step 3: Run exactly one primary/restart attempt**

```bash
<wheel-venv>/bin/python scripts/commission_aros_real_principal.py \
  --aros <wheel-venv>/bin/aros \
  --runtime /workspace/Arbor/.worktree/commissioning/aros-real-principal-run-1 \
  --human-review /workspace/Arbor/.worktree/commissioning/aros-real-principal-run-1/human-review.md
```

Do not loop or switch model. On provider/process/scientific incompleteness,
preserve the runtime and diagnose before asking for a new explicit attempt.

- [ ] **Step 4: Independently rerun verifier**

Run the verifier in a separate process against retained evidence. It must
return `state=verified`, `model=gpt-5.6-luna`, `reasoning_effort=max`, and
`enforcement_class=cooperative` before requesting human review.

- [ ] **Step 5: Request human scientific review**

Present the generated Markdown packet and exact paths. The user must select
accept or reject with a reason. Do not mark this slice complete before that
response.

### Task 5: Recommission regressions and complete documentation

**Files:**
- Create: `docs/analysis/aros-real-principal-scientific-turn.md`
- Modify: `docs/aros/README.md`
- Modify: `docs/document_registry.json`
- Modify: `memory/NOW.md`

- [ ] **Step 1: After human acceptance, rerun existing deterministic receipts**

Independently verify the retained native-start and Principal-loop evidence.
Run focused defaults/driver/verifier/document/architecture tests and Ruff.

- [ ] **Step 2: Run full repository tests**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q
/workspace/Arbor/.venv/bin/ruff check \
  src/aros src/cli/aros_app.py src/cli/aros_start.py \
  src/cli/commands/aros_cmd.py scripts/commission_aros_real_principal.py \
  scripts/verify_aros_real_principal_commissioning.py \
  tests/test_aros_real_principal_commissioning_scripts.py
git diff --check
git status --short --branch
```

- [ ] **Step 3: Record exact evidence and promote design**

Document source/wheel/model/effort, costs/turns, preregistration, Task/Eval
lineage, semantic diffs, mechanical verifier, restart, human decision, and
cooperative limitation. Register the evidence and mark this design current.

- [ ] **Step 4: Audit Design Section 13**

Treat any missing mechanical or human evidence as incomplete. Keep the overall
AROS goal active; the next gate is one brokered durable researcher child, not
async swarm or MCP/Skills.
