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
2. For every evidence item, record exactly one `operational_state`: `unavailable`,
   `failed`, or `executed`; determine it only from the exact process, transport,
   evaluator, and measurement receipts.
3. Only an item whose `operational_state` is `executed` may carry
   `scientific_relations`; assign zero or more of `supports`, `weakens`,
   `eliminates`, `counterexample`, and `negative_result` from the scientific
   evidence.
4. Allow one executed item to carry both `negative_result` and `counterexample`
   when both apply; scientific relations are not mutually exclusive.
5. An item whose `operational_state` is `unavailable` or `failed` must have empty
   `scientific_relations` and must never be recorded as a `negative_result`.
6. Map `supports` to `strengthened`, `weakens` to `weakened`, and `eliminates` to
   `eliminated`; attach the exact evidence reference to every update.
7. Preserve every negative result, counterexample, preregistration deviation,
   protocol deviation, and unexpected condition with its exact evidence reference.
8. If the next action changes, cite the specific prior observation that caused the
   change in `next_action_rationale`; otherwise state why the current action
   remains justified.
9. Record the most important remaining uncertainty, the next-action rationale, and
   the exact input `budget_used` without discarding achieved evidence.

## Output

- Artifact: Return exactly one `ObservationUpdate`.
- Required fields: `evidence_refs`, `strengthened`, `weakened`, `eliminated`,
  `counterexamples`, `negative_results`, `remaining_uncertainty`,
  `next_action_rationale`.
- Evidence binding: Cite an exact input receipt or raw reference for every
  strengthened, weakened, eliminated, counterexample, and negative-result entry;
  preserve the same reference in both lists when one executed item has both
  relations.
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

- Do not attach a scientific relation, including `negative_result`, to evidence
  whose `operational_state` is `unavailable` or `failed`.
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
