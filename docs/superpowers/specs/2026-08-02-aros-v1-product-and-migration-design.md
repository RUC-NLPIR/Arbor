# AROS v1 Product, Architecture, and Arbor Migration Design

Status: approved architecture draft  
Date: 2026-08-02  
Highest authority: AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md

## 1. Purpose

This document freezes how the Design Book v1 is implemented in the current Arbor repository and how Arbor is progressively replaced by AROS.

The final product has:

- one public command: aros;
- one distribution: aros-agent;
- one Python namespace: aros;
- one kernel implementation shared by CLI, MCP, and provider adapters;
- one scientific principal: the Principal Research Agent;
- no semantic Coordinator, universal research cycle, or IdeaTree scheduler;
- no duplicate Arbor and AROS state writers.

This is not a reduced CLI-renaming project. Completion requires the Design Book Phase 0–6 exits and invariants I1–I25 to be demonstrated with real tests and receipts.

## 2. Product axiom

Within the human-authorized boundary:

~~~text
Agent chooses and interprets.
Workspace remembers.
AROS kernel executes, isolates, measures, records, and recovers.
Reality has final veto.
~~~

The Principal owns all scientific semantics:

- questions and priorities;
- models, rivals, and mechanisms;
- experiment and evaluator design;
- interpretation of observations;
- action selection;
- child assimilation;
- decisions to retry, pivot, merge, publish, or stop.

The kernel owns only mechanical invariants:

- paths and capabilities;
- Git and worktree lineage;
- process identity and liveness;
- exact commands and environments;
- budgets and leases;
- raw output and hashes;
- deterministic parser invocation;
- receipts, events, and operational recovery.

No kernel service may decide the scientific value of a result or automatically select the next research action.

## 3. Scope and non-goals

### 3.1 Required v1 scope

AROS v1 includes:

1. Native Principal and bootable project memory.
2. Durable foreground/background process control.
3. Linux capability isolation.
4. Child task/worktree/return/assimilation substrate.
5. Visible and protected deterministic evaluation.
6. Checkpoint, search, audit, events, budgets, and leases.
7. Readable Question/Model/Idea/Knowledge/Skill workspace views.
8. Native CLI and MCP adapters over the same kernel.
9. One-way Arbor data migration.
10. Commissioning, public entry cutover, and legacy retirement.

### 3.2 Explicit non-goals

The implementation does not add:

- a semantic Coordinator above the Principal;
- a universal framing/modeling/ideation cycle;
- an automatic scientific frontier or campaign driver;
- a graph database or opaque memory service;
- a recursive persistent agent swarm;
- MCTS/PUCT as kernel policy;
- automatic belief, model, or skill promotion;
- cluster scheduling, a dashboard platform, or multi-tenant hosting;
- automatic retry of scientifically meaningful lost evaluations;
- claims of protection against host root or arbitrary hostile same-UID processes.

## 4. System architecture

~~~text
Human owner
    |
    | goal, values, safety, budget, publication authority
    v
Principal Research Agent
    |\
    | \---- Child agents: explore, implement, observe, criticize, verify
    |
    +---- Versioned workspace / KnowledgeBank
    |
    +---- AROS system calls
              |
              +-- Workspace / Checkpoint / Search
              +-- Run / Process / Receipt
              +-- Task / Child / Worktree / Return
              +-- Eval / Recipe / Parser / Disclosure
              +-- Git / Merge / Provenance
              +-- Event / Budget / Lease / Recovery
              +-- Audit
              |
              +-- CLI and MCP adapters
~~~

### 4.1 Dependency rule

During migration, AROS may reuse an explicit allowlist of generic Arbor substrate:

- core Agent loop;
- LLM provider adapters;
- deterministic file, Git, process, and parser helpers;
- verified authentication/configuration helpers.

AROS must not import:

- CoordinatorOrchestrator;
- IdeaTree scientific state;
- fixed Arbor cycle logic;
- mandatory role pipelines;
- legacy campaign/frontier scheduling;
- transcript-dependent resume logic.

The dependency direction is one-way:

~~~text
temporary Arbor compatibility shim -> AROS implementation
AROS implementation -X-> legacy semantic modules
~~~

CI enforces this boundary and enforces that legacy code has no net feature growth.

## 5. Canonical workspace

The Git workspace is the KnowledgeBank. Semantic state remains ordinary readable files.

~~~text
AROS.md
AGENTS.md
memory/
questions/
model/
rivals/
ideas/
knowledge/
experiments/
runs/
eval/
tasks/
events/
analysis/
.agents/
.worktree/
.aros/
~~~

Rules:

- AROS.md records mission, constitutional constraints, and scope.
- memory/NOW.md and related files record current orientation and obligations.
- Questions, models, ideas, analyses, and skills are Principal-editable files.
- .aros contains kernel-owned operational state, locks, process identities, and derived indexes.
- .worktree contains isolated write-heavy child/evaluation checkouts.
- Deleting derived indexes never deletes semantic source files.
- Chat transcripts and provider sessions are disposable traces, not canonical memory.
- No state is dual-written to Arbor and AROS formats.

## 6. Principal

The public aros start command creates the existing general-purpose Agent directly with:

- project-local boot context;
- bounded workspace file tools;
- Inspect, Run, Eval, Task, Checkpoint, Search, and Audit system calls;
- optional explicitly trusted foreground shell;
- automatic Git commit disabled.

The Principal may directly edit semantic files. AROS does not issue next-idea, advance-model, or run-campaign instructions.

Restart procedure:

1. Read AGENTS.md and AROS.md.
2. Run aros boot/status.
3. Inspect current Git and dirty ownership.
4. Reconcile runs and tasks.
5. Read unread events and child returns.
6. Re-observe uncertain reality.
7. Choose the next scientific action.

## 7. Run and process service

RunService remains the source of durable process truth.

It provides:

- prepare/start/status/list/tail/observe/stop/finalize;
- exact argv and workspace-relative cwd;
- explicit isolated-linux or trusted-local profile;
- idempotent launch;
- tmux as carrier, never authority;
- PID, process-group, host, and start-token identity;
- bounded timeout and attributed stop;
- raw stdout/stderr;
- create-once final receipt;
- run-complete event;
- reconciliation after Principal or tmux loss.

RunService never parses a scientific metric or interprets process failure as a scientific result.

## 8. Child task substrate

### 8.1 Task contract

A Principal-created task contains:

- task ID and actor;
- question and objective;
- exact base commit;
- read/write capabilities;
- deliverables and acceptance checks;
- budget/deadline;
- child profile;
- ownership lease;
- return location.

### 8.2 Execution

- Read-only children may share a checkout only when no writes are possible.
- Write-heavy children always use a dedicated .worktree/ task checkout.
- Child agents may inspect, edit, run, and scientifically pivot within their brief.
- The task service records process/worktree truth but does not direct the child's scientific reasoning.
- Cleanup removes only verified clean worktrees.
- Dirty or ambiguous work is preserved.

### 8.3 Return and assimilation

A child return records:

- what changed;
- evidence and measurements;
- deviations and uncertainty;
- commits/files/artifacts;
- recommended follow-up.

Completion creates a child_done event. It does not automatically change the Principal's model, ideas, or beliefs.

The Principal:

1. reads the brief, return, diff, and receipts;
2. checks provenance, conflicts, deviations, and evaluation;
3. decides merge/selective-apply/reject/preserve;
4. updates semantic workspace files;
5. creates a coherent checkpoint.

## 9. Evaluation

### 9.1 Principle

Evaluation is an apparatus, not an Agent opinion.

The Principal or evaluator subagent may select, launch, and monitor a versioned recipe. Measurement is produced only by deterministic execution and parsing.

### 9.2 Versioned contract

An evaluator manifest records at minimum:

- evaluator ID and version;
- exact candidate commit;
- recipe command and recipe commit/hash;
- working directory and explicit inputs;
- environment reference;
- seed policy;
- resource request and timeout;
- raw outputs and metric output;
- success exit codes;
- parser contract;
- protected input handles and disclosure policy;
- limitations and calibration references.

### 9.3 Visible evaluation flow

~~~text
Principal freezes candidate + recipe
    -> optional evaluator/observer subagent calls Run
    -> RunService executes recipe in isolated clean worktree
    -> raw logs/artifacts are persisted
    -> fixed parser reads one declared machine output
    -> kernel writes measurement receipt
    -> subagent returns receipt pointer
    -> Principal interprets
~~~

The subagent may monitor liveness, log deltas, resources, and anomalies. It may not set the primary metric through prose.

### 9.4 Protected admission flow

Protected admission separates two processes:

1. an untrusted candidate producer runs the exact candidate and writes sealed target output;
2. a trusted scorer runs independently with protected handles and the sealed target output.

The candidate cannot write scorer stdout or terminate the scorer. Public output contains only the disclosure-approved aggregate.

Hidden paths, raw protected logs, item-level output, and protected manifests are not exposed to Principal tools or model context.

The local V1 boundary protects against isolated candidate/evaluator children and ordinary Principal tool misuse. It does not claim security against arbitrary hostile same-UID processes. Stronger host-adversary protection requires a separate Unix identity, CI, or remote evaluation service.

### 9.5 Failure and retry

Evaluation is foreground and deliberately simple:

~~~text
valid receipt        -> exact measurement observation
process failure      -> failed_process
parser rejection     -> invalid_eval
insufficient samples -> underpowered
negative metric      -> valid scientific negative
process absent and no receipt -> lost
~~~

Lost never triggers automatic retry. The observer reports lost; the Principal decides whether to inspect, revise, or launch a new evaluation with a new idempotency key.

The kernel may repair a missing derived status pointer from an existing create-once receipt. It may not create a measurement or automatically launch another attempt.

## 10. Isolation and security claims

### 10.1 Enforced profiles

isolated-linux provides:

- exact Landlock ABI gate;
- explicit readable/writable roots;
- .git/.aros/.worktree protection;
- no network;
- restricted process/system calls;
- capability drop and no-new-privileges;
- environment allowlist;
- resource limits;
- fail-closed behavior when isolation cannot be installed.

trusted-local provides audit and accident resistance only. It is never described as a security sandbox.

### 10.2 Claim discipline

Every security statement must name:

- protected asset;
- adversary;
- enforcement mechanism;
- unsupported adversary;
- evidence test.

Examples:

- A candidate child cannot read the protected store because Landlock grants no path and protected data arrives only through controlled read-only descriptors.
- A same-UID arbitrary host process is not prevented from accessing owner-readable local state; this is outside the V1 claim.
- A Principal with trusted shell cannot use protected local admission concurrently.

Hashing detects changes; it does not create an authority boundary by itself.

## 11. Events, budgets, leases, and recovery

### 11.1 Events

Kernel events contain facts and pointers, not scientific interpretations.

Required event kinds include:

- run completed/lost;
- child done;
- evaluation completed/lost;
- anomaly;
- budget threshold;
- permission required;
- deadline;
- lease conflict.

Acknowledge means operationally read, not scientifically assimilated.

### 11.2 Budgets

The kernel:

- records compute/token/time/spend;
- reserves protected disclosure budget before input resolution;
- rejects operations exceeding hard limits;
- reports thresholds.

The Principal decides the scientific allocation of remaining budget.

### 11.3 Leases

Leases protect ownership and incompatible capabilities:

- one active Principal lease;
- task/worktree ownership;
- protected evaluation incompatible with AROS-mediated trusted-local execution;
- expired leases are inspected, not silently stolen.

### 11.4 Recovery

Recovery reconstructs process truth from process identity, Git, and receipts.

It never invents a missing terminal measurement and never makes a scientific retry decision.

## 12. Checkpoint, search, audit, and skills

### 12.1 Checkpoint

aros checkpoint:

- inspects dirty ownership;
- validates project-local schemas and links;
- updates only derived indexes;
- assists an intentional Git checkpoint;
- never auto-writes scientific conclusions.

### 12.2 Search

aros search covers:

- workspace text;
- Git history;
- runs, receipts, tasks, events, and returns;
- optional derived indexes.

### 12.3 Audit

aros audit checks:

- protected path separation;
- command/commit/environment provenance;
- stale pointers and missing artifacts;
- receipt/raw-output integrity;
- budget and lease state;
- dirty worktree preservation;
- migration completeness.

### 12.4 Skills

Project skills are versioned user-space procedural memory. Agents may edit them. Promotion is an explicit Principal/human decision, not automatic kernel evolution.

## 13. Public interface

The target CLI is:

~~~text
aros init
aros boot
aros status
aros start
aros checkpoint
aros task create|start|status|message|stop|collect
aros run prepare|start|status|list|tail|observe|stop|finalize
aros eval register|run|admit|status|observe|audit
aros worktree create|list|preserve|prune
aros search
aros audit
aros version
~~~

The CLI contains no semantic commands such as next-idea, mature-model, or run-campaign.

MCP exposes the same operations and schemas. It never owns separate state.

## 14. Public-entry and namespace migration

The migration uses a strangler public-first strategy.

### Gate E1: first-class AROS entry

- Add the aros console script backed by the AROS CLI app.
- aros commands are top-level, not nested under arbor.
- arbor aros becomes a pure forwarding compatibility route.
- New capabilities are added only to aros.

The distribution may temporarily remain arbor-agent and the implementation may temporarily import the retained arbor.core allowlist.

### Gate E2: legacy freeze and command migration

- CI rejects feature growth in legacy Coordinator/Executor/Arbor-cycle paths.
- Each old command remains frozen until an equivalent AROS capability is commissioned.
- Once migrated, the old command becomes a pure forwarding/deprecation shim.
- No command owns a second implementation or writes both state formats.

### Gate E3: product distribution cutover

After fresh AROS commissioning:

- publish aros-agent as the code-bearing distribution;
- make arbor-agent a temporary empty dependency/deprecation metapackage;
- migrate global config once from ~/.arbor to ~/.aros;
- write only ~/.aros after migration;
- update skills, plugin, MCP registration, docs, and examples to aros.

### Gate E4: CLI retirement

After one compatibility release:

- remove executor, coordinator, run-research, and review-research entry points;
- retain arbor only as a migration error/deprecation stub for one release;
- remove the arbor command after the declared sunset gate.

### Gate E5: Python namespace retirement

After legacy code is deleted:

- move retained generic substrate into the aros namespace;
- change internal and external imports to aros;
- retain an arbor import compatibility shim for one release;
- remove the arbor namespace in the next major release.

The final installed product contains no arbor command, distribution, runtime state writer, or Python namespace.

## 15. Arbor data migration and retirement

Migration is one-way and receipt-backed.

~~~text
ResearchFrame       -> AROS.md + questions/
sources/evidence    -> knowledge/ + runs/
ScientificModel     -> model/CURRENT.md + rivals/
IdeaGraph           -> ideas/ + links
trajectories        -> experiments/ + runs/
ExperienceFS        -> run/artifact pointers
prompts/policies    -> .agents/skills or historical docs
campaign report     -> memory/episodes + project report
~~~

Rules:

- old data remains historical until audited;
- import never upgrades historical claims to current truth automatically;
- migration writes an exact source/hash/destination receipt;
- migration never dual-writes future changes;
- old JSON/SQLite/session data is deleted only after audit and backup;
- semantic Coordinator and duplicate schedulers are deleted only after equivalent AROS gates pass.

## 16. Implementation waves

Existing completed foundation:

- Wave 0: document authority and legacy baseline.
- Wave 1: Native Principal and bootable workspace.
- Wave 2: durable RunService.
- Wave 3: Linux capability isolation.

Remaining work:

1. Public entry wave: first-class aros command and legacy freeze gates.
2. Child wave: TaskService, leases, worktrees, returns, assimilation evidence.
3. Eval wave: simplify the experimental M4 branch to recipe/RunService/parser/lost semantics.
4. Operations wave: events, budgets, checkpoint, search, audit, skills.
5. Semantic migration wave: Questions/Models/Ideas/Knowledge and one-way Arbor import.
6. Adapter wave: CLI/MCP/provider parity over one kernel.
7. Commissioning wave: end-to-end acceptance and public traffic switch.
8. Retirement wave: delete old commands, legacy modules, distribution, and namespace.

Each wave is independently testable, committed, and reversible. No wave claims completion from mocks alone.

## 17. Error handling

General rules:

- missing or inconsistent authority fails closed;
- isolation unavailable fails closed;
- dirty ownership ambiguity preserves work and stops cleanup;
- process disappearance without receipt becomes lost;
- stale pointer remains visible to audit;
- budget exhaustion returns a factual denial;
- adapter/provider loss never deletes committed workspace artifacts;
- no recovery path invents scientific or process truth;
- no automatic retry converts an operational failure into a scientific decision.

## 18. Verification and commissioning

### 18.1 Module gate

Every module requires:

1. failing tests for the target invariant;
2. minimal implementation;
3. focused tests;
4. a real behavior smoke test;
5. full regression;
6. exact receipt/evidence document;
7. spec-compliance review;
8. code-quality/security review.

### 18.2 End-to-end commissioning

Required commissioning scenarios:

- fresh aros init/boot/start without transcript;
- restart from workspace with correct mission/current uncertainty;
- long experiment survives Principal exit;
- run stop/timeout/lost truth is reconciled;
- parallel read-only and write-heavy child tasks return without shared corruption;
- Principal assimilates a child diff/return intentionally;
- visible evaluator subagent runs a versioned recipe and returns a receipt pointer;
- forged candidate output cannot set the primary metric;
- protected admission uses exact commit and leaks no hidden item/path;
- lost evaluation waits for Principal action and is not retried automatically;
- dirty worktrees survive cleanup;
- stale pointer and missing artifact are detected by audit;
- CLI and MCP produce the same kernel state;
- provider switch preserves workspace continuity;
- Arbor historical import is one-way and auditable;
- aros is the documented/default public entry;
- legacy code paths receive no production traffic.

### 18.3 Final retirement gate

Arbor is removed only when:

- all Design Book Phase 0–6 exits pass;
- invariants I1–I25 have executable evidence;
- no current docs/config/scripts invoke Arbor;
- no AROS module imports a legacy semantic module;
- migration receipts cover retained historical data;
- the compatibility release and sunset gates are complete;
- a clean install exposes only aros;
- import aros succeeds and import arbor is absent.

## 19. Success definition

AROS v1 is complete when a new strong Agent can enter an existing project with no transcript, accurately understand the mission and live uncertainty, launch and observe durable work, delegate isolated children, obtain independent measurements, assimilate evidence, checkpoint its project memory, survive restart/provider change, and continue choosing scientific actions without a semantic controller.

The implementation is successful only if the kernel remains smaller in meaning than the Agent: it enforces mechanics while leaving science in user space.

