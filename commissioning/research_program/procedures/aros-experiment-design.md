---
name: aros-experiment-design
source_ids:
  - source-1
  - source-2
input: RivalMechanismSet
output: ExperimentProposal
tools:
  - Receipt.read
  - Research.observe
  - Research.petition
---

## Purpose

Choose one information-seeking experiment that can resolve the most important
uncertainty among surviving causal rivals without executing it.

## Inputs

- Artifact: Read exactly one `RivalMechanismSet`.
- Required fields: `root_question_ref`, `mechanisms`, `predictions`, `falsifiers`,
  `conflicts`, `remaining_uncertainty`.
- Evidence binding: Use `Receipt.read` to inspect the bound evidence for the
  surviving rivals before designing a proposal.

## Method

1. Begin from first principles: compress each rival into the smallest causal
   mechanism that explains its bound evidence before proposing an intervention.
2. Identify one explicit `decision_uncertainty` that separates the surviving
   rivals or determines that they require revision.
3. For each candidate, state one discriminating prediction, its falsifier,
   controls, primary comparisons, transfer test, stopping rules, rerun plan,
   cache-isolation plan, and exact evaluator binding in the proposed
   `run_request`.
4. Apply the gates lexicographically and in this exact order: essentiality first,
   falsifiability second, and decision relevance third. Reject every candidate
   that fails any gate before comparing expected information gain, cost, or
   concurrency.
5. Among candidates that pass all three gates, maximize expected information gain
   per cost, with every estimate bound to cited evidence and uncertainty.
6. Consider concurrency only after all three gates pass and only when parallel
   proposals improve coverage; concurrency must not change the lexicographic
   choice.

## Output

- Artifact: Return exactly one `ExperimentProposal`.
- Required fields: `mechanism_refs`, `decision_uncertainty`, `prediction`,
  `falsifier`, `controls`, `run_request`, `expected_information_gain`, `cost_bound`.
- Lineage: Restrict `mechanism_refs` to mechanisms in the input
  `RivalMechanismSet` and preserve their evidence bindings.
- Authority: Treat `run_request` as a future AROS request descriptor, not an
  execution command or authorization.

## Completion

- Complete only with one proposal that passes all three gates, maximizes expected
  information gain per cost among the passing candidates, and contains every
  required design binding.
- If no candidate passes every gate, do not emit an `ExperimentProposal`.
- Call `Research.observe` with the unresolved gate failure and missing evidence,
  then call `Research.petition` to request the new evidence needed to form a valid
  proposal.
- Exit incomplete after those calls; never complete this procedure.

## Forbidden

- Do not execute an experiment or emit an executable command; return only an
  `ExperimentProposal`.
- Do not call `Run.request`, `Run.status`, `Eval.run`, any `Task.*` tool, or any
  direct process tool.
- Do not use a shell, subprocess, SSH, remote execution, job queue, upload, or
  notification service.
- Do not rank proposals by pilot score, aggregate score, or a score threshold.
- Do not use a fixed experiment count or fixed-round stopping rule.
- Do not select the top positive result, prefer positive outcomes, or choose a
  winner from pilot results.
