---
name: aros-claim-package
source_ids:
  - source-1
  - source-2
input: AdjudicatedEvidence
output: ClaimPackage
tools:
  - Source.read
  - Receipt.read
  - Git.read
  - Research.checkpoint
---

## Purpose

Package a verified Principal adjudication with exact scientific lineage,
counterevidence, reproduction, uncertainty, and review objections without
performing admission or laundering rejection into evidence.

## Inputs

- Artifact: Read exactly one `AdjudicatedEvidence`.
- Required fields: `claim_draft_ref`, `evidence_refs`, `review_ref`,
  `principal_response_ref`, `root_question_ref`, `candidate_commit`,
  `preregistration_ref`, `reproduction_ref`, `principal_actor_ref`,
  `principal_checkpoint_ref`, `disposition`.
- Exact reads: Read the exact Claim draft, every evidence reference, Reviewer report,
  and Principal response before constructing the package; bind every value to its
  immutable reference.
- Authority: Treat only the referenced Principal actor, Principal checkpoint, and
  Principal response as adjudication; do not infer actor authority, acceptance,
  narrowing, rejection, or objection resolution.

## Method

1. Read the exact Claim draft, every evidence reference, Reviewer report, Principal
   response, Principal actor record, Principal checkpoint, preregistration, and
   reproduction package before packaging.
2. Require `root_question_ref`, `candidate_commit`, `preregistration_ref`,
   `reproduction_ref`, `claim_draft_ref`, evidence lineage, and review lineage to
   match exactly across every input and every referenced artifact; reject missing
   or cross-lineage references.
3. Verify that `principal_actor_ref` identifies the canonical Principal actor and
   that `principal_checkpoint_ref` is an immutable successful checkpoint by that same
   actor binding the exact Principal response, lineage, and disposition.
4. Require input `disposition` and the Principal response disposition to match and
   to equal exactly one of `accept`, `narrow`, or `reject`; copy the disposition,
   rationale, and evidence references without changing them.
5. Enumerate every material Reviewer objection, including every fatal and unresolved
   objection, and map it one-to-one to an explicit Principal response.
6. Require the Principal response to answer every material objection with exactly
   one disposition: `accept`, `narrow`, or `reject`; copy each answer, rationale,
   evidence reference, and scope effect without changing them.
7. For disposition `accept` or `narrow`, construct `claim` and `scope` only from the
   adjudicated wording and boundaries; never broaden the admitted result or repair
   it with new policy.
8. For disposition `reject`, construct only a rejected adjudication package that
   records the rejected Claim draft and reasons; do not describe it as an admitted,
   supported, or scientific negative Claim.
9. Describe a scientific negative Claim only when executed evidence explicitly
   records a negative result and the Principal disposition `accept` or `narrow`
   admits that scoped negative Claim; rejection alone is never scientific evidence.
10. Construct `evidence_refs` and `counterevidence` from the exact adjudicated evidence
    and review references, preserving contrary observations, counterexamples, and
    executed negative results.
11. Copy exact, bounded reproduction commands from `reproduction_ref` into
    `reproduction_commands`, derive `environment_ref` from matching preregistration,
    reproduction, and evidence receipts, and do not invent or execute a command.
12. State limitations and `remaining_uncertainty` at the adjudicated scope, including
    unresolved nonfatal objections and evidence that could change the conclusion.
13. Populate `review_objections` with every material objection, its exact
    `review_ref`, its Principal disposition and `principal_response_ref`, and the
    resulting scope effect.

## Output

- Artifact: Return exactly one `ClaimPackage`.
- Required fields: `claim`, `scope`, `evidence_refs`, `counterevidence`,
  `reproduction_commands`, `limitations`, `remaining_uncertainty`,
  `review_objections`, `disposition`, `root_question_ref`, `candidate_commit`,
  `preregistration_ref`, `review_ref`, `principal_response_ref`, `reproduction_ref`,
  `environment_ref`, `checkpoint_ref`.
- Disposition authority: Copy input `disposition` exactly; `accept` and `narrow` may
  represent only the scoped Claim admitted by the verified Principal response, while
  `reject` represents only a rejected adjudication record.
- Lineage binding: Copy `root_question_ref`, `candidate_commit`,
  `preregistration_ref`, `review_ref`, `principal_response_ref`, and
  `reproduction_ref` exactly from the verified input lineage.
- Environment and checkpoint binding: Set `environment_ref` to the exact matching
  environment receipt and `checkpoint_ref` exactly to verified input
  `principal_checkpoint_ref`.
- Evidence binding: Bind every evidence and counterevidence entry, limitation,
  uncertainty, reproduction command, and objection to its exact input reference.
- Review traceability: Include every material review objection and its explicit
  Principal disposition without omission, repair, or reinterpretation.

## Completion

- Complete only after every exact input reference and cross-artifact lineage is
  verified, Principal actor and checkpoint authority are proven, every material
  objection has an explicit Principal disposition, no fatal objection remains
  unresolved, and all `ClaimPackage` fields are populated.
- If any required reference is missing or cross-lineage, Principal actor authority or
  checkpoint binding is absent or forged, the Principal response is missing, any
  material objection is unanswered, or any fatal objection remains unresolved, do not
  emit or checkpoint a `ClaimPackage`; return incomplete for Principal adjudication.
- For disposition `accept` or `narrow`, emit only the scoped admitted Claim that the
  verified Principal response authorizes.
- For disposition `reject`, emit a rejected adjudication package and set
  `disposition` to `reject`; do not emit an admitted or supported Claim and do not
  convert rejection into a scientific negative result.
- Only after adjudication, call `Research.checkpoint` with the complete package,
  exact lineage and evidence references, `review_ref`, `principal_response_ref`, and
  `checkpoint_ref` set exactly to verified `principal_checkpoint_ref`.
- Exit immediately after the successful checkpoint; do not continue the research
  session.

## Forbidden

- Do not admit, accept, narrow, or reject a Claim; only the Principal may perform Claim
  admission or adjudication.
- Do not repair policy, invent a Principal disposition, resolve an objection, or alter
  adjudicated wording or scope.
- Do not trust a claimed Principal role without `principal_actor_ref`, accept a
  missing or forged `principal_checkpoint_ref`, or combine references from different
  questions, candidates, preregistrations, reproductions, reviews, or responses.
- Do not launder disposition `reject` into an admitted, supported, or scientific
  negative Claim; rejection is an adjudication outcome, not scientific evidence.
- Do not call a result scientifically negative unless exact executed evidence records
  the negative result and the verified Principal response admits that scoped negative
  Claim with disposition `accept` or `narrow`.
- Do not perform new science, create evidence, search for supporting evidence, or
  reinterpret the exact evidence beyond the Principal response.
- Do not run or request an experiment or evaluation, and do not use a shell,
  subprocess, SSH, direct remote execution, job queue, upload, or notification service.
- Do not omit counterevidence, a negative result, limitation, remaining uncertainty,
  or material Reviewer objection.
- Do not use a score, ranking, pass threshold, or aggregate quality number as an
  admission rule or package field.
- Do not produce a paper, rebuttal, poster, slide deck, publication, or submission.
