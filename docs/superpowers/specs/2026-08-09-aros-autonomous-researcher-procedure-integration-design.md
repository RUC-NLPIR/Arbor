# AROS Autonomous Researcher Procedure Integration Design

Status: approved commissioning-only design; not a product capability claim
Date: 2026-08-09
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`

## 1. Decision

AROS will build one commissioning-only compatibility layer that distills useful
research procedures and deterministic tool patterns from two upstream research
agent repositories already available to the host. The integration will not copy
either upstream orchestrator, scheduler, state machine, scoring loop, or session
state.

Every new runtime component, Skill, adapter, schema, tool, and directory uses an
`aros` name. Upstream project names appear only in this design's source
discussion and in the single canonical source record described below; they do
not appear in runtime component names.

The purpose is to close one real autonomous scientific loop:

```text
immutable question
→ rival mechanisms
→ information-seeking experiment
→ factual evidence
→ ScientificModel update
→ adaptive follow-up
→ preregistered confirmation
→ independent review
→ Principal adjudication
→ Claim package
→ checkpoint and continuation
```

This work remains outside `src/aros` until a real cache campaign proves the
loop. It does not by itself make current AROS an autonomous researcher.

## 2. Source and naming policy

All source provenance is stored once in:

```text
commissioning/research_program/SOURCES.json
```

The file contains one entry per upstream repository with only:

- a stable opaque source id;
- repository path or URL;
- exact Git commit;
- license;
- selected paths;
- one concise adaptation summary.

Git commits bind the source contents. AROS will not add per-procedure source
files, copied prompt archives, import transcripts, duplicate NOTICE manifests,
or per-paragraph hashes. Procedures refer only to the opaque source id.

No new file, class, function, CLI, Skill, MCP tool, or user-visible label may
contain either upstream product name. Acceptable names include
`aros-source-research`, `aros-reviewer`, and `aros-research-mcp`.

## 3. Integration approaches considered

### 3.1 Copy the upstream skill suites

This is fast but rejected. The suites contain permissive shell access, direct
remote execution, their own state, score-based stopping, paper-production
workflows, and duplicate orchestration.

### 3.2 Run an upstream pipeline as an external Researcher

This is useful for an early canary, but its internal actions and state cannot
provide AROS-grade lineage. It remains an optional comparison, not the accepted
architecture.

### 3.3 Distill procedures behind AROS contracts

This is the selected approach. Scientific methods are rewritten as concise
AROS procedures. All actions pass through AROS adapters and canonical
Source/Task/Run/Eval/Git receipts. AROS retains one scientific state and one
budget authority.

## 4. Procedure set

The first delivery contains exactly six procedures.

### 4.1 `aros-source-research`

Searches scholarly and code sources, checks novelty, and audits citations. It
returns a `SourcePacket` containing query, source identity, retrieval time,
content reference, and hash. Search results are evidence, never scientific
verdicts.

### 4.2 `aros-rival-mechanisms`

Produces at least two independently stated, falsifiable causal mechanisms.
Every mechanism includes prediction, distinguishing observation, falsifier,
scope, and conflicts. It does not rank ideas by pilot score or automatically
select a winner.

### 4.3 `aros-experiment-design`

Chooses the next experiment lexicographically:

1. pass the essentiality, falsifiability, and decision-relevance gate;
2. maximize expected information gain per cost;
3. use concurrency only to improve coverage after the first two gates.

It produces an `ExperimentProposal`, not an execution command. Experimental
execution requires AROS Run/Eval authority.

### 4.4 `aros-evidence-update`

Reads immutable Run/Eval receipts and updates rival support, counterexamples,
negative results, remaining uncertainty, budget use, and the rationale for the
next action. It cannot reinterpret missing or failed measurements as negative
scientific evidence.

### 4.5 `aros-independent-review`

Runs in a fresh model context with no Researcher transcript. It receives only
the TaskBrief, preregistration, exact commits, source references, raw evidence,
and reproduction package. It actively checks alternative explanations,
leakage, statistics, scope, and reproducibility. Its output is an objection
report, not a canonical verdict.

### 4.6 `aros-claim-package`

Constructs a scoped Claim, evidence map, counterevidence, exact reproduction
commands, limitations, unresolved uncertainty, and Reviewer objections. It
cannot admit the Claim; only the Principal may accept, narrow, or reject it.

## 5. Component layout

```text
commissioning/research_program/
├── SOURCES.json
├── procedures/
│   ├── aros-source-research.md
│   ├── aros-rival-mechanisms.md
│   ├── aros-experiment-design.md
│   ├── aros-evidence-update.md
│   ├── aros-independent-review.md
│   └── aros-claim-package.md
├── contracts/
│   └── procedure_contracts.json
├── adapters/
│   ├── aros-deterministic-tools.py
│   ├── aros-source-adapter.py
│   └── aros-review-adapter.py
├── skills/
│   ├── aros-researcher/
│   └── aros-reviewer/
├── driver/
└── tests/
```

The filenames shown here are logical delivery names. Python importable modules
use underscores while all user-facing Skill and MCP names retain the `aros-`
prefix.

## 6. Durable scientific state

Canonical scientific state remains in the Task repository:

```text
research/
├── rivals/
├── observations/
├── decisions/
├── preregistrations/
├── reviews/
└── claims/
model/CURRENT.md
memory/NOW.md
```

Every scientific decision records:

- exact evidence read;
- mechanisms strengthened, weakened, or eliminated;
- the most important remaining uncertainty;
- why the next experiment has the highest expected information value;
- budget used and any renewal rationale.

Model transcripts, hidden provider state, upstream wiki state, Idea Trees, and
review scores are not canonical state.

## 7. Adapter and MCP boundary

### 7.1 Deterministic tools

AROS adopts the useful implementation pattern of keyless deterministic
operations separated from a thin MCP transport, including worker-thread
offloading for blocking operations. It does not expose legacy tree, scalar
evaluation, or guarded-merge semantics.

The commissioning MCP facade exposes only:

- source read/search;
- Task/Run/Eval request and status;
- observation/checkpoint;
- budget petition;
- Git and receipt lookup.

All calls resolve to current AROS services. No adapter stores a second session
truth.

### 7.2 Source adapter

The source adapter may call allowed host search capabilities and read public
papers, documentation, and code. It normalizes results into Source receipts.
It cannot download undeclared experimental data or perform external writes.

### 7.3 Review adapter

The review adapter adopts fresh-thread, model-identity, cross-family, async
submit/poll, and prompt/response hashing patterns. Provider credentials and
provider-local state remain host-owned. A review response is untrusted evidence
until independently bound and admitted by the Principal.

### 7.4 Forbidden adapters

The first delivery excludes remote job queues, SSH deployment, compute
purchase, notification services, cloud upload, paper synchronization, and
publication. Those actions require separate human-approved authority.

## 8. Researcher data flow

```text
Principal creates immutable TaskBrief
→ Researcher forms rival mechanisms
→ Researcher requests AROS Run/Eval
→ session exits
→ evidence event starts a fresh session
→ session recovers from Git without transcript replay
→ ScientificModel and decision record update
→ Researcher chooses the next experiment
→ confirmation is preregistered
→ fresh Reviewer reproduces and attacks
→ Principal accepts, narrows, or rejects
→ Claim package is checkpointed
→ the mission continues to the next uncertainty
```

An external heartbeat may detect lack of progress, dead processes, released
resources, or repeated zero-new-evidence wakeups. It may request a structural
pivot or human attention. It may never decide that evidence is sufficient or a
Claim is correct.

## 9. Researcher and Reviewer Skills

### 9.1 `aros-researcher`

The Researcher Skill loads the TaskBrief and current durable state, selects one
procedure, calls only allowed AROS tools, writes model-authored scientific
artifacts, checkpoints, and exits. It owns hypotheses, experiment choice,
adaptation, and interpretation.

### 9.2 `aros-reviewer`

The Reviewer Skill starts with an empty message history and a read-only frozen
evidence packet. It independently rebuilds, reruns, challenges, and reports. It
cannot edit candidate code, modify the Claim, or continue the Researcher
session.

## 10. Removed upstream behavior

The integration explicitly removes:

- score thresholds and fixed-round stopping;
- automatic selection of a top pilot result;
- positive-result preference;
- paper, rebuttal, poster, slide, and publication workflows;
- direct experiment execution outside AROS Run;
- remote GPU, SSH, queue, upload, and notification actions;
- duplicate coordinator, scheduler, session, tree, and merge state;
- self-review as a replacement for independent review;
- evaluator or heartbeat quality verdicts.

## 11. Procedure contracts

`procedure_contracts.json` defines exact input and output objects for:

- `SourcePacket`;
- `RivalMechanismSet`;
- `ExperimentProposal`;
- `ObservationUpdate`;
- `Preregistration`;
- `ReviewerReport`;
- `ClaimPackage`.

Contracts reject unknown fields, non-finite numbers, score-based acceptance,
unbound source references, missing falsifiers, and missing receipt links.
Scientific text remains model-authored; deterministic validation checks only
structure, provenance, authority, and consistency.

## 12. Failure semantics

- Search or reviewer transport may use a bounded transport retry.
- A scientific experiment always requires a new action and idempotency key;
  earlier results remain immutable.
- Missing measurements are operationally unavailable, not negative evidence.
- Reviewer unavailability blocks Claim admission.
- Failed zero-message recovery fails the capability gate.
- Budget exhaustion returns achieved evidence, remaining uncertainty, and a
  value-based renewal petition.
- Missing real data cannot be replaced by synthetic data for a real campaign.
- Any external write authority conflict stops for human approval.

## 13. Validation and promotion

The implementation is tested in this order:

```text
static forbidden-action checks
→ procedure contract tests
→ adapter transport/fault tests
→ frozen trajectory replay
→ synthetic commissioning
→ real cache campaign
→ standalone capability verification
```

A procedure remains commissioning-only until it:

1. succeeds in at least two distinct research tasks;
2. survives an independent Reviewer audit;
3. preserves negative evidence and exact provenance;
4. shows value after zero-message restart;
5. is explicitly promoted by the Principal/human.

Only then may it move into the repository's shipped top-level Skill suite.

## 14. First E2E capability gate

The cache campaign must show all of the following:

- only Root Question, repository, frozen data, budget, and factual evaluator
  were supplied;
- at least two falsifiable rivals were independently formed;
- at least three experiments were run;
- a later action cited a prior observation and changed course;
- zero-message recovery succeeded;
- confirmation was preregistered;
- at least one rival was eliminated;
- a fresh Reviewer independently reproduced and challenged the Claim;
- the Principal accepted, narrowed, or rejected every material objection;
- a Claim/evidence/reproduction/limitation package was produced;
- temporal-sealed R3 ran at most once;
- no direct experimental or external-write bypass occurred.

The campaign may pass Researcher capability with a negative scientific result.
One campaign does not prove general research quality.

## 15. Delivery sequence

### Wave 1: Procedure core

Create the single source record, six procedures, contracts, and static forbidden
behavior checks.

### Wave 2: AROS adapters

Create deterministic-tool, source, and review adapters. Normalize every result
into AROS receipts.

### Wave 3: Scientific Skills

Create `aros-researcher` and `aros-reviewer`, durable update rules, and
zero-message recovery.

### Wave 4: E2E driver

Create the event-driven commissioning driver, preregistration gate, independent
review flow, Principal adjudication, R3 handoff, and capability verifier.

### Wave 5: Real campaign

Provision the approved real trace portfolio, complete calibration, run the
campaign, and decide from evidence whether any component should be
productized.

Waves 1–4 do not require production traces. Wave 5 is blocked until the host
provides them.

## 16. Exit and non-claims

The first delivery exits only after the real E2E gate is independently
verified. Before that point:

- the integration is a commissioning artifact;
- current AROS cannot claim E2E autonomous research;
- current AROS cannot claim superiority over either upstream source;
- no procedure is a project Skill;
- no new product capability is added under `src/aros`.
