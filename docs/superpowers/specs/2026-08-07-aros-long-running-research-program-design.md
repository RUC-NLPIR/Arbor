# AROS Long-Running Experimental Research Program Design

Status: approved architecture design; not an implementation claim
Date: 2026-08-07
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`

## 1. Decision

AROS will become a long-running experimental researcher by composing durable,
event-driven scientific subjects around the existing small kernel. Capability
does not come from adding a semantic workflow engine. It comes from:

1. one Principal that remains scientifically responsible across fresh sessions;
2. multiple bounded Researcher agents that own free inner research loops;
3. independent Reviewers that try to reproduce and refute material Claims;
4. source, experiment, and evaluation interfaces that expose external reality;
5. mechanical supervision for events, budgets, processes, approvals, and
   receipts.

The target workload is experimental open research over code repositories,
public or private data, reproducible commands, and CPU or GPU execution. A
positive metric is not required. Success is a reviewable research result package
containing new understanding, evidence, counterevidence, reproducible artifacts,
limitations, and remaining uncertainty.

## 2. Scope and boundaries

### 2.1 Required scope

- One Git repository and one canonical research workspace.
- One active Principal at a time, reconstructed from canonical state rather
  than transcript replay.
- Several asynchronous Researcher or Reviewer tasks in isolated worktrees.
- Public network reads and authenticated reads from approved private sources.
- Reproducible CPU/GPU experiments through durable Run and independent Eval.
- A human-approved mission budget with autonomous allocation inside that
  boundary.
- Event-driven Principal sessions plus a periodic watchdog so the mission cannot
  sleep forever.
- Exploratory and confirmatory research tracks.
- Independent review before a Claim becomes a formal reported result.
- Preliminary and formal result notifications.

### 2.2 External side-effect boundary

Research reads may use public internet, private datasets, authenticated APIs,
and authenticated Git. The following always require explicit human approval:

- publishing or sending a result outside the research workspace;
- uploading data or artifacts;
- buying or expanding compute;
- modifying a remote repository, service, dataset, or account;
- any other externally visible or irreversible write.

Approval for one exact action does not authorize a broader class of actions.

### 2.3 Non-goals

- A universal scientific state machine, Idea scheduler, or belief reducer.
- A kernel score for scientific truth, fundamentality, or novelty.
- Persistent LLM conversations as project memory.
- Unlimited autonomous spending or credential exposure to model context.
- Automatic publication or automatic promotion of a scientific Claim.
- Recreating the Arbor Coordinator, fixed cycle, or hypothesis tree under new
  names.
- Requiring a positive result for mission success.

## 3. Architecture and authority

```text
Human
  <-> approvals, budget expansion, blockers, result notifications
Mission Supervisor
  <-> events, watchdog, budget accounting, process state, wakeups
Principal
  +-> Researcher A / B / C
  +-> Independent Reviewer
  +-> Source / Run / Eval
        <-> external evidence and experiments
Versioned Research Workspace
```

### 3.1 Principal

The Principal is the only scientific authority for canonical project meaning.
It owns:

- the mission interpretation and current Key Research Question;
- the mechanism model and competing explanations;
- the map of important, actionable uncertainties;
- the research portfolio and allocation among directions;
- decisions to start, renew, stop, narrow, or redirect work;
- interpretation of evidence, counterevidence, and Reviewer criticism;
- canonical Question, Model, Idea, Claim, NOW, and result-package edits;
- requests for human approval and mission-budget expansion.

The Principal may refine, split, or redirect questions inside the human mission,
but must checkpoint the reason and evidence. It may not redefine the mission.

Each Principal invocation is a fresh, bounded session. The provider transcript
is never canonical research memory.

### 3.2 Researcher

A Researcher receives one bounded direction and owns its scientific inner loop:

```text
understand -> search/read -> hypothesize -> implement or probe
-> experiment -> inspect anomalies -> iterate -> validate
-> analyze limitations -> commit -> return exact refs
```

The Researcher may change method within its brief. It may not silently change
the mission, Question, evaluator, comparison, capability boundary, or budget.
It returns child commits, reports, logs, source references, and receipts. It
cannot directly modify canonical scientific meaning.

### 3.3 Independent Reviewer

A Reviewer is a fresh agent session in an independent worktree. It receives the
Claim draft, preregistration, raw evidence, exact commits, environment, and the
Researcher report, but never the Researcher or Principal transcript. It must:

- reproduce the central result when technically possible;
- search for data leakage, metric drift, selection bias, and low power;
- construct alternative mechanisms and counterexamples;
- inspect contradictory literature and priority claims;
- determine whether the experiment distinguishes the stated rivals;
- return critique, reproduction evidence, and requested corrections.

The Reviewer advises; it cannot admit or reject canonical scientific meaning.
The Principal must respond to material objections before formal reporting.

### 3.4 Mission Supervisor

The Mission Supervisor is a mechanical control plane. It owns:

- a durable event inbox/outbox and idempotency keys;
- the single-Principal lease;
- event coalescing, wakeups, and watchdog deadlines;
- mission and per-task budget accounting;
- process observation and restart requests;
- exact human approval requests and responses;
- notification delivery state.

It never selects an Idea, interprets a measurement, renews a Researcher for
scientific reasons, edits a Claim, or decides that an uncertainty is resolved.

### 3.5 Reality interfaces

- `Source` retrieves public or authenticated research material with provenance,
  content hashes, access class, and license/usage metadata where available.
- `Run` executes durable commands under declared CPU/GPU, time, storage, and
  network budgets.
- `Eval` binds the exact candidate, apparatus, inputs, seed, environment,
  parser, outputs, and measurement receipt.
- Git/worktree services bind canonical and child artifacts without allowing a
  child to rewrite the Principal's project meaning.

### 3.6 Human Gateway

The Human Gateway is used only for:

- mission ambiguity that materially changes the research program;
- capability or resource-boundary conflicts;
- blockers that safe autonomous work cannot remove;
- expansion of the mission total budget;
- approval of external writes;
- preliminary discoveries and formal result packages.

Routine progress does not require human review.

## 4. Canonical and operational state

Git remains the only canonical scientific memory. Canonical files are ordinary,
human-readable Markdown and versioned artifacts. They include the mission,
questions, mechanism model and rivals, portfolio, preregistrations, Claims,
counterevidence, results, and current NOW.

Operational runtime state contains events, leases, budgets, process handles,
heartbeats, approval delivery, and receipts. It may be rebuilt or reconciled
from versioned records and external observations. It is not scientific truth.

The model does not write event acknowledgements, lease fields, budget ledger
entries, or receipt hashes. The host records exact returned observations. A
scientific terminal observation remains unread until a Principal checkpoint
records that the session handled it. Ephemeral watchdog/liveness ticks may be
coalesced and acknowledged mechanically because they contain no scientific
result.

## 5. Event-driven long-running operation

### 5.1 Wake events

The Supervisor wakes the Principal when any of these occurs:

- a Researcher heartbeat, completion, failure, or budget threshold;
- a Run or Eval completion, timeout, loss, or anomalous measurement;
- new source material or an authenticated-source change;
- mission budget thresholds;
- a human approval or instruction;
- a periodic watchdog deadline.

Events are immutable, attributed, idempotent, and refer to exact records. The
Supervisor coalesces redundant wakeups but never drops an unread terminal
observation.

### 5.2 Principal episode

```text
event or watchdog
-> coalesce and acquire the single-Principal lease
-> build bounded Attention
-> start a fresh Principal session
-> interpret evidence and choose actions
-> checkpoint / start-stop-renew tasks / request approval / report
-> declare the next wake conditions and deadline
-> release the lease and exit
```

Attention includes the mission, current mechanism model, important
uncertainties, active portfolio, unread returns, budget, blockers, obligations,
and recent canonical changes. It contains pointers rather than unbounded source
or transcript content.

Every Principal episode must leave a next-wake deadline. The Supervisor rejects
an indefinitely sleeping mission. The watchdog cadence is adaptive: active or
near-terminal work is checked more frequently; an idle mission is checked less
frequently. A watchdog wake that finds no decision-relevant change should exit
without inventing work.

### 5.3 Crash behavior

- Principal death releases or expires the lease and leaves canonical state at
  the last completed checkpoint.
- Researcher and Run processes continue independently when their carrier is
  still observable.
- Missing or ambiguous process truth becomes `lost`; it never becomes success.
- Unhandled terminal observations reappear on the next Attention packet.
- Recovery may repair derived operational pointers, never scientific meaning.

## 6. Scientific portfolio and first-principles selection

The Principal maintains a user-space research portfolio. Each candidate
direction records:

- the phenomenon and mission relevance;
- primitive assumptions, constraints, invariants, and proposed mechanism;
- competing explanations and discriminating predictions;
- falsification conditions;
- current literature position and novelty risk;
- impact if true;
- expected information gain, cost, dependencies, and blockers;
- current evidence and counterevidence;
- the reason to explore, pause, stop, or confirm.

The OS does not parse these fields or choose among directions.

### 6.1 Lexicographic gate

A direction must pass these priorities in order:

1. **Mechanism compression**: it proposes a deeper mechanism that can unify
   multiple observations and generate new predictions.
2. **Literature novelty**: it is not already established, refuted, or made
   redundant by existing work.
3. **Impact scale**: if correct, it materially changes the model, method, or
   research direction.
4. **Falsifiability and decision relevance**: an attainable observation can
   distinguish rivals and change the next action.
5. **Expected information gain per cost**: it reduces an important actionable
   uncertainty efficiently.

Claim throughput and probability of a positive result are secondary objectives.
Concurrency increases coverage only after directions pass the gate. It does
not make budget opportunity cost disappear.

### 6.2 Dynamic portfolio

The Principal chooses the number and diversity of active Researcher directions.
The kernel enforces total budget, per-task budgets, maximum concurrency, and
resource isolation only. There is no kernel Idea queue or automatic replacement
of an idle slot.

The Principal should avoid duplicate experiments, preserve high-risk mechanism
directions when their expected information gain justifies cost, and terminate
incremental directions that no longer affect an important uncertainty.

## 7. Exploration and confirmation

Research uses two explicitly labeled tracks.

### 7.1 Exploratory track

Researchers may search, prototype, inspect data, change code, run cheap probes,
and revise local hypotheses. Exploratory output may guide the portfolio but may
not justify a formal Claim. It must preserve commands, artifacts, and negative
results sufficient to understand how the direction evolved.

### 7.2 Confirmatory track

Before an important Claim is tested, the Principal checkpoints a
preregistration containing:

- the mechanism hypothesis and competing explanations;
- primary predictions and explicit falsifiers;
- candidate/input population, controls, exclusions, and contamination risks;
- exact evaluator or apparatus and primary measurement;
- analysis method and interpretation boundaries;
- stopping or rerun conditions.

The confirmatory experiment binds the preregistration, exact candidate, data,
apparatus, environment, and output receipt. A negative or underpowered result is
preserved as such. The Principal may design a new experiment, but may not relabel
the original one or silently switch its primary metric.

## 8. Researcher contract and renewal

Each TaskBrief contains the smallest mechanical boundary needed for safe,
independent work:

- objective and why it matters to the portfolio;
- relevant Question, Model, source, evidence, and counterevidence refs;
- base commit, isolated worktree, read/write paths, and required deliverables;
- evaluator, controls, and comparison conditions that may not change silently;
- network, shell, credential-handle, CPU/GPU, storage, token, and wall-time
  capabilities;
- initial budget, heartbeat schedule, stop conditions, and return requirements;
- the external-write approval boundary.

The TaskBrief does not prescribe a universal scientific phase sequence.

### 8.1 Heartbeat

A Researcher heartbeat contains:

- new observations and exact artifact/receipt refs;
- completed and active commands;
- budget consumed and remaining;
- blockers and deviations;
- the next proposed action and why it remains valuable.

Process liveness alone is not evidence of research progress.

### 8.2 Evidence-based renewal

- Human approves a hard mission budget and capability envelope.
- Principal may allocate, reclaim, or renew work inside the remaining mission
  budget.
- Researcher may request renewal but cannot enlarge its own budget.
- A renewal request must state new evidence, remaining uncertainty, next action,
  and expected information gain.
- Work without evidence of progress is not renewed by default; all partial and
  negative artifacts remain preserved.
- Exceeding the mission budget requires a human request containing achieved
  results, unresolved uncertainty, the exact increment, and its expected value.

Research milestones, not a fixed wall-clock duration, define mission progress.
Runtime still enforces finite resource limits for every process and allocation.

## 9. Source and credential boundary

Source access supports papers, documentation, repositories, datasets, and APIs.
Every returned material has a stable source reference and provenance. Public
bytes may be captured when licensing and size permit. Private or restricted data
may remain outside Git; the workspace stores a content hash or version, access
class, retrieval method, and authorized pointer.

Credentials are supplied through host-owned capability handles. Raw secrets are
never placed in prompts, environment dumps, logs, reports, commits, or receipts.
Source tools expose read operations by default. Any request that could modify a
remote system becomes an exact approval request and remains blocked until the
Human Gateway authorizes that action.

## 10. Review and result packages

### 10.1 Formal review

Every formal Claim requires an independent Reviewer return and a Principal
response. A structural verifier may confirm that the result package points to a
preregistration, evidence receipts, reproduction artifacts, Reviewer report,
and Principal response. It cannot judge whether the Claim is true.

### 10.2 Preliminary notice

A materially novel or high-impact observation may trigger an immediate human
notice before review. It must say that it is preliminary, name the exact
evidence, state the main alternative explanation, and identify the next
confirmation or review step.

### 10.3 Formal result package

A formal package contains:

- the scoped Claim and mechanism explanation;
- mission and Question relevance;
- preregistration and exact evidence receipts;
- reproduction commands and environment;
- counterevidence and alternative explanations;
- Reviewer reproduction and critique;
- Principal response and any narrowed wording;
- limitations, external-validity boundary, and remaining uncertainty;
- the canonical checkpoint containing the result.

A negative result may be formal when it materially eliminates a mechanism,
reveals a boundary condition, or redirects the research program.

## 11. Procedural learning

Scientific conclusions remain in the Knowledge Bank. Reusable research methods
follow a separate promotion path:

```text
one useful method
-> candidate procedure with failure notes
-> repeated success on distinct tasks
-> independent Reviewer checks scope and failure modes
-> Principal and, where policy requires, Human approval
-> project Skill
```

A single successful run cannot automatically create an active Skill. A Skill
contains procedure, applicability tests, inputs, outputs, and known failures; it
does not promote an unreviewed scientific conclusion.

## 12. Failure semantics

The system preserves distinctions that change scientific action:

- source or network failure;
- provider or transport failure;
- process crash, timeout, or lost identity;
- budget exhaustion;
- invalid evaluator or apparatus;
- underpowered or contaminated measurement;
- scientific negative result;
- contradictory evidence;
- mission, permission, or external-write blocker.

Only bounded transport retry may repeat the same external call automatically.
Repeating a Researcher, experiment, Eval, or review requires a new Principal
action and a new idempotency key. No missing receipt, unknown process state, or
agent prose can be converted into success.

## 13. Notifications and human interaction

The Principal interrupts the human only when:

- mission ambiguity changes the research program;
- a capability or resource boundary conflicts with the next valuable action;
- a blocker cannot be safely removed autonomously;
- a mission-budget expansion or external write needs approval;
- a preliminary discovery or formal result is ready.

Approval and notification records are operational. Scientific content remains
in the canonical result package. Notification failure is retried mechanically
without rerunning research or duplicating the canonical result.

## 14. Delivery decomposition

This architecture is intentionally split into independent subprojects. It must
not be implemented as one cross-cutting rewrite.

The current `src/aros` tree already consumes the simple-kernel first-slice limit
of 19,000 lines. Mission supervision must not create a temporary complexity
spike. Task's duplicate process carrier is therefore removed before any new
Supervisor module is added.

### Phase 0A: Task-on-Run carrier removal (complete)

- preserve Task identity, immutable brief, isolated worktree, collection,
  preservation, pruning, and return lineage;
- replace the duplicate Task process carrier with the existing durable Run
  substrate;
- retain exact terminal, timeout, stop, lost, dirty-work, and descendant truth;
- delete superseded Task runner/process code and meet the human-approved interim
  recursive `src/aros <= 17,700 LOC` gate;
- recommission current Task, Run, Eval, and simple-loop behavior before adding
  new long-running control code.

Exit met: one Task executes through Run with unchanged externally meaningful
lineage and failure truth, the old carrier is absent, retained evidence is
verified, and the approved recursive `src/aros <= 17,700` interim gate is met.
Task 8 evidence recorded exactly 17,700 lines. The first reviewed
post-commission stop/final fix is `26fe611f`; the second fix `14d8268a` adds
nested descendant drain and ESRCH disappearance handling. The third fix
`c11eed14` preserves failed direct-KILL delivery truth. Current authoritative
code source `a07f50fc` records refreshed KILL only after successful delivery and
is 17,699 lines. None of these fixes begins Phase 0B or Phase A functionality.

### Phase 0B: Program-wide kernel consolidation (required)

- preserve the commissioned Phase 0A behavior and receipts;
- complete the remaining consolidation without adding Supervisor functionality;
- meet the original program-wide `src/aros <= 12,000 LOC` gate.

Exit: the full verification gates pass and recursive `src/aros <= 12,000 LOC`.
Phase A must not begin before this exit is met.

### Phase A: Mission Supervisor and budgets

- durable event inbox/outbox;
- single-Principal lease and event-driven wake;
- adaptive watchdog and mandatory next-wake deadline;
- mission/per-task resource ledgers;
- approval and notification envelopes;
- crash reconciliation without scientific decisions.

Exit: a synthetic long mission survives repeated Supervisor and Principal
termination, never sleeps permanently, never exceeds budget, and never invents
a scientific transition.

### Phase B: Real Researcher

- real provider-backed Researcher adapter;
- bounded TaskBrief, heartbeat, return, stop, and renewal request;
- one real exploratory inner loop with committed negative and positive evidence.

Exit: a real Researcher can iterate, survive Principal absence, return exact
artifacts, and be stopped or renewed without a fixed scientific workflow.

### Phase C: Source and dynamic portfolio

- provenance-preserving public and authenticated reads;
- credential broker and external-write approval boundary;
- at least three independent asynchronous Researcher directions;
- Principal-side deduplication and resource-aware portfolio decisions.

Exit: concurrent directions cannot corrupt shared state, leak credentials, or
duplicate an exact experiment silently.

### Phase D: Independent Reviewer and reporting

- fresh-context Reviewer adapter and independent worktree;
- reproduction and adversarial critique return;
- preliminary notification and formal result package;
- structural completeness verifier that does not interpret truth.

Exit: an unreviewed result cannot be sent as formal; the Principal must answer a
material objection before checkpointing the formal package.

### Phase E: Procedural learning

- candidate procedure records;
- cross-task recurrence evidence;
- Reviewer scope/failure analysis;
- explicit Skill promotion and rollback.

Exit: a one-off success cannot enter the active Skill set, while a repeatedly
verified procedure remains usable after a fresh Principal restart.

### Phase F: Full research commissioning

Run the complete milestone below. Only after it passes may the project compare
AROS against Arbor for default traffic and retirement.

## 15. Full milestone commissioning

The first complete commissioning is milestone-based, not wall-clock-based. It
uses a real external model, a non-fixture experimental open question, a real
code repository, real data, reproducible commands, and CPU or GPU execution.

```text
Principal frames a mechanism question
-> Source grounds literature and prior evidence
-> Principal records competing mechanisms
-> at least three Researcher directions explore asynchronously
-> Principal selects a decision-relevant uncertainty
-> Principal checkpoints a confirmatory preregistration
-> independent Eval records a reproducible result
-> independent Reviewer reproduces and attacks the Claim
-> Principal responds, narrows, rejects, or retains the Claim
-> formal result package checkpoint
-> every Agent session and Supervisor process is destroyed
-> watchdog/event recovery starts a fresh Principal
-> the fresh Principal explains the state and chooses the next research action
```

The result may be positive or negative. The driver may create the mission,
policy, environment, source credentials, adapters, and evaluator. It may not
write mechanisms, choose experiments, interpret results, answer the Reviewer,
or author the Claim package after agents start.

### 15.1 Required fault scenarios

- Kill Principal before and after a scientific terminal event.
- Kill and restart the Supervisor while Researcher and Run processes continue.
- Kill a Researcher carrier and verify completed, failed, and lost truth.
- Exhaust a task budget and reject unauthorized renewal.
- Reach a mission threshold and require a human expansion request.
- Attempt an external write and prove it remains blocked before approval.
- Run three writers in isolated worktrees and preserve all dirty work.
- Lose a wake event and prove watchdog rediscovery.
- Produce a valid negative result and carry it through review and reporting.
- Present an unpreregistered or unreviewed positive result and reject it as
  formal.
- Attempt secret leakage through prompt, environment output, logs, commits, and
  receipts.
- Restart with zero messages and recover the exact mission, mechanism model,
  portfolio, unread returns, budget, result package, and next action.

### 15.2 Evidence

Commissioning evidence must bind:

- exact product wheel and source commit;
- effective Principal, Researcher, and Reviewer provider/model/effort;
- all Task/Run/Eval/Source receipts and exact Git lineage;
- mission and per-task budget ledger entries;
- event delivery, lease, watchdog, approval, and notification records;
- Agent tool sequences and semantic Write bytes;
- preregistration, Reviewer return, Principal response, and final result package;
- crash injections, cleanup state, and zero-message restart packet;
- full tests, focused security/fault tests, lint, architecture boundaries, and a
  clean worktree.

## 16. Success definition

AROS has become a long-running experimental researcher when it can repeatedly
reduce important actionable uncertainty under a human-approved budget, preserve
negative and positive evidence, coordinate asynchronous real Researchers,
withstand independent criticism, survive the death of every session and control
process, and continue choosing scientifically meaningful actions from canonical
project memory.

Passing deterministic composition tests is necessary but insufficient. Passing
one real milestone proves the first complete capability but does not by itself
prove general scientific superiority over Arbor. Default replacement requires
subsequent matched-budget, multi-task, multi-seed non-inferiority evidence.
