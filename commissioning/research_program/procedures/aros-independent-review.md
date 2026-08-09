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
  - Research.petition
---

## Purpose

Independently reproduce and attack one coherent frozen evidence lineage in a
provably fresh, cross-family Reviewer context without changing the candidate or
deciding the Claim.

## Inputs

- Artifact: Read exactly one `FrozenEvidencePacket`.
- Required fields: `task_brief_ref`, `preregistration_ref`, `candidate_commit`,
  `source_refs`, `raw_refs`, `reproduction_ref`, `root_question_ref`,
  `claim_draft_ref`, `researcher_model_id`, `researcher_model_family`,
  `researcher_session_refs`, `researcher_worktree_ref`, `packet_sha256`,
  `review_session_receipt_ref`.
- Context isolation: Start a fresh Reviewer model with empty message history, no
  Researcher transcript, no prior review conversation, and only the frozen packet
  as initial context.
- Read-only boundary: Treat the packet, exact `candidate_commit`, Claim draft,
  preregistration, sources, raw evidence, reproduction package, Researcher session
  references, and Researcher worktree reference as immutable.

## Method

1. Require `packet_sha256` to equal SHA-256 of the UTF-8 bytes of canonical JSON for
   the `FrozenEvidencePacket` excluding `packet_sha256`, encoded with
   `sort_keys=True`, `separators=(",", ":")`, and `ensure_ascii=False`; compute and
   compare it before dereferencing or reviewing any packet content.
2. Use `Receipt.read` to read exact `review_session_receipt_ref` before any Reviewer
   analysis; require it to be immutable, host-issued before the Reviewer started,
   and to contain `reviewer_model_id`, `reviewer_model_family`,
   `researcher_model_id`, `researcher_model_family`, `initial_message_count`,
   `supplied_packet_sha256`, `created_at`, and `issuer`.
3. Read the exact TaskBrief, root question, Claim draft, preregistration, candidate
   commit, sources, raw evidence, and reproduction package named by the packet;
   require one coherent question, Claim, candidate, preregistration, source, raw, and
   reproduction lineage across every exact reference.
4. Read and bind `researcher_model_id`, `researcher_model_family`, every
   `researcher_session_refs` entry, and `researcher_worktree_ref`; reject a missing,
   unknown, unverifiable, cross-lineage, or review-receipt-mismatched Researcher
   identity or execution reference.
5. Require the review session receipt `supplied_packet_sha256` to equal input
   `packet_sha256`, `initial_message_count` to be the plain integer 0, its Reviewer
   identity to equal the active Reviewer, its Researcher identity to equal the packet,
   and both model families to be known and different.
6. Set `independence_evidence_ref` exactly to input `review_session_receipt_ref`; bind
   its exact Reviewer and Researcher identities to every new Reviewer Run and Eval
   receipt, and do not self-issue or replace the host receipt.
7. Independently rebuild the exact `candidate_commit` in a clean isolated Reviewer
   Run without editing the candidate or using `researcher_worktree_ref` or any
   Researcher-built workspace.
8. Independently rerun every primary evidence path from the exact
   `reproduction_ref` through a new Reviewer `Run.request`, follow each with
   `Run.status`, and never reuse a candidate, Researcher, or prior Reviewer Run as
   the independent reproduction.
9. Run the specified evaluations through new Reviewer `Eval.run` calls and compare
   both raw outputs and parsed results with the frozen raw evidence; record every
   match, mismatch, and unavailable comparison.
10. Record exact packet, Claim-draft, candidate, source, raw-evidence, reproduction,
   Run, Eval, and receipt references in `reproduction_refs`, together with exact
   Researcher and Reviewer identities and `independence_evidence_ref`.
11. Develop and test plausible alternative explanations, and attempt at least one
    counterexample that could distinguish the reported mechanism from those
    alternatives.
12. Audit leakage and contamination, warmup and startup effects, data identity and
    partitioning, cache isolation and cache state, statistical assumptions and
    uncertainty, hard-constraint compliance, and claimed scope.
13. Place every material issue in the corresponding report field, mark objections
    that defeat the Claim as `fatal_objections`, retain all other open issues in
    `unresolved_objections`, and do not convert the report into a canonical verdict.

## Output

- Artifact: Return exactly one `ReviewerReport`.
- Required fields: `reproduction_refs`, `alternative_explanations`,
  `leakage_findings`, `statistical_findings`, `scope_objections`, `fatal_objections`,
  `unresolved_objections`, `reviewer_model_id`, `reviewer_model_family`,
  `independence_evidence_ref`, `packet_sha256`, `claim_draft_ref`,
  `candidate_commit`.
- Independence binding: Set `reviewer_model_id` and `reviewer_model_family` exactly
  from the host-issued review session receipt, and set `independence_evidence_ref`
  exactly to input `review_session_receipt_ref`.
- Lineage binding: Copy input `packet_sha256`, `claim_draft_ref`, and
  `candidate_commit` unchanged into the report.
- Reproduction binding: In `reproduction_refs`, bind exact candidate, source,
  raw-evidence, reproduction, Reviewer Run, Eval, and receipt references with the
  exact Researcher identity, Reviewer identity, and independence evidence.
- Objection mapping: Put alternative explanations, leakage findings, statistical
  findings, scope objections, fatal objections, and unresolved objections only in
  their corresponding required fields; retain empty fields explicitly.

## Completion

- Complete only after the exact canonical packet hash, host-issued review session
  receipt, zero-message fresh context, and one coherent lineage are verified,
  cross-family independence is proven, every frozen reference is read, every primary
  evidence path has an independent Reviewer reproduction attempt, every required
  audit is performed, and all `ReviewerReport` fields are populated.
- If the packet hash fails, any required reference is missing, or question, Claim,
  candidate, preregistration, source, raw, reproduction, identity, session, or
  worktree references are spliced across lineages, do not emit a `ReviewerReport`;
  call `Research.petition` with the exact lineage blocker and exit incomplete.
- If `review_session_receipt_ref` is missing or unreadable, the receipt is not
  host-issued before Reviewer start, `supplied_packet_sha256` mismatches,
  `initial_message_count` is not the plain integer 0, either identity mismatches, the
  families are equal, unknown, or unverifiable, or `independence_evidence_ref` cannot
  equal that receipt, do not emit a `ReviewerReport`; call `Research.petition` with
  the exact independence blocker and exit incomplete.
- If Reviewer transport is unavailable, a required new Reviewer Run or Eval cannot
  be requested or observed, or the fresh model identity cannot be bound, do not emit
  a `ReviewerReport`; call `Research.petition` with the exact transport blocker and
  exit incomplete, which blocks Claim admission.
- A completed reproduction that disagrees with the candidate is scientific
  counterevidence, not transport failure; emit it in the report and mark a fatal
  objection when it defeats the Claim.
- Return the completed objection report and exit the fresh Reviewer session without
  admitting or editing a Claim.

## Forbidden

- Do not read or request a Researcher transcript, reuse message history, continue a
  Researcher session, or substitute self-review for the fresh independent review.
- Do not edit the candidate, exact `candidate_commit`, frozen packet, Claim draft,
  preregistration, evidence, or reproduction package.
- Do not admit, accept, narrow, or reject a Claim, and do not present the report as a
  canonical scientific verdict.
- Do not reuse a candidate, Researcher, or prior review Run as an independent
  Reviewer reproduction.
- Do not claim independence from different model identifiers alone, accept the same
  or an unknown model family, fabricate `independence_evidence_ref`, or accept spliced
  packet references.
- Do not issue, synthesize, alter, or substitute `review_session_receipt_ref`; it must
  be the pre-existing host receipt read with `Receipt.read`, and output
  `independence_evidence_ref` must equal it.
- Do not use a shell, subprocess, SSH, direct remote execution, job queue, upload, or
  notification service; experimental work must use the declared AROS Run and Eval
  tools.
- Do not use a score, ranking, pass threshold, or aggregate quality number to replace
  evidence or objections.
- Do not produce a paper, rebuttal, poster, slide deck, publication, or submission.
