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
  `experiment_proposal_ref`, and `prediction_ref` as required non-null lineage,
  and treat `preregistration_ref` as a required field whose value may be null; do
  not infer omitted lineage.

## Method

1. Read the exact immutable receipts identified by `run_ref` and `eval_refs` and
   the exact immutable raw references identified by `raw_refs`; bind each read to
   its reference before interpretation.
2. Use `Git.read` to read the exact `RivalMechanismSet`, `ExperimentProposal`, and
   prediction named by the non-null lineage fields before classification; when
   `preregistration_ref` is non-null, also read that exact `Preregistration`;
   reject missing or inconsistent non-null lineage.
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
7. Set every classification's `confirmation_status` to exactly `confirmatory` or
   `non_confirmatory`.
8. Only an item whose `operational_state` is `executed` may carry
   `scientific_relations`; assign zero or more of `supports`, `weakens`,
   `eliminates`, `counterexample`, and `negative_result` from the scientific
   evidence.
9. Allow one executed item to carry both `negative_result` and `counterexample`
   when both apply; scientific relations are not mutually exclusive.
10. An item whose `operational_state` is `unavailable` or `failed` must have empty
    `scientific_relations` and must never be recorded as a `negative_result`; set
    its `confirmation_status` to `non_confirmatory`.
11. For every `supports`, `weakens`, `eliminates`, or `counterexample` relation,
    require every `relation_targets` value to belong to the input `mechanism_refs`;
    do not infer or name an arbitrary rival.
12. Connect every executed classification's `evidence_ref` to the input
    `experiment_proposal_ref` and `prediction_ref` before applying any scientific
    relation.
13. When the input `preregistration_ref` is null, set every `confirmation_status`
    to `non_confirmatory`; executed evidence may still carry `supports`, `weakens`,
    `eliminates`, `counterexample`, or `negative_result`.
14. For `candidate_commit`, require the executed receipt value to exactly equal the
    `Preregistration` value.
15. For `data_manifest_refs`, require the executed receipt references and order to
    exactly equal the `Preregistration` value.
16. For `environment_sha256`, require the executed receipt value to exactly equal
    the `Preregistration` value.
17. For `evaluator_version`, require the executed receipt value to exactly equal the
    `Preregistration` value.
18. For `output_schema_sha256`, require the executed receipt value to exactly equal
    the `Preregistration` value.
19. For `controls`, require the executed receipt controls and values to exactly equal
    the `Preregistration` value.
20. For `primary_comparisons`, require the executed receipt comparison identities
    and measurements to exactly equal the `Preregistration` specification.
21. For `analysis_boundaries`, require the executed analysis to exactly equal the
    `Preregistration` value.
22. For `stopping_rules`, require the executed stopping behavior to exactly equal
    the `Preregistration` value.
23. For `rerun_rules`, require the executed rerun behavior to exactly equal the
    `Preregistration` value.
24. For any missing or deviating confirmation binding, set `confirmation_status` to
    `non_confirmatory` and append a `confirmation_deviations` item containing the
    field name, expected preregistered value, observed receipt value or a missing
    marker, and `evidence_ref`; record every mismatch.
25. Set `confirmation_status` to `confirmatory` only for executed evidence when
    `preregistration_ref` is non-null, the proposal and prediction lineage matches,
    every confirmation binding matches exactly, and `confirmation_deviations` is
    empty; null never yields `confirmatory`.
26. Map `supports` to `strengthened`, `weakens` to `weakened`, and `eliminates` to
    `eliminated`; attach the exact evidence reference to every update.
27. Preserve every negative result, counterexample, preregistration deviation,
    protocol deviation, and unexpected condition with its exact evidence reference.
28. If the next action changes, cite the specific prior observation that caused the
    change in `next_action_rationale`; otherwise state why the current action
    remains justified.
29. Record the most important remaining uncertainty, the next-action rationale, and
    the exact input `budget_used` without discarding achieved evidence.

## Output

- Artifact: Return exactly one `ObservationUpdate`.
- Required fields: `evidence_refs`, `classifications`, `strengthened`, `weakened`,
  `eliminated`, `counterexamples`, `negative_results`, `remaining_uncertainty`,
  `next_action_rationale`.
- Classification entries: Every `classifications` item contains exactly
  `evidence_ref`, `operational_state`, `scientific_relations`, and
  `relation_targets`, `confirmation_status`, and `confirmation_deviations`.
- Confirmation deviations: `confirmation_deviations` is empty only when
  `confirmation_status` is `confirmatory`; for `non_confirmatory`, list every
  missing or deviating preregistration binding with its field name, expected value,
  observed value or missing marker, and `evidence_ref`.
- Evidence binding: Cite an exact input receipt or raw reference for every
  strengthened, weakened, eliminated, counterexample, and negative-result entry;
  preserve the same reference in both lists when one executed item has both
  relations.
- Budget accounting: Include the exact input `budget_used` in
  `next_action_rationale`; do not add an output field outside the central contract.

## Completion

- Complete only after every declared receipt, raw reference, and non-null lineage
  artifact has been read and every evidence item has exactly one valid
  classification in one `ObservationUpdate`.
- If required rival, proposal, or prediction lineage is missing or inconsistent,
  do not emit an `ObservationUpdate`; a null `preregistration_ref` is allowed and
  does not block completion.
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
- Do not set `confirmation_status` to `confirmatory` when `preregistration_ref` is
  null, the evidence is not executed, any required receipt lineage value is missing
  or deviates, or `confirmation_deviations` is nonempty.
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
