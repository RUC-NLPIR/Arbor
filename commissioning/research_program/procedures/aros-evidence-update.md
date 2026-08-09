---
name: aros-evidence-update
source_ids:
  - source-1
  - source-2
input: RunEvidence
output: ObservationUpdate
tools:
  - Run.status
  - Receipt.read
  - Git.read
  - Research.observe
  - Research.checkpoint
---

## Purpose

Turn immutable run, evaluation, and raw evidence into a durable scientific update
without changing the evidence or continuing the experimental session.

## Inputs

- Artifact: Read exactly one `RunEvidence`.
- Required fields: `run_ref`, `eval_refs`, `raw_refs`, `process_state`,
  `budget_used`, `rival_mechanism_set_ref`, `mechanism_refs`,
  `experiment_proposal_ref`, `prediction_ref`, `preregistration_ref`.
- Immutability: Treat `run_ref`, every `eval_ref`, and every `raw_ref` as an
  immutable exact reference.
- Lineage: Treat `rival_mechanism_set_ref`, `mechanism_refs`,
  `experiment_proposal_ref`, `prediction_ref`, and `preregistration_ref` as the
  complete input lineage; do not infer omitted lineage.

## Method

1. Read the exact immutable receipts identified by `run_ref` and `eval_refs` and
   the exact immutable raw references identified by `raw_refs`; bind each read to
   its reference before interpretation.
2. Use `Git.read` to read the exact `RivalMechanismSet`, `ExperimentProposal`,
   prediction, and `Preregistration` named by the lineage fields before
   classification; reject missing or inconsistent lineage.
3. Require input `mechanism_refs` to belong to the referenced `RivalMechanismSet`
   and to match the `ExperimentProposal` `mechanism_refs`; require `prediction_ref`
   to identify that proposal's prediction.
4. Emit exactly one `classifications` entry for every evidence item; each entry is
   interpreted only within the verified proposal and prediction lineage.
5. Set each `evidence_ref` to exactly the input `run_ref`, one of the input
   `eval_refs`, or one of the input `raw_refs`; emit no classification for any
   other reference.
6. For every evidence item, record exactly one `operational_state`: `unavailable`,
   `failed`, or `executed`; determine it only from the exact process, transport,
   evaluator, and measurement receipts.
7. Only an item whose `operational_state` is `executed` may carry
   `scientific_relations`; assign zero or more of `supports`, `weakens`,
   `eliminates`, `counterexample`, and `negative_result` from the scientific
   evidence.
8. Allow one executed item to carry both `negative_result` and `counterexample`
   when both apply; scientific relations are not mutually exclusive.
9. An item whose `operational_state` is `unavailable` or `failed` must have empty
   `scientific_relations` and must never be recorded as a `negative_result`.
10. For every `supports`, `weakens`, `eliminates`, or `counterexample` relation,
    require every `relation_targets` value to belong to the input `mechanism_refs`;
    do not infer or name an arbitrary rival.
11. Connect every executed classification's `evidence_ref` to the input
    `experiment_proposal_ref` and `prediction_ref` before applying any scientific
    relation.
12. Treat a `supports` relation as confirmatory only when the input
    `preregistration_ref` names a `Preregistration` that binds the same proposal and
    prediction; otherwise record it as non-confirmatory support.
13. Map `supports` to `strengthened`, `weakens` to `weakened`, and `eliminates` to
   `eliminated`; attach the exact evidence reference to every update.
14. Preserve every negative result, counterexample, preregistration deviation,
   protocol deviation, and unexpected condition with its exact evidence reference.
15. If the next action changes, cite the specific prior observation that caused the
   change in `next_action_rationale`; otherwise state why the current action
   remains justified.
16. Record the most important remaining uncertainty, the next-action rationale, and
   the exact input `budget_used` without discarding achieved evidence.

## Output

- Artifact: Return exactly one `ObservationUpdate`.
- Required fields: `evidence_refs`, `classifications`, `strengthened`, `weakened`,
  `eliminated`, `counterexamples`, `negative_results`, `remaining_uncertainty`,
  `next_action_rationale`.
- Classification entries: Every `classifications` item contains exactly
  `evidence_ref`, `operational_state`, `scientific_relations`, and
  `relation_targets`.
- Evidence binding: Cite an exact input receipt or raw reference for every
  strengthened, weakened, eliminated, counterexample, and negative-result entry;
  preserve the same reference in both lists when one executed item has both
  relations.
- Budget accounting: Include the exact input `budget_used` in
  `next_action_rationale`; do not add an output field outside the central contract.

## Completion

- Complete only after every declared receipt, raw reference, and lineage artifact
  has been read and every evidence item has exactly one valid classification in
  one `ObservationUpdate`.
- If any required lineage field is missing or inconsistent, do not emit an
  `ObservationUpdate`; confirmatory support additionally requires a matching
  `preregistration_ref`.
- Call `Research.observe` with the complete `ObservationUpdate`.
- Call `Research.checkpoint` with the observation update and its exact immutable
  evidence references.
- Exit immediately after the successful checkpoint; do not continue the research
  session.

## Forbidden

- Do not attach a scientific relation, including `negative_result`, to evidence
  whose `operational_state` is `unavailable` or `failed`.
- Do not place a value outside the input `mechanism_refs` in `relation_targets` or
  update an arbitrary rival.
- Do not call `Eval.run`, create an evaluation, or execute an evaluator; read only
  existing evaluation receipts.
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
