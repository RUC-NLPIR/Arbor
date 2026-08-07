# AROS Arbor-Simple Kernel Design

Status: proposed implementation design
Date: 2026-08-07
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`

## 1. Decision

AROS must not be more complex than the Arbor research system it replaces.

Observed source size:

```text
legacy Coordinator + Executor + SearchAgent: 10,251 lines
current src/aros:                         27,124 lines
current checkpoint/transition/index:      8,946 lines
```

The current implementation has inverted complexity: the mechanism intended to
help a strong Agent checkpoint research has become almost as large as legacy
Arbor's complete research stack. The real `gpt-5.6-luna` attempt confirmed that
the dominant burden was proposal/assimilation/EvidenceLink ceremony rather than
scientific reasoning.

This design removes that ontology instead of hiding it behind a Skill, prompt,
adapter, or compatibility layer.

## 2. Reuse the proven Arbor topology

Original Arbor already has the correct subject structure:

```text
Coordinator Agent: global outer loop
Executor Agent: one Idea's inner research loop
SearchAgent: delegated source exploration
```

AROS renames and simplifies those subjects:

```text
Principal Research Agent
  owns complete Question/Model/taste/action choice

Researcher subagent
  owns one delegated direction's free inner loop

AROS kernel
  provides workspace, Git/worktree, process, Eval, receipt, and restart
```

The OS does not implement either Agent's scientific loop.

## 3. Principal outer loop

The Principal naturally interleaves:

```text
inspect workspace/runtime
-> read/search/reason
-> revise Question/Model/Idea
-> run a quick probe or delegate a Researcher
-> inspect return and measurement
-> interpret
-> ordinary Git checkpoint
-> continue, stop, or pivot
```

There is no K→Q→I phase machine, Idea queue, belief reducer, readiness enum,
transition proposal, or assimilation action.

Question, Model, Idea, Claim, source note, analysis, and NOW remain ordinary
editable files. Their prose owns scientific relation, scope, uncertainty, and
counterevidence.

## 4. Researcher inner loop

One selected direction receives a bounded brief:

```text
objective and why delegated
Question/Model/source refs
base commit and isolated worktree
read/write paths
capabilities and budget
required evaluator/controls
return/stop conditions
```

Within that boundary, the Researcher owns:

```text
understand
-> search/read
-> hypothesize
-> implement or probe
-> run small checks
-> iterate until technically valid
-> run the declared Eval
-> analyze phenomena and limitations
-> commit artifacts
-> return report + exact refs
```

The Principal does not micromanage these stages. A child may change method
within scope and report deviation. It may not silently change the Question,
evaluator, capability, comparison, or budget.

Child output is a worktree commit, report, logs, and receipts. It never writes
canonical scientific meaning directly. The Principal reads it, selectively
merges/applies it, and edits workspace meaning itself.

Async research means several independent Researcher processes, not an OS-owned
Idea scheduler. The Principal decides whether parallelism has positive gain.

## 5. What AROS adds beyond Arbor

AROS is justified only by four improvements:

1. **Workspace continuity** — versioned project mind instead of transcript
   replay plus IdeaTree/checkpoint synchronization.
2. **Durable Task/Run** — child and long experiment survive Principal exit.
3. **Independent measurement** — exact evaluator/commit/output receipt instead
   of parsing a score from Agent prose.
4. **Bounded restart attention** — fresh Agent sees Question, model,
   active work, unread returns, budget, and blockers without full history.

Anything else must prove positive gain or be deleted.

## 6. Model-visible system calls

AROS-specific model-visible tools are capped at five:

```text
Attention   bounded workspace/runtime observation
Task        launch/observe/stop/collect Researcher work
Run         durable arbitrary experiment process
Eval        registered measurement apparatus and receipt
Checkpoint  ordinary selected-path Git commit
```

Read, Grep, Glob, Write, Edit, and optional bounded Bash remain general Agent
tools, not AROS ontology.

No tool exposes:

```text
transition_audit
assimilation
update_belief
promote_model
resolve_question
select_idea
advance_stage
```

## 7. Git-native checkpoint

The only Principal-facing checkpoint input is:

```json
{
  "message": "Explain the coherent research change",
  "paths": ["questions/Q-0001/question.md", "model/CURRENT.md"]
}
```

There is no proposal file, base_commit field, assimilation list,
affected_paths, rationale anchor, audit call, admission object, EvidenceLink
JSON, transition ID, or transition directory.

Checkpoint uses mature Git behavior:

1. Require an attached branch and a clean ordinary Git index.
2. Validate each selected path is either a plain workspace file or an exact
   tracked path deleted by the Principal, always outside `.git/`, `.aros/`, and
   `.worktree/`.
3. Re-read exact selected bytes or exact tracked absence.
4. `git add -A -- <exact paths>`.
5. `git commit --only --no-verify -m <message> -- <exact paths>` with explicit
   AROS Principal system identity when repository identity is unavailable.
6. Verify HEAD advanced once and selected blobs equal the precommit bytes.
7. Return commit, parent, paths, and observed refs.

Git owns ref locking and atomic commit publication. AROS does not reimplement
Git's transaction, temporary index, commit-tree, compare-and-swap, shadow
projection, crash fence, or admission ledger in cooperative local mode.

Pre-existing staged changes block checkpoint. Unselected unstaged files remain
untouched. A failed commit leaves ordinary visible Git state; AROS never reset,
clean, rebase, merge, or discard work automatically.

## 8. Observation exposure without assimilation schema

The model never supplies observation refs to Checkpoint.

The host session records exact validated refs returned to the Principal by:

```text
Task.collect
Run.status/final observation
Eval.status/observe/audit
```

At successful Checkpoint, the host appends stable Git trailers automatically:

```text
AROS-Observed: tasks/TASK-.../collected.json
AROS-Observed: eval/evaluations/EVAL-.../receipt.json
```

This means only “the Principal session received this observation before this
checkpoint.” It does not mean supports, challenges, proves, resolves, or
assimilates. Scientific meaning remains prose in changed files.

If the session exits before checkpoint, the refs are not recorded and appear
again after restart. This is conservative and requires no model ceremony.

## 9. Attention without transition index

Attention derives:

- mission, active Question, current Model/rivals, uncertainty, and obligations
  from ordinary workspace files;
- Task/Run/Eval facts from their owning services;
- observed refs by reading `AROS-Observed` Git trailers;
- unread returns as terminal observation refs absent from those trailers;
- recent evidence as the latest checkpoint's observed refs and ordinary Git
  changed paths;
- dirty state, worktrees, budgets, authority class, and blockers directly.

There is no `TransitionIndex`, cache rebuild command, audit record, or hidden
semantic database. Git log scanning is the first-version implementation. An
index is not added until measured repository scale proves it necessary; if
added later it is a disposable performance cache over the same trailers.

Rename `unassimilated_returns` to `unread_returns`. The OS does not claim to
know whether scientific assimilation occurred.

## 10. Scientific evidence in ordinary files

Remove strict EvidenceLink JSON. A Claim uses ordinary Markdown:

```markdown
## Evidence

- `eval/evaluations/EVAL-.../receipt.json` — supports this scoped statement
  only under the fixed evaluator; limitations follow below.

## Counterevidence and limitations
```

AROS checks no relation enum, confidence scalar, rationale anchor, or exact
field set. Broken local links may produce navigation warnings, not checkpoint
denial. Human/Agent readers judge meaning.

Observation files remain immutable factual records. Merely mentioning a ref
does not change it or certify a Claim.

## 11. Operational records

Task/Run/Eval keep only schemas that protect research reality:

- stable ID and exact command/commit;
- process state and final receipt;
- evaluator apparatus/input/seed/environment/parser;
- raw output/artifact hashes;
- metric and validity state;
- worktree/return lineage;
- timeout, stop actor/reason, and resource facts.

Operational record commits call the same internal selected-path Git helper with
no Principal-facing schema. Delete `OperationalIntent`; tools pass exact record
paths and commit message to the helper callback directly.

The first simplification slice keeps current Task/Run/Eval behavior intact
except for replacing operational checkpoint plumbing. Task/Run/Eval are not
rewritten until their existing E2E passes through the new checkpoint helper.

## 12. Cooperative authority boundary

This design explicitly targets single-user, same-UID cooperative execution.

- Host decides whether the Principal receives Checkpoint.
- Model cannot enable it through tool arguments.
- Receipts and prompts never claim protected/non-bypass authority.
- No AdmissionGateway, admission receipt, budget ledger, lease fence, hidden
  broker, or security ceremony is implemented preemptively.

If protected multi-process authority is later required, an external process may
own the same Git operation. The Principal-facing Checkpoint API and workspace
meaning do not change.

## 13. First deletion slice

Delete, do not deprecate or wrap:

```text
src/aros/transitions.py
src/aros/transition_index.py
src/aros/checkpoint_bridge.py
src/aros/operational.py
strict EvidenceLink parsing from research_files.py
current checkpoint.py implementation
transition_audit Research action
aros transition audit
aros audit --rebuild-index
proposal/audit/admission transition files
all obsolete tests, fixtures, plans, and commissioning schemas
```

Replace `checkpoint.py` with a small Git-native implementation. Update
Attention, Research/Checkpoint tool wiring, Task/Run/Eval operational commit
callbacks, and the two current E2E drivers in the same slice.

There is no compatibility reader, migration command, alias, old schema import,
dual write, fallback path, or deprecation period.

Historical runtime directories are not imported. Current source documents that
normatively require the deleted schema are deleted or replaced; informative
failed-attempt evidence may remain only when it teaches a still-relevant lesson
and is clearly non-authoritative.

## 14. Complexity budgets

Budgets are hard acceptance gates:

```text
current src/aros                         27,124 LOC
after checkpoint/assimilation deletion <=19,000 LOC
after Task-on-Run simplification       <=12,000 LOC
long-term AROS research stack          <=10,500 LOC
```

Per-component first-slice budgets:

```text
checkpoint.py       <=350 LOC
research_tool.py    <=100 LOC
Attention core      <=700 LOC
model-visible AROS tools <=5
new dependencies      0
new config layers      0
```

Tests may be larger than production code where fault behavior warrants it, but
production growth cannot be justified by test count. Every replacement task
reports deleted/added production LOC.

## 15. Subsequent simplification

After the new checkpoint E2E is proven:

1. Make Task a thin worktree + Run + Researcher adapter instead of a duplicate
   process system.
2. Keep one durable Run carrier and one Eval wrapper over it.
3. Introduce the real Researcher inner loop with a bounded brief and ordinary
   report/commit return.
4. Add async Researcher concurrency only after one child E2E.
5. Add project Skill only after a repeated procedural problem exists.
6. Add MCP only as a thin transport over proven native services.

No future step resurrects transition, assimilation, EvidenceLink, IdeaTree, or
Coordinator state machines.

## 16. Failure semantics retained

Keep distinctions that change scientific action:

```text
transport failure
process failure/timeout
lost observation
invalid apparatus
underpowered measurement
scientific negative
contradictory evidence
authority/budget blocked
```

Only transport failure may retry the same external call under the provider's
bounded policy. Task/Eval/scientific reruns require a new Principal action.

These are factual states, not workflow phases.

## 17. Minimal end-to-end acceptance

One clean-wheel cooperative scenario must prove:

1. Native `aros start` boots a Question-centered KB.
2. Principal preregisters Model/Idea using ordinary files and
   `Checkpoint(message, paths)`.
3. One Task and one Eval produce immutable C/R and MeasurementReceipt.
4. Host automatically records returned Task/Eval refs at the next checkpoint.
5. Principal writes scoped Question/Model/Idea/Claim/NOW prose with ordinary
   Markdown links; no schema ceremony.
6. Final Git commit contains only selected semantic paths and automatic
   `AROS-Observed` trailers.
7. Fresh Agent Attention shows no unread Task/Eval return and explains the
   scientific state from canonical files.
8. Session death before checkpoint makes observations reappear as unread.
9. Current native-start and Task/Eval deterministic behavior remains verified.
10. Deleted transition/audit/index/assimilation modules and commands are absent
    from wheel, imports, help, docs, and tests.
11. Production LOC budgets, full tests, Ruff, architecture gates, and clean Git
    status pass.

This gate proves a simpler Agent-centric OS. It does not yet prove Researcher
LLM children, async Idea research, protected authority, Source Gateway, Skills,
MCP, or the complete AROS goal.
