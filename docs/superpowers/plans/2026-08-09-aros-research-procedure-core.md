# AROS Research Procedure Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the commissioning-only source record, contracts, six AROS research procedures, and strict validator for Wave 1 of the autonomous Researcher integration.

**Architecture:** Record upstream provenance once in `SOURCES.json`; procedures contain only opaque source ids and AROS-native instructions. A standard-library validator binds sources, contracts, allowed tools, required sections, naming, and forbidden behavior without judging scientific quality.

**Tech Stack:** Markdown Agent Skills, strict JSON, Python 3.10 standard library, pytest, Git.

---

## Scope

Wave 1 does not implement adapters, providers, runtime Researcher/Reviewer
Skills, event drivers, or `src/aros` product code. Completion requires exactly
six `aros-*` procedures, one source record, one contract record, no duplicate
state machine, and all repository gates passing.

## Files

- `commissioning/research_program/__init__.py`
- `commissioning/research_program/SOURCES.json`
- `commissioning/research_program/contracts/procedure_contracts.json`
- `commissioning/research_program/validate.py`
- six Markdown files under `commissioning/research_program/procedures/`
- `tests/test_aros_research_procedure_core.py`
- `tests/test_aros_architecture_boundary.py`

### Task 1: Bind both procedure sources once

**Files:** Create `__init__.py`, `SOURCES.json`; create the focused test file.

- [ ] **Step 1: Write failing strict-source tests**

```python
def test_sources_are_recorded_once() -> None:
    value = load_strict_json(PROGRAM / "SOURCES.json")
    assert value["schema_version"] == 1
    assert [item["id"] for item in value["sources"]] == ["source-1", "source-2"]
    assert all(set(item) == {
        "id", "repository", "commit", "license", "selected_paths", "adaptation"
    } for item in value["sources"])


def test_every_selected_source_path_is_bound() -> None:
    for item in load_sources()["sources"]:
        for relative in item["selected_paths"]:
            git(Path(item["repository"]), "cat-file", "-e", f"{item['commit']}:{relative}")
```

Reject duplicate JSON keys, unknown fields, duplicate paths, non-absolute local
repositories, invalid commits, missing license, and another source/provenance
file anywhere in `commissioning/research_program`.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py -k source`

Expected: FAIL because the program package and source record are absent.

- [ ] **Step 3: Add the exact source record**

Set `SCHEMA_VERSION = 1` in `__init__.py`. `SOURCES.json` contains two entries:

```json
{
  "schema_version": 1,
  "sources": [
    {
      "id": "source-1",
      "repository": "/workspace/Auto-claude-code-research-in-sleep",
      "commit": "df729a3f942e4a97646d212eb8aee1144ab5e31b",
      "license": "MIT",
      "selected_paths": [
        "skills/research-lit/SKILL.md",
        "skills/novelty-check/SKILL.md",
        "skills/citation-audit/SKILL.md",
        "skills/idea-creator/SKILL.md",
        "skills/research-refine/SKILL.md",
        "skills/experiment-plan/SKILL.md",
        "skills/ablation-planner/SKILL.md",
        "skills/analyze-results/SKILL.md",
        "skills/research-wiki/SKILL.md",
        "skills/research-review/SKILL.md",
        "skills/experiment-audit/SKILL.md",
        "skills/integrity-forensics/SKILL.md",
        "skills/result-to-claim/SKILL.md",
        "skills/claims-drafting/SKILL.md",
        "skills/shared-references/external-cadence.md",
        "skills/shared-references/reviewer-independence.md",
        "mcp-servers/claude-review/server.py",
        "mcp-servers/gemini-review/server.py",
        "mcp-servers/manual-review/server.py"
      ],
      "adaptation": "Distill scientific procedures, durable recovery, cadence, and fresh review; remove scoring, paper production, remote execution, and duplicate orchestration."
    },
    {
      "id": "source-2",
      "repository": "/workspace/Arbor/.worktree/aros-long-running-research-program-design",
      "commit": "e9c58c998767dd87bdea99a727533819850ac281",
      "license": "Apache-2.0",
      "selected_paths": [
        "skills/arbor-agent-setup-intake/SKILL.md",
        "skills/arbor-agent-ideate/SKILL.md",
        "skills/arbor-agent-executor/SKILL.md",
        "skills/arbor-agent-search/SKILL.md",
        "skills/arbor-agent-resume-report/SKILL.md",
        "skills/arbor-agent-tools/SKILL.md",
        "src/mcp/server.py",
        "src/mcp/session_ops.py"
      ],
      "adaptation": "Distill mechanism framing, deterministic tool boundaries, search, and durable handoff; remove tree authority, scalar evaluation, merge gates, and duplicate session state."
    }
  ]
}
```

- [ ] **Step 4: GREEN and commit**

```bash
.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py -k source
git diff --check
git add commissioning/research_program tests/test_aros_research_procedure_core.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' commit -m 'test(aros): bind research procedure sources'
```

### Task 2: Define canonical contracts

**Files:** Create `contracts/procedure_contracts.json`, `validate.py`; modify focused tests.

- [ ] **Step 1: Write failing contract tests**

```python
EXPECTED = {
    "aros-source-research": ("ResearchQuestion", "SourcePacket"),
    "aros-rival-mechanisms": ("SourcePacket", "RivalMechanismSet"),
    "aros-experiment-design": ("RivalMechanismSet", "ExperimentProposal"),
    "aros-evidence-update": ("RunEvidence", "ObservationUpdate"),
    "aros-independent-review": ("FrozenEvidencePacket", "ReviewerReport"),
    "aros-claim-package": ("AdjudicatedEvidence", "ClaimPackage"),
}
```

Require exact procedure set, exact input/output, known tools, exact artifact
required fields, duplicate-key rejection, finite JSON, and no score/ranking/pass
acceptance fields.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py -k contract`

- [ ] **Step 3: Add contracts and loader**

The contract file has exact top-level keys `schema_version`, `allowed_tools`,
`artifacts`, and `procedures`. Allowed tools are:

```json
["Source.read","Source.search","Task.create","Task.start","Task.status","Task.collect","Run.request","Run.status","Eval.run","Receipt.read","Research.observe","Research.checkpoint","Research.petition","Git.read"]
```

Artifact required fields are exactly those in Design §11. Procedure tools are:

```json
{
  "aros-source-research": ["Source.read", "Source.search"],
  "aros-rival-mechanisms": ["Git.read", "Receipt.read", "Research.observe"],
  "aros-experiment-design": ["Receipt.read", "Research.observe", "Research.petition"],
  "aros-evidence-update": ["Run.status", "Eval.run", "Receipt.read", "Research.observe", "Research.checkpoint"],
  "aros-independent-review": ["Source.read", "Run.request", "Run.status", "Eval.run", "Receipt.read", "Git.read"],
  "aros-claim-package": ["Source.read", "Receipt.read", "Git.read", "Research.checkpoint"]
}
```

`validate.py` implements strict JSON loading plus frozen `ProcedureContract` and
`ContractSet`. It imports neither source repository nor production AROS.

- [ ] **Step 4: GREEN and commit**

```bash
.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py -k contract
.venv/bin/ruff check commissioning/research_program/validate.py tests/test_aros_research_procedure_core.py
git diff --check
git add commissioning/research_program/contracts commissioning/research_program/validate.py tests/test_aros_research_procedure_core.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' commit -m 'test(aros): define research procedure contracts'
```

### Task 3: Add source and rival procedures

**Files:** Create `procedures/aros-source-research.md` and `aros-rival-mechanisms.md`; modify tests.

- [ ] **Step 1: Write failing procedure tests**

Require exact frontmatter keys `name`, `source_ids`, `input`, `output`, `tools`;
exact contract values; headings `Purpose`, `Inputs`, `Method`, `Output`,
`Completion`, `Forbidden` exactly once.

- [ ] **Step 2: Write the procedures**

`aros-source-research` requires primary-source preference, multi-source queries,
dead-end retention, bound `SourcePacket`, and explicit limitations. It forbids
experimental-data download, external write, citation fabrication, and verdicts.

`aros-rival-mechanisms` requires at least two independent falsifiable mechanisms,
mechanism compression before novelty/impact, predictions, falsifiers, conflicts,
scope, and a discriminating observation. It forbids pilot-score ranking and
winner selection.

- [ ] **Step 3: Verify and commit**

```bash
.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py -k 'source_procedure or rival'
git diff --check
git add commissioning/research_program/procedures tests/test_aros_research_procedure_core.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' commit -m 'feat(aros): add source and rival procedures'
```

### Task 4: Add experiment and evidence procedures

**Files:** Create `aros-experiment-design.md`, `aros-evidence-update.md`; modify tests.

- [ ] **Step 1: Write RED semantic-rule tests**

Require the lexicographic essentiality/falsifiability/decision-relevance gate,
information-gain-per-cost ordering, proposal-only execution, unavailable versus
negative evidence, cited adaptive observations, negative-result preservation,
budget accounting, checkpoint, and exit.

- [ ] **Step 2: Write both procedures**

The design procedure outputs an `ExperimentProposal` and can only request AROS
Run/Eval later. The update procedure reads immutable receipts, updates rivals
and uncertainty, writes its decision rationale, checkpoints, and exits.

- [ ] **Step 3: Verify and commit**

```bash
.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py -k 'experiment_design or evidence_update'
git diff --check
git add commissioning/research_program/procedures tests/test_aros_research_procedure_core.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' commit -m 'feat(aros): add experiment and evidence procedures'
```

### Task 5: Add review and Claim procedures

**Files:** Create `aros-independent-review.md`, `aros-claim-package.md`; modify tests.

- [ ] **Step 1: Write RED independence/admission tests**

Require empty history, no transcript, frozen read-only packet, independent
reproduction, alternative explanations, leakage/statistical/scope objections,
and no candidate edit. Require evidence, counterevidence, reproduction,
limitations, uncertainty, objections, and Principal-only admission.

- [ ] **Step 2: Write both procedures and verify**

```bash
.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py -k 'independent_review or claim_package'
git diff --check
git add commissioning/research_program/procedures tests/test_aros_research_procedure_core.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' commit -m 'feat(aros): add review and claim procedures'
```

### Task 6: Static validation and final Wave 1 gates

**Files:** Modify `validate.py`, focused tests, and `tests/test_aros_architecture_boundary.py`.

- [ ] **Step 1: Write failing integration tests**

Reject non-`aros-` names, source detail outside `SOURCES.json`, unknown tools,
direct shell/remote/upload/notification/publication/merge/scheduler authority,
score thresholds, fixed rounds, auto winner selection, missing/duplicate
headings, symlinks, non-UTF-8, files over 128 KiB, unknown procedure files, and
any Wave 1 diff beneath `src/aros` including deletion.

- [ ] **Step 2: Implement `validate_program(root)`**

Return a canonical factual object with source ids/commits, contract hash,
procedure names/hashes/tools, and `state="valid"`. Never emit a scientific
score or quality verdict.

- [ ] **Step 3: Run all gates**

```bash
.venv/bin/python -m pytest -q tests/test_aros_research_procedure_core.py tests/test_aros_architecture_boundary.py tests/test_document_registry.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check commissioning/research_program tests/test_aros_research_procedure_core.py tests/test_aros_architecture_boundary.py
.venv/bin/python -m py_compile commissioning/research_program/*.py
git diff --check
git status --short
```

Expected: all exit 0 and no `src/aros` path changes.

- [ ] **Step 4: Commit**

```bash
git add commissioning/research_program tests/test_aros_research_procedure_core.py tests/test_aros_architecture_boundary.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' commit -m 'test(aros): validate research procedure core'
```

## Deferred plans

After Wave 1 independent review, create separate implementation plans for AROS
adapters, AROS Researcher/Reviewer Skills, and the event-driven E2E driver.
