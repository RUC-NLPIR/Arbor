# AROS Live Agent Cooperative Commissioning Design

Status: proposed implementation design
Date: 2026-08-06
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`

## 1. Goal

Prove that the existing Arbor `Agent.run` loop, rather than a commissioning
driver that calls AROS services directly, can complete one real cooperative
AROS research transition:

```text
fresh Attention
-> Task create/start/status/collect
-> Eval backed by durable Run
-> valid MeasurementReceipt
-> Principal-authored Claim/NOW/proposal bytes through file tools
-> Research audit/checkpoint
-> cooperative Git CAS
-> destroyed Agent and provider
-> fresh Agent recovers from canonical Attention
```

This closes the missing live-Agent path over the commissioned mechanical
kernel. The cooperative authority class is the permanent explicit local mode
for a trusted same-UID owner; it is not a temporary implementation that a later
mode replaces. It is not evidence for protected or non-bypass authority.

## 2. Scope boundary

This slice includes:

1. A deterministic commissioning-only provider that drives the real Arbor
   Agent loop from tool results.
2. Host injection of the existing cooperative checkpoint gateway and an
   explicitly cooperative Attention authority context.
3. Real Principal calls to Research, Task, Eval, and file tools.
4. Exact evidence that the admitted semantic bytes came from Agent file-tool
   arguments rather than direct driver writes.
5. Destruction of the first Agent/provider and recovery by a fresh Agent from
   canonical workspace state without transcript replay.
6. Replacement of the obsolete direct-driver walkthrough with one live-Agent
   commissioning command and one independent verifier.

This slice does not include:

- a real external model quality claim;
- protected evaluation or a hidden evaluator;
- an Arbor-native broker, Principal lease, budget enforcement, or non-bypass
  authority;
- Source Gateway, MCP parity, project Skills lifecycle, provider parity, K/M/G,
  or Arbor retirement;
- a semantic Coordinator, research workflow state machine, automatic
  assimilation, or automatic scientific interpretation.

The evidence must use `enforcement_class=cooperative`. Any mediated or
protected label is a commissioning failure.

## 3. Chosen approach

Use a deterministic state-machine provider as the Agent's provider. The
provider implements the existing `LLMProvider` protocol and emits ordinary
`LLMResponse` tool-use blocks. It receives only the system prompt and Agent
message history. It has no workspace path, file, Git, process, Task, Eval, or
checkpoint dependency and must not import filesystem or subprocess helpers.

This approach is preferred over two alternatives:

- A real external model is nondeterministic, costs external tokens, and cannot
  be a reproducible first commissioning gate.
- A driver that invokes tool classes directly is the current mechanical proof
  and does not exercise `Agent.run`.

The deterministic provider is an ordinary protocol test double. It proves
Agent-loop composition, tool-result feedback, and restart continuity without
adding a product abstraction. It does not prove scientific taste or model
quality. A real model uses the exact same public host, Agent, tool, gateway, and
workspace path; no commissioning-only runtime is swapped into production.

No dependency is added. The implementation uses the repository's maintained
Agent/LLMProvider contracts, Typer CLI, pytest, Git, and tmux support. It does
not introduce a new workflow engine, RPC layer, configuration layer, or custom
test framework.

## 4. Components

### 4.1 Deterministic Principal provider

The provider is a commissioning fixture, not a production provider choice. It
emits one ordered tool call per Agent turn and advances only after parsing the
corresponding tool result from the messages supplied by `Agent.run`.

It performs these actions:

1. `Research.attention` and validates that the initial Task/Eval observations
   are absent.
2. `Task.create`, `Task.start`, bounded `Task.status`, and `Task.collect`.
3. `Eval.run` for the exact Task candidate commit.
4. `Research.attention` to re-observe the current canonical HEAD and the two
   unassimilated observations.
5. `Write` complete Claim, NOW, and four-field proposal files.
6. `Research.transition_audit` and requires mechanical validity.
7. `Research.checkpoint` and requires a cooperative admitted commit.
8. Returns a final text response only after the checkpoint result is observed.

The provider may retain transient state during this one Agent process, just as
a model retains current context. The restart uses a new provider instance with
no copied state.

### 4.2 Cooperative Principal host wiring

The host constructs the existing native Principal with:

- the canonical repository/ref binding;
- the existing `HumanDirectGateway` selected by an explicit host/user option;
- an `AttentionAuthorityContext` labelled `cooperative` with budget state
  `not_configured`;
- the existing Research, Task, Run, Eval, Read/Grep/Glob/Edit/Write tools;
- no Bash requirement for the commissioning scenario.

The gateway selection is host-owned and never appears in model tool arguments
or the Research schema. `aros start` gains an explicit cooperative option so a
human can run the same usable path; the default remains without checkpoint
authority and fails closed. This is a supported local mode with an honest
authority label, not a compatibility fallback.

### 4.3 Commissioning driver

The driver creates a new Git workspace, copies the existing deterministic Task
adapter and evaluator apparatus, registers the evaluator, and commits only the
initial fixture. After the Agent starts, the driver does not write Claim, NOW,
proposal, audit, admission, Task, Run, or Eval records.

The driver records:

- source and fixture commits;
- each normalized `agent.tool_uses` entry in order;
- the exact first-Agent terminal result and stop reason;
- Task C/R, Eval/Run/Measurement identities, and final canonical commit;
- hashes of semantic `Write` payloads and their admitted Git blobs;
- first and restarted Attention packets;
- distinct first/restart Agent and provider instance identifiers;
- the explicit cooperative enforcement class.

The live-Agent driver replaces the existing direct-driver commissioning in
place. Obsolete direct tool orchestration, its current-evidence claim, and old
retained commissioning output are removed once the new run verifies. The
verifier accepts only the current live-Agent evidence shape; it contains no
legacy branch, schema fallback, migration reader, or compatibility mode.

The working Task adapter, evaluator apparatus, strict validators, and kernel
services are preserved because the new Agent path exercises them directly.
Only the obsolete walkthrough orchestration is deleted.

### 4.4 Independent verifier

The verifier reads only the evidence file and exact referenced repository
objects. It does not trust driver success prose. It reuses current strict AROS
validators for Task, Run, Eval, transition, receipt, and Git lineage.

It reconstructs every admitted semantic file from the matching Agent `Write`
tool argument and requires byte equality with the final Git blob. The proposal,
audit, admission, Claim, and NOW must be in the same final commit. It also
requires distinct Agent/provider instances at restart and no first-session
messages in the restart record.

## 5. Data flow

The setup phase creates only baseline and apparatus state. The first Agent then
owns every scientific action:

```text
Agent.run
  -> provider emits Task.create
  -> Agent executes Task tool
  -> operational checkpoint admits Task record
  -> tool result returns to provider
  -> provider chooses the next action
  -> ...
  -> reality returns Task C/R and MeasurementReceipt
  -> provider authors semantic bytes through Write
  -> Research audits and checkpoints the exact bytes
```

After Git CAS, the driver discards the first Agent, provider, and transcript.
A new Agent receives a new boot packet, calls `Research.attention`, and returns
an explanation derived from canonical Claim/NOW/evidence pointers. The
restarted packet must contain the admitted evidence delta and no pending Task
or measurement from the completed transition.

The deterministic provider never becomes canonical state. Only the versioned
workspace and immutable operational observations survive restart.

## 6. Failure semantics

The commissioning fails closed when:

- the provider emits an unexpected tool, field, order, or extra call;
- a tool result is malformed, reports an error, or refers to unrelated Task and
  Eval candidate commits;
- Task or Eval polling exceeds a fixed attempt count;
- a process is lost, the apparatus is invalid, or the measurement is not
  `valid` with the fixture's exact metric;
- the operational or scientific checkpoint lacks injected authority;
- an audit is mechanically invalid or Git CAS is stale;
- semantic bytes do not match Agent tool payloads;
- the driver writes semantic or service-owned records after Agent start;
- restart reuses the prior Agent/provider/messages;
- the evidence claims stronger than cooperative enforcement.

No failed Task, Run, Eval, audit, or checkpoint is automatically repeated. A
commissioning rerun uses a new absent runtime root and new idempotency keys.

## 7. Verification strategy

Implementation follows strict RED -> GREEN:

1. Provider unit tests reject missing, malformed, reordered, unrelated, and
   failed tool results.
2. Principal integration tests prove the real `Agent.run` loop executes the
   exact tool sequence and feeds each result back to the provider.
3. CLI tests prove cooperative gateway/context injection is explicit,
   correctly labelled, and absent by default without changing model schemas.
4. Verifier tests reject driver-authored semantic bytes, mismatched blobs,
   synthetic tool traces, transcript reuse, same provider instance, unrelated
   Task/Eval lineage, pending observations, or stronger enforcement labels.
5. The replacement one-command clean-wheel commissioning produces a retained
   evidence file that the independent verifier accepts.
6. Focused AROS tests, architecture/document gates, Ruff, diff-check, and the
   full repository suite run after the live evidence is accepted.

## 8. Acceptance gate

The slice is complete only when one retained clean-wheel run proves all of the
following:

- the first process used a real Arbor `Agent` and `Agent.run`;
- Research attention preceded action selection;
- Task create/start/status/collect and Eval run were Agent tool calls;
- Task collection child commit equals MeasurementReceipt candidate commit;
- the Agent observed both returns before authoring meaning;
- Claim, NOW, and proposal bytes exactly equal Agent `Write` payloads;
- audit and checkpoint were Agent Research calls;
- the final commit contains one explicit Task assimilation and one explicit
  measurement assimilation/EvidenceLink;
- the first Agent/provider/transcript were destroyed;
- the fresh Agent recovered the evidence delta from canonical Attention and
  both observations disappeared from `unassimilated_returns`;
- the verifier returns `state=verified` and
  `enforcement_class=cooperative`;
- the obsolete direct-driver code and evidence are absent;
- no claim is made for protected authority, external-model scientific quality,
  Source Gateway, Skills/MCP parity, or full Design Book commissioning.

## 9. Next gate

After this slice, the existing admission-gateway boundary gains an Arbor-native
broker/lease/fence implementation for a protected deployment mode. The Agent,
Research/Task/Run/Eval tools, semantic workspace, proposal, audit, checkpoint,
and verifier invariants do not change. The protected mode repeats the same
scenario with a Principal that cannot write the canonical Git control plane or
invoke the human-direct route. Only that evidence may claim mediated,
non-bypass authority.
