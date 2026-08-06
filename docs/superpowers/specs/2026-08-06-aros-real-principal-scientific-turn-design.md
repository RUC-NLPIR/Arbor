# AROS Real Principal Scientific Turn Design

Status: proposed implementation design
Date: 2026-08-06
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`

## 1. Goal

Commission one non-scripted scientific turn using the native Arbor Principal
and the real configured OpenAI Responses provider:

```text
native local intake
-> real Principal reads Question/source/repo
-> Principal authors ScientificModel and one Idea
-> preregistration checkpoint freezes prediction and controls
-> one deterministic Task candidate C/R
-> one visible Eval and MeasurementReceipt
-> Principal interprets result
-> explicit Task + measurement assimilation
-> Git checkpoint
-> primary session destroyed
-> real fresh Principal explains canonical state from Attention
```

The model chooses the scientific prose, mechanism, uncertainty, and action
rationale. The driver supplies only the human Question, local source, fixed
apparatus, capability/budget boundary, and success criteria.

## 2. AROS model default

AROS owns one direct default independent of legacy Arbor user configuration:

```text
provider: openai-responses
model: gpt-5.6-luna
reasoning_effort: max
```

`aros start` uses this triple when no explicit CLI override is supplied. It
continues to accept explicit `--provider`, `--model`, and a new
`--reasoning-effort` value. Environment/provider-native credentials remain the
authentication source; AROS does not copy API keys into a workspace, Task,
receipt, or prompt.

There is no model alias, fallback, automatic downgrade, legacy config reader,
or retry with another model. If the endpoint rejects `gpt-5.6-luna` or `max`,
the attempt ends as a provider/transport failure and no scientific result is
claimed.

## 3. Why this gate comes next

Three options were considered:

1. **Real Principal first — selected.** Current native intake, Attention,
   Task/Run/Eval, semantic file tools, checkpoint, and restart already exist.
   This directly tests whether they increase a real Agent's research ability.
2. **Child Agent first — rejected for this slice.** A durable child model needs
   a credential/capability broker and provider child profile. Building those
   before proving one Principal scientific turn inverts complexity.
3. **Skills/MCP first — rejected for this slice.** Skills are procedure and MCP
   is transport. Neither can establish scientific interpretation, measurement
   separation, or assimilation.

## 4. Scope

### 4.1 Included

- Real OpenAI Responses calls using `gpt-5.6-luna`, effort `max`.
- One new local Git KB initialized through native `aros start` mechanics.
- One local Markdown source and one small deterministic code/evaluator fixture.
- One human-authored Question.
- Principal-authored Question update, CURRENT model, one Idea, one Claim, and
  NOW update.
- One pre-measurement semantic checkpoint freezing Model/Idea prediction and
  controls with no observation assimilation.
- One existing deterministic Task adapter operating in an isolated worktree.
- One visible evaluator registered and run through current Eval/Run services.
- Exact Task C == MeasurementReceipt candidate lineage.
- One explicit Task assimilation and one explicit measurement assimilation.
- Cooperative human-direct admission, clearly labelled.
- Destruction of the primary Agent/provider and a real external-model restart.
- Independent mechanical verifier plus separate human scientific review.

### 4.2 Excluded

- Child LLM Agent or provider child adapter.
- Async Idea generation or Idea queue.
- Peer Agent review and automatic Question resolution.
- Remote source acquisition or Source Gateway.
- Project Skills runtime or MCP.
- Protected Eval, Principal lease/broker/budget enforcement, or non-bypass
  authority.
- Automatic scientific scoring, belief updates, stop/pivot, or retry.
- New production workflow, scheduler, ontology, or database.

## 5. Fixed research apparatus

The commissioning workspace is assembled before the Principal starts:

1. create and commit `candidate-mode.txt`, Task adapter, and evaluator code
   outside AROS-owned semantic roots;
2. initialize the Question/source KnowledgeBank through the permanent native
   intake service;
3. add and commit the visible manifest under `eval/suites/` as the fixed
   apparatus commit;
4. start the real Principal on the clean initialized workspace.

The resulting pre-Principal files are:

```text
candidate-mode.txt                baseline
commissioning/real_principal/task_adapter.py
commissioning/real_principal/evaluation/score.py
eval/suites/real-principal/1/manifest.json
```

The local source says that changing `candidate-mode.txt` from `baseline` to
`success` is predicted to activate a deterministic mediator. It is source
material, not a Claim or measurement.

The Task adapter may change only `candidate-mode.txt`, create candidate commit
C, create a strict Task return, and create return commit R. The evaluator binds
the exact candidate/apparatus commits, fixed seed, environment, parser, resource
limit, and output receipts. It emits one factual scalar:

```text
real_principal_quality = 1.0
measurement_state = valid
```

The worker cannot set the metric through prose. The Principal cannot modify the
apparatus after launch and cannot fill the MeasurementReceipt.

## 6. Human instruction and Agent freedom

The driver sends one bounded human instruction that states:

- inspect canonical Attention, Q-0001, local source, repo, and evaluator;
- form an explicit model/rival and one information-seeking Idea;
- use exactly one Task and one Eval;
- inspect Task return, apparatus, raw output, and receipt;
- update only semantic files whose meaning changed;
- preserve uncertainty and state what the result cannot establish;
- author an explicit two-observation assimilation proposal;
- audit/checkpoint only after checking exact lineage;
- stop after one coherent admitted transition.

It does not provide the Model, Idea, Claim statement, evidence interpretation,
or final scientific answer. The Principal may choose headings/prose within the
current readable contracts, may revise its initial mechanism after measurement,
and may conclude that the result only bounds rather than resolves Q-0001.

The Principal must not open a second Idea, Task, Eval, or child. This is a
budget boundary, not a scientific stage machine.

## 7. Required Principal-authored state

### 7.1 Question

`questions/Q-0001/question.md` retains the exact human Question and replaces
the relevant `Not yet assessed` sections with scoped current answer,
uncertainty, evidence-changing conditions, resolution criterion, and stop/pivot
criterion. The host never decides whether the Question is resolved.

### 7.2 ScientificModel

`model/CURRENT.md` must contain non-placeholder sections for:

- scope/boundary;
- proposed mechanism;
- at least one rival or null;
- prediction before measurement;
- observed result and apparatus limits;
- remaining uncertainty;
- exact source/Task/Eval/Claim refs.

### 7.3 Idea

One `ideas/I-0001-*.md` must identify Q-0001, the intended discriminator,
expected observations under the focal/rival models, minimal controls/evaluator,
cost/capability, what failure would teach, and Task/Eval links. It remains a
map of the chosen action, not an OS queue.

### 7.4 Claim and NOW

One Claim contains a strict measurement EvidenceLink with Principal-selected
relation/scope plus counterevidence, assumptions, and uncertainty. NOW compresses
the changed thesis, strongest limitation/counterevidence, current obligations,
and next possibilities without becoming a transcript.

Every final semantic byte must originate in a real Agent `Write` tool call after
the Agent has read the existing file where one exists. `Edit` is not accepted
in this commissioning trace, avoiding a second diff replay implementation.
The commissioning driver cannot write these files after the Principal begins.

## 8. Execution and assimilation

The real Principal uses the current sealed tools:

```text
Research.attention
Read/Grep/Glob
Task.create/start/status/collect
Eval.register/run/status/observe/audit
Write
Research.transition_audit/checkpoint
```

Run may appear only as Eval's owned execution lineage; no duplicate standalone
experiment is required.

Before Task creation, the Principal reads the source, repo, and evaluator, then
writes CURRENT and I-0001 with a discriminating prediction and controls. It
authors a four-field preregistration proposal with those semantic paths and an
empty assimilation list, audits it, and checkpoints it. Task creation therefore
starts from a clean committed base, and the later measurement can be compared
against prediction bytes that already existed in Git.

The final proposal has exactly two assimilations:

1. Task collection → Idea/NOW and any Model/Question paths genuinely changed by
   the returned candidate.
2. MeasurementReceipt → Claim plus any Model/Question/Idea/NOW paths genuinely
   changed by the measurement.

Source prose is cited by path/hash in Model/Idea but is not silently converted
into a service observation or EvidenceLink before Source Gateway exists.

TransitionAudit verifies syntax, changed paths, rationale anchors, Task/Eval
lineage, EvidenceLink structure, and C equality. It does not judge the model,
idea, claim, or answer. Cooperative checkpoint uses the existing gateway and
Git CAS.

## 9. Budget and authority

The primary process is bounded to:

```text
model: gpt-5.6-luna
reasoning_effort: max
max_turns: 40
Task count: 1
Eval count: 1
Bash tool: absent
Task network: false
Task timeout: 120 seconds
Eval timeout: 120 seconds
```

The restart process uses the same model/effort, at most 6 turns, and no Bash.
It may inspect Attention/files and explain current state but may not create a
Task/Eval or checkpoint.

Existing provider-native bounded retries may repeat a failed transport call.
They do not repeat Task, Eval, measurement, or scientific action. Any second
scientific attempt uses a new runtime root and explicit human action.

Authority remains `cooperative`. Same-UID writable Git is not described as
protected, mediated, or non-bypass.

## 10. Failure semantics

The run fails incomplete when:

- credentials/model/effort are rejected;
- the Agent stops without the required tool-backed state;
- more than one Task/Eval is attempted;
- Task is lost/failed/timed out;
- Eval is lost, invalid, underpowered, or does not measure C;
- Agent prose attempts to manufacture a metric;
- semantic bytes cannot be matched to Agent tool calls;
- proposal/audit/checkpoint lineage is invalid;
- restart reuses prior messages or cannot explain canonical state.

A valid scientific negative is not a commissioning failure if the apparatus is
valid and the Principal records the scoped negative correctly. This particular
fixture predicts a positive deterministic measurement, so another scalar is an
apparatus/fixture failure, not evidence against the scientific hypothesis.

No failure triggers automatic model fallback or a second scientific run.

## 11. Verification and human review

The independent verifier checks only mechanically decidable facts:

- exact provider/model/effort and turn caps recorded;
- native Agent class and real provider call usage;
- ordered Attention/source/repo/Task/Eval/semantic/audit/checkpoint tool trace;
- preregistration commit is an ancestor of Task C and contains the prediction
  before Task/Eval launch;
- every final semantic blob equals one complete Agent Write output;
- Task C/R and Eval/Run/Measurement immutable lineage;
- exactly one Task, one Eval, two assimilations, and one strict EvidenceLink;
- Question/Model/Idea/Claim/NOW are non-placeholder and contain required refs;
- no hidden driver semantic writes;
- cooperative receipt and final Git commit;
- destroyed primary session and zero-message real-model restart;
- restart explains Question, current model, evidence, uncertainty, and next
  possibility without changing canonical state.

The verifier does not score scientific prose. A human reads the exact diff,
source, apparatus, raw measurement, and restart explanation and records whether
the Model/Idea/Claim are coherent, appropriately scoped, and useful. Human
review does not retroactively change the mechanical receipt.

## 12. Production changes

Only the following product change is planned:

- define AROS-owned default provider/model/effort;
- add `--reasoning-effort` to direct `aros start`;
- stop reading legacy Arbor user config for AROS defaults;
- record effective provider/model/effort in commissioning evidence.

All other work is fixture, driver, verifier, and evidence documentation. No
Task/Run/Eval/Research/checkpoint API is changed.

## 13. Acceptance

This slice is complete only when:

1. Tests prove AROS defaults to `openai-responses`, `gpt-5.6-luna`, effort
   `max`, with exact CLI overrides and no legacy config dependency.
2. Current credential preflight succeeds without printing or persisting a
   secret.
3. One real-model primary run stays within 40 turns and performs exactly one
   Task and Eval plus exactly one preregistration and one final checkpoint.
4. Independent verifier accepts exact semantic/tool/Git/measurement lineage.
5. One real-model restart stays within 6 turns, starts with zero messages, and
   explains canonical scientific state without mutation.
6. Human review accepts or explicitly rejects scientific coherence; rejection
   keeps the slice incomplete but preserves the evidence.
7. Existing deterministic native-start and Principal-loop commissioning still
   verify.
8. Focused/full tests, Ruff, architecture/document gates, and diff/status pass.

This acceptance does not complete child Agents, async Idea research, peer
review, Source Gateway, Skills/MCP, protected authority, or overall AROS.

## 14. Next gate

After a successful real Principal turn, implement one durable researcher child
profile through an Arbor-native credential/capability broker. Only after one
child return is assimilated without shared-state corruption should async Idea
generation be designed.
