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

Package a Principal-adjudicated scientific result with its exact evidence,
counterevidence, reproduction path, uncertainty, and review objections without
performing admission or new science.

## Inputs

- Artifact: Read exactly one `AdjudicatedEvidence`.
- Required fields: `claim_draft_ref`, `evidence_refs`, `review_ref`,
  `principal_response_ref`.
- Exact reads: Read the exact Claim draft, every evidence reference, Reviewer report,
  and Principal response before constructing the package; bind every value to its
  immutable reference.
- Authority: Treat only the referenced Principal response as adjudication; do not
  infer acceptance, narrowing, rejection, or objection resolution.

## Method

1. Verify that the Claim draft, evidence, review, and Principal response refer to the
   same question, candidate, evidence lineage, and review before packaging.
2. Enumerate every material Reviewer objection, including every fatal and unresolved
   objection, and map it one-to-one to an explicit Principal response.
3. Require the Principal response to answer every material objection with exactly one
   disposition: `accept`, `narrow`, or `reject`; copy the disposition, rationale, and
   evidence references without changing them.
4. Construct `claim` and `scope` only from the adjudicated wording and boundaries;
   never broaden the Principal-adjudicated result or repair it with new policy.
5. Construct `evidence_refs` and `counterevidence` from the exact adjudicated evidence
   and review references, preserving contrary observations, counterexamples, and
   negative results.
6. Copy exact, bounded reproduction commands from the adjudicated reproduction package
   into `reproduction_commands`; do not invent or execute a command.
7. State limitations and `remaining_uncertainty` at the adjudicated scope, including
   unresolved nonfatal objections and evidence that could change the conclusion.
8. Populate `review_objections` with every material objection, its exact review
   reference, its Principal disposition and response reference, and the resulting
   scope effect.
9. Preserve a Principal-adjudicated negative or rejected result as a valid negative
   Claim package; never force a positive Claim or omit it because the result is
   negative.

## Output

- Artifact: Return exactly one `ClaimPackage`.
- Required fields: `claim`, `scope`, `evidence_refs`, `counterevidence`,
  `reproduction_commands`, `limitations`, `remaining_uncertainty`,
  `review_objections`.
- Scope authority: Make `claim` and `scope` exactly reflect the Principal response;
  this procedure packages adjudication and does not perform admission.
- Evidence binding: Bind every evidence and counterevidence entry, limitation,
  uncertainty, reproduction command, and objection to its exact input reference.
- Review traceability: Include every material review objection and its explicit
  Principal disposition without omission, repair, or reinterpretation.

## Completion

- Complete only after every exact input reference is read, every material objection
  has an explicit Principal disposition, no fatal objection remains unresolved, and
  all `ClaimPackage` fields are populated.
- If the Principal response is missing, any material objection is unanswered, or any
  fatal objection remains unresolved, do not emit or checkpoint a `ClaimPackage`;
  return incomplete for Principal adjudication.
- Only after adjudication, call `Research.checkpoint` with the complete package, the
  exact evidence and review references, and the exact Principal response reference.
- Exit immediately after the successful checkpoint; do not continue the research
  session.

## Forbidden

- Do not admit, accept, narrow, or reject a Claim; only the Principal may perform Claim
  admission or adjudication.
- Do not repair policy, invent a Principal disposition, resolve an objection, or alter
  adjudicated wording or scope.
- Do not perform new science, create evidence, search for supporting evidence, or
  reinterpret the exact evidence beyond the Principal response.
- Do not run or request an experiment or evaluation, and do not use a shell,
  subprocess, SSH, direct remote execution, job queue, upload, or notification service.
- Do not omit counterevidence, a negative result, limitation, remaining uncertainty,
  or material Reviewer objection.
- Do not use a score, ranking, pass threshold, or aggregate quality number as an
  admission rule or package field.
- Do not produce a paper, rebuttal, poster, slide deck, publication, or submission.
