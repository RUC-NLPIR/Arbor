---
name: aros-independent-review
source_ids:
  - source-1
  - source-2
input: FrozenEvidencePacket
output: ReviewerReport
tools:
  - Source.read
  - Run.request
  - Run.status
  - Eval.run
  - Receipt.read
  - Git.read
---

## Purpose

Independently reproduce and attack frozen evidence in a fresh Reviewer context,
then return traceable objections without changing the candidate or deciding the
Claim.

## Inputs

- Artifact: Read exactly one `FrozenEvidencePacket`.
- Required fields: `task_brief_ref`, `preregistration_ref`, `commit`,
  `source_refs`, `raw_refs`, `reproduction_ref`.
- Context isolation: Start a fresh Reviewer model with empty message history, no
  Researcher transcript, no prior review conversation, and only the frozen packet
  as initial context.
- Read-only boundary: Treat the packet, exact candidate `commit`, Claim,
  preregistration, sources, raw evidence, and reproduction package as immutable.

## Method

1. Read the exact TaskBrief, preregistration, candidate commit, source references,
   raw evidence, and reproduction package named by the packet; bind every read to
   its exact reference before review.
2. Record the fresh Reviewer model provider, family, version, and model identifier
   before reproduction, and bind that identity to every new Reviewer Run and Eval
   receipt.
3. Independently rebuild the exact candidate commit in a clean isolated Reviewer
   Run without editing the candidate or using a Researcher-built workspace.
4. Independently rerun every primary evidence path from the reproduction package
   through a new Reviewer `Run.request`, follow each with `Run.status`, and never
   reuse a candidate or Researcher Run as the independent reproduction.
5. Run the specified evaluations through new Reviewer `Eval.run` calls and compare
   both raw outputs and parsed results with the frozen raw evidence; record every
   match, mismatch, and unavailable comparison.
6. Record exact candidate, source, raw-evidence, reproduction, Run, Eval, and receipt
   references in `reproduction_refs`, together with the exact Reviewer model
   identity.
7. Develop and test plausible alternative explanations, and attempt at least one
   counterexample that could distinguish the reported mechanism from those
   alternatives.
8. Audit leakage and contamination, warmup and startup effects, data identity and
   partitioning, cache isolation and cache state, statistical assumptions and
   uncertainty, hard-constraint compliance, and claimed scope.
9. Place every material issue in the corresponding report field, mark objections
   that defeat the claim as `fatal_objections`, retain all other open issues in
   `unresolved_objections`, and do not convert the report into a canonical verdict.

## Output

- Artifact: Return exactly one `ReviewerReport`.
- Required fields: `reproduction_refs`, `alternative_explanations`,
  `leakage_findings`, `statistical_findings`, `scope_objections`, `fatal_objections`,
  `unresolved_objections`.
- Reproduction binding: In `reproduction_refs`, bind exact candidate, source,
  raw-evidence, reproduction, Reviewer Run, Eval, and receipt references with the
  exact Reviewer model provider, family, version, and model identifier.
- Objection mapping: Put alternative explanations, leakage findings, statistical
  findings, scope objections, fatal objections, and unresolved objections only in
  their corresponding required fields; retain empty fields explicitly.

## Completion

- Complete only after every frozen reference is read, every primary evidence path
  has an independent Reviewer reproduction attempt, every required audit is
  performed, and all `ReviewerReport` fields are populated.
- If Reviewer transport is unavailable, a required new Reviewer Run or Eval cannot
  be requested or observed, or the fresh model identity cannot be bound, do not emit
  a `ReviewerReport`; report transport unavailability to the caller, which blocks
  Claim admission.
- A completed reproduction that disagrees with the candidate is scientific
  counterevidence, not transport failure; emit it in the report and mark a fatal
  objection when it defeats the Claim.
- Return the completed objection report and exit the fresh Reviewer session without
  admitting or editing a Claim.

## Forbidden

- Do not read or request a Researcher transcript, reuse message history, continue a
  Researcher session, or substitute self-review for the fresh independent review.
- Do not edit the candidate, exact commit, frozen packet, preregistration, evidence,
  reproduction package, or Claim.
- Do not admit, accept, narrow, or reject a Claim, and do not present the report as a
  canonical scientific verdict.
- Do not reuse a candidate, Researcher, or prior review Run as an independent
  Reviewer reproduction.
- Do not use a shell, subprocess, SSH, direct remote execution, job queue, upload, or
  notification service; experimental work must use the declared AROS Run and Eval
  tools.
- Do not use a score, ranking, pass threshold, or aggregate quality number to replace
  evidence or objections.
- Do not produce a paper, rebuttal, poster, slide deck, publication, or submission.
