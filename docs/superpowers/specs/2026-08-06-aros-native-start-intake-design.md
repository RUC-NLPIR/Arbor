# AROS Native Start and Local Research Intake Design

Status: proposed implementation design
Date: 2026-08-06
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`

## 1. Goal

Make one native Arbor command the complete entry into an AROS research world:

```text
aros start
-> native boot transition
-> Key Research Question
-> local Git workspace or new paper-only workspace
-> zero or more local PDF/Markdown materials
-> canonical KnowledgeBank bootstrap commit
-> bounded ResearchAttentionPacket
-> Principal Agent
```

An already initialized workspace skips intake and boots directly from canonical
state. The command never replays a transcript or depends on provider memory.

This is the first product-facing scientific slice. It does not add an Idea
scheduler, async swarm, Source Gateway, MCP surface, or automatic Skill loader.

## 2. First-principles check

The design is essential only if these statements remain true:

1. KnowledgeBank is the entire versioned workspace, not the `knowledge/`
   directory and not a hidden database.
2. Question is the load-bearing topology, ScientificModel is the Principal's
   explanatory compression, and IdeaGraph is its action map.
3. Q/M/I are editable user-space views. The OS does not advance a fixed
   K→Q→I stage machine or choose the next scientific action.
4. Child returns, source material, process results, peer review, and
   MeasurementReceipts are observations. None becomes a Claim or resolved
   Question without explicit Principal assimilation.
5. The kernel owns workspace mechanics, Git, Task, Run, Eval, receipts,
   authority, recovery, and bounded Attention. It does not own scientific
   meaning.
6. Human goal, capability, budget, stop, safety, and publication authority stay
   outside model-controlled tool arguments.

The natural research loop remains:

```text
Attention
-> Principal chooses read/search/model/delegate/run/stop
-> reality returns observations
-> Principal interprets and edits Q/M/I/Claim/NOW
-> explicit assimilation and checkpoint
-> fresh Attention
```

## 3. Scope

### 3.1 Included

- One principal Git workspace per KnowledgeBank.
- Existing local Git repository as the workspace.
- New Git repository for a paper-only topic.
- Zero or more local `.pdf` and `.md` materials.
- Exact source bytes, SHA-256 identity, extracted Markdown/text, and provenance.
- One canonical Q-0001, FRONTIER focus, model placeholder, NOW, and source
  manifest.
- A coherent initial Git commit containing only AROS-owned bootstrap paths.
- Native Rich/Typer/prompt-toolkit intake and a non-interactive flag path over
  the same pure intake service.
- Existing provider configuration and native Principal Agent start.
- Replacement clean-wheel commissioning through the public `aros start` path.
- Removal of public `aros init` after the replacement path is commissioned.
- Removal of the `arbor aros` forwarding mount, warning, and retirement shim
  after the direct entry is re-commissioned.

### 3.2 Excluded

- Remote Git URL cloning.
- Paper URL download or online scholarly search.
- Semantic Scholar/OpenAlex/Crossref fallback.
- Multiple attached repositories in one first-version KB.
- Async Idea generation or multi-child orchestration.
- Peer/human resolution UI.
- Protected Eval, Principal broker/lease/budget, or non-bypass authority.
- Project Skills discovery/lifecycle.
- AROS MCP and provider parity.
- Migration readers, legacy session import, or compatibility aliases.

Adding later local/remote source kinds extends the same source-record boundary;
it does not replace the workspace, Principal, observation, or assimilation
architecture.

## 4. Existing maintained components

No dependency is added.

| Need | Existing maintained component |
| --- | --- |
| CLI commands and typed options | Typer |
| Boot panel, spinner, visual transition | Rich Console/Live/Spinner |
| Interactive terminal input | prompt-toolkit and existing intake patterns |
| PDF extraction | pypdf |
| Git repository operations | existing Git CLI helpers/subprocess pattern |
| Workspace continuity | `ResearchAttentionService` |
| Principal execution | existing Arbor `Agent.run` and provider factory |
| Child/run/evaluation | current Task/Run/Eval services |
| Canonical admission | Research/TransitionAudit/Checkpoint/Git CAS |

Existing legacy assets are references, not dependencies:

- `src/cli/intake/` supplies proven Rich/prompt-toolkit interaction patterns.
- `arbor-agent-setup-intake` supplies question/material/permission prompting
  lessons.
- `arbor-agent-executor`, `merge-eval`, `search`, and `resume-report` supply
  worktree, evaluator, source, and recovery lessons.
- `src/mcp/server.py` demonstrates a stateless thin transport adapter.

The old Coordinator phase loop, IdeaTree queue, `tree_*` MCP vocabulary,
`.arbor/sessions` canonical state, and automatic semantic fan-in are not reused.

## 5. Product entry

The canonical product entry is the direct executable command:

```text
aros start
```

`/aros-start` is the visual action name a future host UI may display;
it is not a second implementation or a product dependency. A host adapter, if
added later, invokes the same `aros start` service and owns no state.

### 5.1 Initialized workspace

When AROS.md, NOW, an attached Git branch, and a committed initialization record
exist:

1. Render a short AROS boot transition.
2. Build the bounded canonical Attention packet.
3. Display workspace, active Question, authority class, budget state, and
   blockers.
4. Start the Principal through the existing Agent/provider path.

No questionnaire is shown.

### 5.2 Uninitialized workspace

Interactive mode asks exactly:

1. `Key Research Question` — required non-empty text.
2. `Workspace` — an existing local Git root or a new directory.
3. `Local materials` — optional repeated local PDF/Markdown paths.

The existing command options continue to define `max_turns`, model/provider,
shell access, and cooperative checkpoint authority. Intake displays these
boundaries before mutation; it does not create a second contract/config layer.

Non-interactive mode supplies the same values through:

```text
--question TEXT
--cwd PATH
--material PATH     repeatable
```

If required input is absent without a TTY, the command fails with the exact
missing option. It never chooses a research question or workspace silently.

## 6. KnowledgeBank bootstrap

### 6.1 Workspace selection

- An existing path must be a clean, attached local Git root.
- A new path must have an existing parent. AROS creates the directory and runs
  `git init -b main`.
- An existing non-Git directory, nested Git path, detached HEAD, dirty index,
  staged work, symlinked root, or linked-worktree control ambiguity fails before
  any AROS file is written.
- The workspace itself is the target repository. The first version does not
  copy or nest a second repository.

### 6.2 Canonical paths

The initial commit contains:

```text
AGENTS.md
AROS.md
memory/NOW.md
memory/decisions/
questions/FRONTIER.md
questions/Q-0001/question.md
model/CURRENT.md
model/rivals/
ideas/
knowledge/claims/
sources/manifest.json
sources/papers/SRC-<sha256>/original.pdf|md
sources/papers/SRC-<sha256>/extracted.md
sources/papers/SRC-<sha256>/metadata.json
eval/
tasks/
runs/
transitions/
.gitignore
```

`.aros/` and `.worktree/` remain ignored runtime roots. Empty semantic
directories may exist locally but are not represented as fake placeholder
knowledge merely to make Git retain them.

### 6.3 Q/M/I initialization

Q-0001 contains:

- the exact user Question;
- why it is currently load-bearing: explicitly `not yet assessed`;
- empty current answer, alternatives, facts, uncertainty, and links;
- unresolved resolution and stop/pivot criteria marked for Principal review.

FRONTIER points to Q-0001. CURRENT.md states that no explanatory model has yet
been admitted. No Idea is invented by the host. NOW points to Q-0001 and lists
the source records awaiting Principal inspection.

The host creates structure and exact user facts only. It does not synthesize a
Claim, Model, Idea, answer, confidence, or expected information gain.

## 7. Local source ingestion

All materials are validated and extracted before workspace mutation.

For each source:

1. Resolve a plain, non-symlink local file.
2. Require `.pdf` or `.md` and a bounded byte size.
3. Read exact bytes once and compute SHA-256.
4. For Markdown, require strict UTF-8 and preserve the text.
5. For PDF, use pypdf and require at least one non-whitespace extracted page.
6. Derive `SRC-<first-16-sha256>` and deduplicate identical bytes.
7. Write exact original bytes, extracted text, and metadata.

Metadata contains only factual provenance:

```json
{
  "schema_version": 1,
  "source_id": "SRC-...",
  "kind": "pdf|markdown",
  "original_name": "...",
  "content_sha256": "...",
  "byte_size": 0,
  "ingested_from": "absolute local path",
  "extracted_ref": "sources/papers/.../extracted.md"
}
```

Extraction is not an EvidenceLink, Claim, or admission. The Principal must read
and assimilate relevant passages later.

## 8. Components and ownership

### 8.1 `arbor.aros.intake`

Pure product mechanics:

- validate question/workspace/materials;
- inspect existing/new Git state;
- parse local sources with pypdf/UTF-8;
- produce an immutable bootstrap write set;
- write only approved AROS paths;
- create the exact initial Git commit;
- return a factual intake receipt.

It imports no provider, Agent, Task, Eval, MCP, Skill registry, Coordinator, or
legacy intake module.

### 8.2 `arbor.cli.aros_start`

Presentation only:

- Rich AROS boot transition and boundary panel;
- Typer/prompt-toolkit questions;
- TTY/non-TTY selection;
- call the pure intake service;
- render its receipt.

It owns no workspace schema, source parser, Git mutation, provider creation, or
scientific interpretation.

### 8.3 Existing `aros_cmd.start_command`

Composition root only:

1. resolve initialized/uninitialized state;
2. invoke native intake when required;
3. build canonical Attention;
4. create provider and Principal;
5. run the Agent.

Provider/Agent creation stays exactly on the current commissioned path.

## 9. Atomicity and failure semantics

Before mutation, intake validates every material, extraction, root path, Git
state, and collision. A source error therefore leaves the workspace untouched.

Bootstrap writes only new AROS-owned paths. Any collision fails; existing
meaning is never overwritten or merged. The host stages exact created paths
and creates one initialization commit. It never runs reset, clean, rebase,
merge, or an all-files `git add`.

The commit uses a per-command system identity
`AROS Intake <aros-intake@local.invalid>` without changing repository or global
Git configuration. This identifies the mechanical bootstrap actor honestly and
does not impersonate the human or Principal.

If filesystem or Git commit publication fails after writes, the repository is
left visibly dirty and `aros start` reports initialization incomplete. It does
not delete uncertain user data or claim success. A subsequent run fails on the
collision and requires explicit human inspection; there is no recovery
fallback or migration reader.

Failure kinds remain distinct:

```text
invalid question/workspace/material
PDF extraction unavailable
Git authority/dirty-state blocked
provider configuration failure
Agent/tool/process failure
scientific uncertainty
```

Only the first four block boot. None is reinterpreted as a scientific result.

## 10. Skills and MCP boundary

Skills and MCP are deliberately absent from the first runtime slice.

Long-term Skills are project-local, versioned procedural memory under:

```text
.agents/skills/<skill>/SKILL.md
```

They are discovered and loaded on demand. They may guide source review,
researcher delegation, evaluator construction, peer criticism, or checkpoint
discipline. They do not implement a hidden Q→I scheduler or automatically
change canonical meaning.

Long-term AROS MCP is a thin transport over the same Attention, Task, Run,
Eval, source, and checkpoint services. It exposes no IdeaTree mutation API and
owns no parallel state. The existing legacy MCP server is removed when its
remaining non-AROS consumers are retired; no compatibility wrapper maps
`tree_*` calls into AROS.

## 11. Replacement and deletion

This slice keeps working lower-level services and deletes only proven-obsolete
entry paths:

- remove public `aros init`; internal workspace mechanics remain used by
  intake;
- remove `arbor aros` registration, warning, known-command entry, sunset hash,
  and related tests/docs after direct `aros start` clean-wheel commissioning;
- do not add aliases, deprecation periods, old schema readers, migration code,
  or fallback branches;
- do not delete legacy Arbor research commands until equivalent direct AROS
  product capabilities are independently commissioned.

## 12. Minimal end-to-end acceptance

One retained clean-wheel scenario must prove:

1. A new local repo is created through `aros start` with non-interactive exact
   Question/workspace/material arguments.
2. A local Markdown or PDF original, hash, extracted text, and metadata are in
   the initialization commit.
3. Q-0001 contains the exact human Question; no host-invented Claim, Model, or
   Idea exists.
4. FRONTIER, NOW, and Attention point to Q-0001 and the source observation.
5. The real native Principal Agent starts from that Attention packet.
6. The Principal reads the source through ordinary file tools.
7. Restart uses the committed KB with zero transcript messages.
8. Invalid material and dirty existing repo commissioning cases fail before
   mutation.
9. `aros init` and `arbor aros` are absent from clean-wheel help and dispatch.
10. Existing Task→Eval→Assimilation live-Agent commissioning still verifies.
11. Focused and full repository suites, Ruff, architecture gates, and
    independent receipts pass.

This acceptance establishes the native product entry and KB bootstrap. It does
not claim a resolved Question, research-quality external model, child-agent
loop, Source Gateway, Skills/MCP parity, or protected authority.

## 13. Next scientific gate

The next independent slice uses this permanent entry and workspace with one
real configured model, one local repo/paper question, one Principal-authored
Model and Idea, one bounded researcher Task, one real Eval/MeasurementReceipt,
one explicit assimilation, and one fresh restart. Async Idea production is
added only after that single-child scientific loop is commissioned.
