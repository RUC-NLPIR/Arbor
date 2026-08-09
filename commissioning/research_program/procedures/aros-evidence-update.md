---
name: aros-evidence-update
source_ids:
  - source-1
  - source-2
input: RunEvidence
output: ObservationUpdate
tools:
  - Run.status
  - Eval.run
  - Receipt.read
  - Research.observe
  - Research.checkpoint
---

## Purpose

Turn immutable run, evaluation, and raw evidence into a durable scientific update
without changing the evidence or continuing the experimental session.

## Inputs

- Artifact: Read exactly one `RunEvidence`.
- Required fields: `run_ref`, `eval_refs`, `raw_refs`, `process_state`,
  `budget_used`.
- Immutability: Treat `run_ref`, every `eval_ref`, and every `raw_ref` as an
  immutable exact reference.

## Method

1. Read the exact immutable receipts identified by `run_ref` and `eval_refs` and
   the exact immutable raw references identified by `raw_refs`; bind each read to
   its reference before interpretation.
2. Classify each evidence item as exactly one of: operationally unavailable
   because of process or transport failure or a missing measurement; an executed
   negative result; or a counterexample to a stated prediction.
3. Never classify operational unavailability, process failure, transport failure,
   or a missing measurement as a negative scientific result.
4. Update `strengthened`, `weakened`, and `eliminated` only from classified
   evidence, and attach the exact supporting evidence reference to every update.
5. Preserve every negative result, counterexample, preregistration deviation,
   protocol deviation, and unexpected condition with its exact evidence reference.
6. If the next action changes, cite the specific prior observation that caused the
   change in `next_action_rationale`; otherwise state why the current action
   remains justified.
7. Record the most important remaining uncertainty, the next-action rationale, and
   the exact input `budget_used` without discarding achieved evidence.

## Output

- Artifact: Return exactly one `ObservationUpdate`.
- Required fields: `evidence_refs`, `strengthened`, `weakened`, `eliminated`,
  `counterexamples`, `negative_results`, `remaining_uncertainty`,
  `next_action_rationale`.
- Evidence binding: Cite an exact input receipt or raw reference for every
  strengthened, weakened, eliminated, counterexample, and negative-result entry.
- Budget accounting: Include the exact input `budget_used` in
  `next_action_rationale`; do not add an output field outside the central contract.

## Completion

- Complete only after every declared receipt and raw reference has been read,
  classified, preserved, and bound into one `ObservationUpdate`.
- Call `Research.observe` with the complete `ObservationUpdate`.
- Call `Research.checkpoint` with the observation update and its exact immutable
  evidence references.
- Exit immediately after the successful checkpoint; do not continue the research
  session.

## Forbidden

- Do not treat an unavailable process, transport, evaluator, or measurement as a
  negative scientific result.
- Do not request, retry, restart, rerun, or resubmit an experiment, and do not call
  `Run.request`.
- Do not overwrite or mutate a receipt, raw reference, prior observation, negative
  result, counterexample, or deviation.
- Do not use a shell, subprocess, SSH, remote execution, job queue, upload, or
  notification service.
- Do not use a score, score threshold, fixed-round rule, or preference for the top
  positive result to interpret evidence or decide whether to stop.
- Do not admit, accept, or reject a `Claim`; Claim admission is outside this
  procedure.
