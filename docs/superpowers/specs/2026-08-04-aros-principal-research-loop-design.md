# AROS Principal Research Loop Design

Status: proposed implementation design
Date: 2026-08-04
Highest authority: AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md

## 1. Goal

Give a fresh Principal a small, transcript-independent view of what deserves
attention, then let that Principal explicitly assimilate real Task, Run, Eval,
and source observations into ordinary K/Q/I/model/memory files.

The first complete vertical slice must prove:

    canonical workspace + operational reality
    -> bounded ResearchAttentionPacket
    -> Principal chooses and interprets
    -> Principal edits only the semantic files that actually changed
    -> Principal authors a thin transition intent
    -> TransitionAudit produces mechanical testimony
    -> ProContract decides only canonical eligibility
    -> one Git ref compare-and-swap admits the complete snapshot
    -> a fresh Principal resumes without transcript or provider memory

AROS must preserve the Principal's scientific judgment across long runs,
compaction, process loss, model/provider replacement, and collaboration. It
must not replace that judgment with a workflow engine.

## 2. Product boundary

This design uses the following terms:

| Term | Owner | Meaning |
| --- | --- | --- |
| K | Principal | Knowledge and Claim views in the versioned workspace |
| Q | Principal | Questions, current answers, uncertainty, and stop criteria |
| I | Principal | Candidate actions and their expected information value |
| X | AROS | Executions: Task, Run, Eval, and source acquisition attempts |
| O | reality interface | Observations and immutable receipts produced by X |
| A | Principal | Assimilation: an explicit decision about how O changes project meaning |
| Authority | Human / kernel / evaluator / Principal | Constitutional, capability, measurement, and scientific authority remain separate |
| Admission | ProContract plus Git | Mechanical permission and atomic canonical promotion |

X/O/A means Execution / Observation / Assimilation. It does not mean
Execution / Observation / Authority. Assimilation, Authority, and Admission
must remain separate.

The permanent relation is:

    Principal-authored K/Q/I
              ^
              | explicit Assimilation and EvidenceLinks
              |
    X -> immutable O -> TransitionAudit -> ProContract -> Git CAS

TransitionAudit and ProContract never decide whether a Claim is true, whether
evidence is persuasive, which hypothesis is leading, or which action is worth
doing next.

In this design, admission means only:

> This exact Principal-authored transition, under this exact Research Contract,
> lease, capability, budget, evaluator boundary, base commit, and observation
> lineage may become the canonical Git snapshot.

## 3. Scope and decomposition

This slice includes:

1. Readable Question, Claim, Idea, model, and NOW views.
2. A bounded ResearchAttentionPacket derived from canonical K/Q/I and current
   X/O/A, never from chat history.
3. A mechanically parseable Observation-to-EvidenceLink-to-semantic-target
   relation authored by the Principal.
4. A four-field transition intent with explicit assimilations.
5. A deterministic, read-only TransitionAudit.
6. A narrow ProContract admission operation that consumes audit testimony and
   host-injected authority facts.
7. An atomic Git checkpoint whose sole canonical linearization point is a
   compare-and-swap of the canonical branch ref.
8. A real Task/Run/Eval -> MeasurementReceipt -> Claim/Question/Idea -> restart
   commissioning path.

This slice does not include:

- a semantic Coordinator, BeliefEngine, research phase machine, or next-action
  scheduler;
- automatic Claim scoring, confidence calculation, assimilation, retry,
  question resolution, stop, pivot, or expected-information-gain calculation;
- a graph database, hidden Claim database, or second canonical acknowledgement
  ledger;
- a new general budget engine or a second lease system inside AROS;
- protected Eval Gate D;
- automatic source-provider fallback implementation;
- MCP parity, provider parity, Skills lifecycle, or Arbor retirement.

The Source Gateway is the next independent slice; Section 14.6 reserves its
exit boundary. It does not enter this implementation plan.

The existing OpenCode ProContract is not imported as a scientific ontology.
AROS uses one narrow admission adapter over its institutional authority and
fencing. ProContract does not own K/Q/I, the research frontier, or scientific
obligations.

## 4. Canonical and operational state

Canonical scientific meaning remains ordinary versioned files:

    AROS.md
    memory/NOW.md
    memory/decisions/
    questions/FRONTIER.md
    questions/Q-*/question.md
    knowledge/claims/C-*.md
    ideas/I-*.md
    model/CURRENT.md
    model/rivals/

Versioned execution and observation records already produced by current AROS
retain their owning layer:

    tasks/TASK-*/brief.json
    tasks/TASK-*/collected.json
    runs/RUN-*/manifest.json
    runs/RUN-*/final.json
    eval/evaluations/EVAL-*/receipt.json

Admitted transition records are:

    transitions/T-*/proposal.json
    transitions/T-*/audit.json
    transitions/T-*/admission.json

Runtime state remains under .aros and is never staged:

    locks, process identity, mutable status, raw logs, event delivery,
    derived indexes, admission denials, local leases, and caches

The ProContract ledger remains authority state, not scientific state. AROS
never mirrors its lifecycle into K/Q/I.

A versioned observation record is canonical observation, not canonical belief.
An admitted EvidenceLink is a Principal-authored relation, not a verdict by the
kernel. A committed Claim remains revisable scientific meaning, not objective
truth certified by Git.

## 5. Canonical semantic views

### 5.1 Question

questions/Q-<id>/question.md has navigation-only frontmatter:

    ---
    id: Q-0001
    status: open
    parents: []
    scope: project
    last_revised: 2026-08-04
    ---

The Question template contains these headings:

    # Question
    ## Why load-bearing
    ## Current best answer
    ## Live alternatives
    ## Known facts
    ## Current uncertainty
    ## Unobserved variables
    ## Evidence that would change the answer
    ## Resolution criterion
    ## Stop / pivot criterion
    ## Expected information gain
    ## Links

AROS gates only UTF-8, stable IDs, ordinary-file shape, and parseable links.
Missing recommended sections become Attention warnings, not admission denial.
The Principal owns their applicability, content, remaining uncertainty, and the
decision to continue, stop, or pivot.

questions/FRONTIER.md may identify one optional dominant/focus question through
navigation frontmatter while retaining multiple live, blocked, speculative, or
deprioritized branches. It is an attention map, never an execution queue.

Its minimal navigation frontmatter is:

    ---
    focus_question: Q-0001
    ---

### 5.2 Claim

knowledge/claims/C-<id>.md uses navigation-only frontmatter:

    ---
    id: C-0001
    questions: [Q-0001]
    scope: project
    last_revised: 2026-08-04
    ---

Its template contains:

    # Claim
    ## Statement and scope
    ## Evidence links
    ## Counterevidence
    ## Assumptions
    ## Uncertainty and alternatives
    ## Consequences

When present, the Evidence links and Counterevidence sections contain the EvidenceLinks
themselves as strict one-object-per-line JSON:

    {"observation_ref":"eval/evaluations/EVAL-.../receipt.json","relation":"supports","scope":"declared experimental regime"}

The target Claim and section are implied by location. The Principal authors
only observation_ref, relation, and scope. AROS derives kind, record hash, Git
blob OID, target, and link identity, then checks exact observation lineage. It
never decides whether the relation is sensible or sufficient.

### 5.3 Idea, model, and NOW

An Idea exposes:

    why it is worth considering
    target question or bottleneck
    proposed action
    expected observations under live models
    minimal controls and evaluator
    cost, risk, and required capabilities
    what failure would still teach
    stop or return condition
    prior attempts and links

Expected information gain is a Principal-authored estimate, not a kernel
priority score.

model/CURRENT.md and model/rivals remain directly editable explanatory views.
memory/NOW.md remains the small continuity view for thesis, strongest
counterevidence, current uncertainty, obligations, blockers, and promising next
moves. AROS supplies templates and warnings but never rewrites existing meaning.

## 6. ResearchAttentionPacket

The packet is a bounded read-only observation. It is generated on every
aros boot, Inspect/Research attention call, Principal start, and provider
context-epoch restart after compaction or model switch. It is never persisted
and can be deleted without changing project state.

All entry points call one packet builder. Text output is a rendering of the
same JSON object, not a separately implemented boot summary.

### 6.1 Exact shape

    {
      "schema_version": 1,
      "snapshot": {},
      "active_question": {},
      "current_uncertainty": [],
      "recent_evidence_delta": [],
      "hypotheses": {"leading": [], "competing": []},
      "pending_measurements": [],
      "unassimilated_returns": [],
      "current_obligations": {"scientific": [], "institutional": []},
      "remaining_budget": {},
      "blocked_reasons": [],
      "authority": {},
      "warnings": [],
      "omitted": {}
    }

snapshot contains HEAD, canonical ref, branch, dirty paths, worktrees, and any
pending transition intent. It separates HEAD-admitted state from dirty pending
edits.

active_question is derived from the optional FRONTIER focus pointer and is null
when the Principal declares no single dominant question. When present, it
contains exact path/hash pointers and bounded verbatim excerpts for the
question, current best answer, current uncertainty, resolution criterion,
stop/pivot criterion, and expected information gain. Missing sections are
reported, never inferred.

current_uncertainty contains bounded verbatim section references from the
active Question, memory/NOW.md, and current model. A section reference contains
path, heading, content hash, and excerpt. No model summarizes it.

recent_evidence_delta contains only EvidenceLinks added or changed by the most
recent admitted transition that contains EvidenceLinks, as
path/anchor/link-hash pointers. It is the Principal's latest admitted evidence
delta, not a second rendering of pending operational observations.

hypotheses points to the current model and visible rivals. Leading means only
that the Principal placed a model in model/CURRENT.md. AROS does not rank them.

pending_measurements contains nonterminal Eval/Run facts and lost/missing states
that have no versioned terminal observation: identities, process state,
evaluator pointers, timestamps, and blockers. Once a terminal versioned record
exists, its pending detail appears only in unassimilated_returns.

unassimilated_returns contains terminal Task collections, Eval receipts,
standalone Run finals, and later source observations that have not been
explicitly assimilated by a valid admitted transition. It is the sole packet
location for terminal pending observation detail.

Each item contains only bounded factual fields and retrieval pointers: kind,
stable ref, record hash, result/artifact refs, provenance, diff ref when
applicable, stale-base status, apparatus/measurement state, and relevant
current counterevidence refs. Child/source prose and raw output remain
on-demand.

An Eval-linked Run is represented once by its Eval receipt; the linked Run final
is lineage closure, not a second pending scientific return. A standalone Run
final remains independently visible.

current_obligations separates Principal-authored scientific obligations in NOW
from outstanding ProContract duties. Delivery or event acknowledgement never
means scientific assimilation.

remaining_budget and authority are read from the host-injected ProContract
context. They use available, not_configured, or unavailable states and include
the enforcement class. Missing authority is never invented.

blocked_reasons combines factual process, apparatus, authority, budget, and
explicit Principal-authored blockers while retaining their owning layer.

### 6.2 Boundedness and determinism

- Default JSON/text rendering is at most 8,000 UTF-8 characters; the hard
  configurable maximum is 16,000.
- Stable ordering and per-list caps are deterministic for one observed
  snapshot.
- The packet contains no generation timestamp that would perturb an unchanged
  snapshot.
- Omitted entries produce counts and retrieval pointers.
- Raw logs, source bodies, child prose, and reviewer prose are never injected
  automatically.
- Every excerpt retains its path, heading, and content hash.

### 6.3 Unassimilated derivation

AROS enumerates service-validated versioned observations and subtracts only
observations named by an assimilation in a transition whose proposal, audit,
admission, and Git ancestry all validate at current HEAD.

Ordinary citations, evidence_refs, event ACKs, manually committed proposals,
failed admissions, and deferred review do not remove an observation.

A cache under .aros/indexes may map observation IDs to admitted commits. It is
fully rebuildable from Git and is never authoritative. Boot validates only the
cache's HEAD binding and at most the newest 256 transition/observation records.
If the cache is missing,
malformed, stale, or insufficient, boot conservatively re-displays bounded
pending pointers plus an index_incomplete warning; it never performs an
unbounded history rebuild or silently hides an observation. An explicit
aros audit --rebuild-index performs the full rebuild outside boot.

## 7. Observation, EvidenceLink, Claim, and admission

These states must remain distinct:

    source search result       provider observation
    source excerpt             exact located bytes
    child return               attributed candidate report
    Run final                  process observation
    MeasurementReceipt        evaluator-produced observation
    EvidenceLink              Principal-authored relation and scope
    Claim                     Principal-authored scientific statement
    TransitionAudit           mechanical testimony
    ProContract admission     authority eligibility decision
    Git checkpoint            canonical project snapshot

Task prose, reviewer PASS, process exit 0, a metric value, or audit validity
cannot directly create or change a Claim.

An EvidenceLink is the strict JSON line in the target K/Q/I file shown in
Section 5.2. relation is one of supports, challenges, bounds, or context. The
Principal chooses it. AROS derives its identity from transition, target path,
anchor, ordinal, and canonical bytes; it checks only that the observation
lineage is valid and that the target belongs to the same explicit assimilation.

An invalid or unavailable Eval receipt may be cited as process/apparatus
evidence but cannot be declared with observation kind measurement. A Run final
with exit code zero is never promoted into a measurement. An underpowered
receipt remains an underpowered observation; only the Principal can interpret
its scientific consequence.

## 8. Thin transition intent

The Principal writes transitions/T-<id>/proposal.json after editing semantic
files. It has exactly four top-level fields:

    {
      "schema_version": 1,
      "base_commit": "40-hex",
      "workspace_paths": [
        "knowledge/claims/C-0001.md",
        "memory/NOW.md"
      ],
      "assimilations": []
    }

The transition ID comes from the directory name. There is no proposal
self-hash, actor field, timestamp, duplicated question/claim/idea registry,
confidence score, assumptions list, or expected-effects list. Git, the
semantic files, and the generated audit already own those facts.

workspace_paths contains the files selected directly for this commit. The audit
classifies each path as Principal-authored semantic meaning or a
service-generated execution/observation record, then adds the exact required
lineage closure to its displayed candidate path set. Semantic paths may be any
coherent subset of K/Q/I/model/memory; no transition must update all of them.
Service paths must validate byte-for-byte through their owning Task, Run, or
Eval service. Nothing outside the displayed path set enters the commit.

An operational-only checkpoint may contain a newly created Task brief or other
validated execution record with an empty assimilations list. It changes no
scientific meaning and clears no pending observation. The owning Task, Run, or
Eval service mechanically creates this intent from the Principal's explicit
operation; the Principal does not hand-write a research proposal merely to
commit a service record. The intent still uses the same audit, ProContract, and
Git CAS core.

Each assimilation contains:

    {
      "observation_ref": "eval/evaluations/EVAL-.../receipt.json",
      "affected_paths": ["knowledge/claims/C-0001.md", "memory/NOW.md"],
      "rationale": "knowledge/claims/C-0001.md#Evidence links"
    }

The Principal supplies only a stable observation ref, affected paths, and one
rationale anchor. The owning service derives kind and record identity
(collected_sha256, receipt_sha256, or canonical record SHA-256); the audit
derives exact hashes, Git blob OIDs, and EvidenceLinks from the target files.

An assimilation is an explicit Principal assertion that the observation has
been considered and durably incorporated into project meaning. At least one
affected path must be a changed semantic workspace_path and must contain the
rationale anchor. Negative knowledge or a decision that no Claim changes still requires
a changed decision/NOW/Question/Idea path explaining why. If the Principal is
not ready to make such a durable judgment, the observation remains absent from
assimilations and therefore remains pending.

## 9. TransitionAudit

TransitionAuditService.audit(proposal_ref) is deterministic, read-only, and
side-effect free. Authority is a separate ProContract input, not audit
testimony.

The audit executable is a pinned control-plane artifact outside Principal
writable paths. Its trusted execution-closure hash covers the audit code,
Task/Run/Eval validators, interpreter/runtime, and controlled environment.
PATH, PYTHONPATH, and candidate imports cannot alter that closure. Its identity
and closure hash are supplied to ProContract. When AROS researches or modifies
its own source, the candidate AROS code never audits its own admission.

The audit returns:

    {
      "schema_version": 1,
      "transition_id": "T-0001",
      "base_commit": "40-hex",
      "current_head": "40-hex",
      "proposal_blob_sha256": "64-hex",
      "path_receipts": [],
      "observation_closure": [],
      "assimilation_links": [],
      "audit_payload_sha256": "64-hex",
      "candidate_subject_sha256": "64-hex",
      "mechanically_valid": false,
      "issues": []
    }

The candidate subject hash uses canonical UTF-8 JSON over:

    schema version
    transition ID
    base commit
    sorted workspace path, owning class, and Git blob OID tuples
    sorted versioned observation-closure path and Git blob OID pairs
    proposal blob SHA-256
    audit payload SHA-256

The audit payload is the canonical JSON object containing every field above
except audit_payload_sha256 and candidate_subject_sha256. audit.json contains
that payload plus both derived hashes. It never hashes itself. This avoids every
self-hash cycle. Git blob OIDs for untracked files are computed without writing
objects. The final Git tree and commit bind proposal, audit, admission, semantic
files, and observation closure.

The audit checks only:

1. Strict four-field proposal schema and stable transition ID.
2. base_commit equals the observed canonical ref; no silent rebase.
3. Every workspace path is an ordinary file inside an approved semantic or
   service-owned versioned root, with no symlink, submodule, runtime path, or
   escape. A semantic file is strict UTF-8; a service record is validated by
   its owning service.
4. Semantic files are UTF-8 with stable IDs and parseable links. Missing
   recommended sections are audit/Attention warnings, never admission issues.
5. Every assimilation ref resolves through exactly one owning service; the
   audit derives and binds its kind, record hash, and Git blob OID.
6. Every affected path is a changed semantic workspace_path relative to
   base_commit; the rationale anchor exists.
7. Every EvidenceLink is located in a changed affected path under its rationale
   anchor, names the same observation_ref as that assimilation, and has a
   mechanically valid relation/scope object.
8. Task collections validate brief/child/return commits and expose stale-base
   status. Stale prose may remain observable; stale code cannot be promoted
   without current-base reconciliation and required evaluation.
9. Eval receipts validate through current Eval and Run immutable lineage,
   including candidate commit, apparatus commit, parser, metric state, and raw
   output receipts.
10. A transition that jointly assimilates a Task return and its measurement
    derives one artifact lineage: MeasurementReceipt.candidate_commit must equal
    the Task collection child_commit. Unrelated Task and Eval records are
    independent observations and cannot satisfy the Task-to-Measurement closure
    gate.
11. Versioned observation closure is exact:
    - if a referenced record is already in base_commit, it is a ref only;
    - if it is newly generated, its service-derived versioned files enter the
      same admission path set;
    - .aros runtime records are validation inputs and never enter Git.
The audit does not rank evidence, compare hypotheses, resolve a Question,
compute confidence, decide whether an assimilation was wise, choose a commit
message, or mutate any file.

## 10. ProContract admission membrane

### 10.1 Host-injected context

The Principal cannot select its own actor, contract, lease, capability, or
budget in proposal.json. The native host or OpenCode adapter injects:

    contract ID, revision, and specification hash
    workspace identity and canonical ref
    Principal lease, session/prompt/attempt fence, holder, and expiry
    effective capabilities
    remaining budget and charge policy
    allowed evaluator/protected-eval policy
    TransitionAudit implementation identity and trusted execution-closure hash
    enforcement class

The model may inspect these facts but cannot replace the operation context by
supplying different tool arguments.

OpenCode stores the AROS-specific workspace/ref, effective-operation
capabilities, evaluator policy, and audit identity as one immutable
ResearchContractBinding keyed by contract ID and revision. Its exact hash is
shown at issue approval and bound by every AdmissionReceipt. It is adapter
authority state, not a scientific workspace or a replacement ProContract
kernel.

effective capabilities are the intersection of the issued Research Contract,
the OpenCode Location/agent permission set, and AROS operation policy. Coarse
filesystem.write or process.execute authority does not by itself imply
checkpoint, canonical-ref update, run launch, Eval access, or protected
evaluator visibility.

Research Contract formation preserves the exact human-approved limits. The
existing general OpenCode proposal path that widens small budgets to defaults
cannot form an AROS Research Contract unless that widening is explicitly
approved and represented in the issued contract.

Read-only attention and audit remain available without an active write lease.
Agent-authored canonical admission requires an active fenced Principal lease
and checkpoint capability. An explicit human-direct local route may operate
without an Agent lease, but is labelled cooperative and cannot satisfy the
final mediated-authority acceptance test.

In mediated mode the Principal operates in a candidate Location/worktree. The
canonical ref, ProContract ledger, admission credential, and ref-update
capability are outside the Principal's writable authority domain. Shell, when
granted, is bounded to that candidate Location and cannot update the canonical
ref or call the human-direct route. A same-UID process with writable repository
control state is cooperative mode regardless of prompt or tool labels.

An ordinary linked Git worktree is not such a boundary: its .git file points to
the shared common Git directory. Final commissioning therefore requires either
a broker-owned ProjectCopy/candidate repository whose promotion credential and
canonical repository are absent from the Location, or a mount/worker boundary
where the common Git directory is read-only to the Principal and only the
broker holds an already-open ref-update capability. Merely hiding the path in a
prompt or tool schema is insufficient.

### 10.2 Admission operation

The narrow adapter is:

    admit_transition(candidate_subject, audit_testimony, operation_context)
      -> AdmissionReceipt

An allow receipt binds:

    schema version
    decision = allow
    candidate subject SHA-256
    audit payload SHA-256
    contract ID/revision/specification hash
    workspace identity and canonical ref
    lease/session/prompt fence and expiry
    consumed capability
    budget before, charge, and remaining budget
    evaluator policy refs used by assimilated measurements
    research_contract_binding_sha256
    TransitionAudit implementation identity and trusted execution-closure hash
    enforcement class
    issued-at time
    receipt SHA-256

receipt SHA-256 is canonical JSON SHA-256 over every receipt field except the
receipt SHA-256 field itself.

AdmissionReceipt is a ProContract-owned schema and ledger event. AROS stores
and validates its exact serialized receipt in admission.json; it does not
reimplement a parallel contract, lease, or budget state machine.

ProContract allows only when the audit is mechanically valid, the contract and
lease are current and fenced, workspace/ref match, checkpoint capability is
effective, budget permits the operation, and referenced evaluator policy is
allowed. A deny receipt is retained in runtime state and never staged.

ProContract receives finite hashes and audit facts. It does not parse Claim
prose for truth, inspect hypothesis preference, decide evidence strength, or
choose the next scientific action.

One standing Research Contract may authorize many bounded transitions.
AROS must not create a ProContract obligation, scheduler node, or lifecycle for
every Claim or EvidenceLink.

### 10.3 Atomicity boundary

Issuing an allow receipt is authorization, not canonical admission. The only
canonical linearization point is the subsequent compare-and-swap of the Git
canonical ref from base_commit to the fully materialized commit.

If the Git compare-and-swap fails, canonical state is unchanged. The unused
allow receipt remains an attributable, budgeted authorization attempt.
ProContract and AROS reconcile it against Git; they do not claim a distributed
transaction across the ProContract ledger and Git.

## 11. Explicit checkpoint

The Principal invokes:

    aros checkpoint --proposal transitions/T-0001/proposal.json --message "..."

Checkpoint performs:

1. Acquire one narrow workspace-transition lock.
2. Read the canonical ref, HEAD, proposal, declared Principal bytes, current
   ordinary index entries, and relevant observation records.
3. Run TransitionAudit against an immutable in-memory snapshot.
4. Create a temporary Git index from base_commit. Do not stage through the
   user's ordinary index.
5. Materialize create-once audit.json, then add the exact proposal, audit,
   workspace paths, and newly generated versioned observation closure to the
   temporary index.
6. Compute the candidate subject and request ProContract admission using the
   host-injected operation context.
7. On allow, create admission.json, add it to the temporary index, write a
   commit whose sole parent is base_commit, and verify that its tree is exactly
   the audited candidate tree plus that one admission receipt.
8. Re-read and rehash the declared working bytes, their ordinary-index entries,
   canonical ref, contract revision, ResearchContractBinding hash,
   lease/session/prompt fence and expiry, and admission receipt.
9. Atomically update the canonical ref only if it still equals base_commit.
10. Update only the admitted paths in the ordinary index to their new HEAD
    entries. Preserve unrelated staged and unstaged work byte-for-byte.
11. Record an operational admitted-transition event pointing to the commit.

Unrelated dirty or staged paths are allowed and preserved. They block only when
they overlap an admitted path, make ownership ambiguous, or change a required
base/ref. No checkpoint runs an all-files cleanup, reset, rebase, merge, or
cherry-pick.

The Principal-provided message is the commit message. proposal.json,
audit.json, admission.json, semantic edits, and newly generated versioned
observations are in the same commit.

### 11.1 Crash and retry behavior

| Last durable point | Canonical result | Recovery |
| --- | --- | --- |
| before allow receipt | unchanged | preserve Principal edits; rerun audit |
| allow receipt, before Git CAS | unchanged | reuse only if contract revision, binding hash, lease/session/prompt fence, expiry, and subject still match; otherwise request a new receipt |
| commit object, before Git CAS | unchanged | unreachable object is harmless; retry by exact subject |
| Git CAS succeeded, ordinary index stale | admitted | detect transition at HEAD and repair only admitted index paths |
| Git CAS lost to another writer | unchanged for this intent | report stale base; never rebase automatically |
| post-CAS event missing | admitted | rebuild operational event/cache from Git |

Retry never reruns Task, Run, or Eval. It only reconciles the same checkpoint
subject. A different semantic diff or base commit requires a new audit and
admission.

Checkpoint never writes Claims, updates NOW, chooses assimilations, merges
child code, or acknowledges scientific evidence on behalf of the Principal.

## 12. Failure semantics

| State | Producer | Same semantic attempt may retry? | Scientific meaning |
| --- | --- | --- | --- |
| transport/rate-limit failure | source/provider adapter | yes, only under exact typed policy | none |
| process failure or timeout | Run/Task | no automatic retry | Principal inspects |
| lost execution/measurement | Run/Eval | no | unknown; never negative |
| invalid apparatus/eval | Eval | no | apparatus fact, not hypothesis result |
| underpowered | Eval measurement receipt | no | Principal interprets |
| scientific negative | Principal assimilation | not a retry category | scoped scientific outcome |
| contradictory evidence | Principal EvidenceLink | not a retry category | retained counterevidence |
| authority/budget blocked | ProContract | resume only after authority changes | no scientific outcome |

Only a transport failure may preserve the same semantic attempt. Even then the
adapter must prove that no effect was duplicated. Every scientific rerun needs
a new Principal action and idempotency key.

## 13. Public interfaces

CLI:

    aros boot [--json] [--max-chars N]
    aros transition audit PROPOSAL
    aros checkpoint --proposal PROPOSAL --message MESSAGE
    aros audit --rebuild-index

The native Principal receives one Research tool with:

    attention          read-only ResearchAttentionPacket
    transition_audit   read-only mechanical testimony
    checkpoint         explicit admission and Git CAS

The tool owns no semantic mutation API. Existing file tools remain how the
Principal edits K/Q/I/model/memory. Existing Task, Run, and Eval tools remain
the execution and measurement substrate.

The OpenCode custom tool is a thin adapter over these same services and injects
the ProContract operation context outside model-controlled arguments. MCP later
wraps the same services and state; it does not introduce a parallel ontology.

The eventual small model-visible vocabulary maps to the same services:

| Desired verb | AROS owner |
| --- | --- |
| source.search / source.get | later Source Gateway |
| artifact.read | existing bounded file/artifact reader |
| research.transition / checkpoint.audit | Research tool |
| experiment.run | existing Run/Eval services |
| measurement.observe | existing Eval/Run observation services |

claim.assess is scientific interpretation, so it is a Principal move or
project-local Skill, not a kernel admission syscall. A critic may return a
candidate assessment, but only the Principal can turn it into an EvidenceLink,
Claim edit, or assimilation.

## 14. Acceptance

### 14.1 Attention and continuity

- With no transcript and provider memory disabled, a fresh Principal recovers
  the active question, uncertainty, recent observation delta, leading and rival
  hypotheses, pending measurements, unassimilated returns, obligations,
  remaining budget, authority, and blockers.
- The packet is deterministic for one snapshot, removable, non-canonical, and
  bounded to the configured UTF-8 size.
- Admitted HEAD state and dirty pending edits are visibly distinct.
- Question resolution, stop/pivot criteria, expected information gain, and
  remaining uncertainty are visible without AROS calculating them.

### 14.2 Epistemic separation

- A source hit, source excerpt, Task prose, reviewer PASS, process exit zero,
  metric value, EvidenceLink, Claim, audit, and admission remain distinct.
- A naked citation or manually committed proposal cannot clear an
  unassimilated observation.
- Only an admitted explicit assimilation with a changed rationale path clears
  it.
- Invalid/lost outcomes cannot masquerade as valid measurement evidence.
- Audit and ProContract output contain no scientific verdict.

### 14.3 Authority

- The model cannot spoof actor, contract, lease, capability, budget, workspace,
  canonical ref, or evaluator policy through proposal/tool arguments.
- The mediated Principal cannot update the canonical ref, read the admission
  credential, or invoke the human-direct route through file or shell tools.
- Adversarial commissioning proves that direct git update-ref, direct common
  Git-directory writes, credential reads, and human-route calls all fail from
  the Principal Location.
- Candidate AROS code cannot replace the pinned TransitionAudit executable that
  judges its own admission.
- Adversarial PATH/PYTHONPATH/import pollution cannot replace any validator,
  interpreter, or environment component in the trusted audit closure.
- Expired lease, wrong fence/ref/workspace, missing capability, exhausted
  budget, or disallowed evaluator denies admission and leaves Git unchanged.
- Read-only attention/audit remain available while write admission is blocked.
- Cooperative native-local mode is labelled and never used to claim protected
  OpenCode authority.

### 14.4 Atomicity and recovery

- Fault injection at every checkpoint durable point produces either old HEAD
  or one complete admitted commit, never a partial canonical transition.
- An admitted commit contains proposal, audit, admission, every workspace path,
  and every newly generated versioned observation in the exact audited form.
- Unrelated staged/unstaged work survives byte-for-byte.
- A stale base or CAS conflict never rebases, retries science, or discards work.
- A fresh process recognizes a prior successful CAS even if index/event cleanup
  was interrupted.

### 14.5 Real end-to-end vertical slice

One real mediated scenario must complete:

    fresh boot with active Research Contract and Principal lease
    -> isolated child Task return
    -> durable Run and visible Eval MeasurementReceipt
    -> verifier proves Task collection.child_commit equals MeasurementReceipt.candidate_commit
    -> packet shows the Task return and measurement as unassimilated
    -> Principal reads return, apparatus, raw output, counterevidence, and stale status
    -> Principal edits only the K/Q/I/model/NOW files that actually change
    -> Principal authors an explicit assimilation for the Task return and a
       second explicit assimilation/EvidenceLink for the MeasurementReceipt
    -> TransitionAudit validates exact lineage without scientific judgment
    -> ProContract admits exact subject under lease/capability/budget/evaluator policy
    -> Git CAS creates one complete canonical commit
    -> Principal/session process is destroyed
    -> fresh Principal receives a new bounded packet from canonical state
    -> it can explain why the Claim/Question changed and what remains uncertain
    -> neither the Task return nor the MeasurementReceipt remains pending
    -> the Principal independently decides continue, stop, or pivot

The scenario must use current Task, Run, and Eval services and a real
MeasurementReceipt. A synthetic JSON fixture, mock ProContract, or same-session
summary is not commissioning evidence. The commissioning verifier rejects an
unrelated Task return paired with an unrelated Eval receipt.

### 14.6 Reserved Source Gateway exit

The later Source Gateway slice is not complete until a live typed
Semantic-Scholar rate-limit/transport failure falls back to OpenAlex, both
provider observations retain separate query/time/ID/raw-hash provenance, a
scientific negative does not trigger fallback, and neither result creates an
EvidenceLink or Claim automatically.

## 15. Implementation order

1. K/Q/I templates, minimal validators, and exact ResearchAttentionPacket over
   existing workspace/Task/Run/Eval state.
2. Four-field transition intent, explicit assimilation, EvidenceLink parsing,
   and read-only TransitionAudit.
3. Temporary-index candidate construction, exact observation closure, Git CAS,
   and crash recovery against a test-only admission fixture; no production
   bypass.
4. Narrow OpenCode ProContract admission operation and host-injected operation
   context; commissioning accepts no fixture or local allow path.
5. Native Research tool, CLI, and thin OpenCode adapter over the same services.
6. Synthetic adversarial tests for spoofing, naked refs, invalid apparatus,
   stale base, unrelated dirt, every crash point, and cache deletion.
7. Real mediated Task/Run/Eval -> Measurement -> Claim -> restart
   commissioning.

Every implementation task uses RED -> GREEN tests, focused regression, exact
receipts, Design Book review, and independent security/simplicity review.

Source Gateway, protected Eval, Skills/MCP/provider parity, migration, and Arbor
retirement follow as separately designed slices. None may be declared complete
from this Principal Loop commissioning alone.
