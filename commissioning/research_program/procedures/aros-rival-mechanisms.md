---
name: aros-rival-mechanisms
source_ids:
  - source-1
  - source-2
input: SourcePacket
output: RivalMechanismSet
tools:
  - Git.read
  - Receipt.read
  - Research.observe
---

## Purpose

Turn bound source evidence into causal alternatives that future observations can
distinguish without selecting a preferred explanation prematurely.

## Inputs

- Artifact: Read exactly one `SourcePacket`.
- Required fields: `query`, `question_ref`, `sources`, `retrieved_at`, `content_refs`,
  `content_sha256s`, `limitations`.
- Lineage: Treat input `question_ref` as the immutable root question reference.

## Method

1. Produce at least two independently formed falsifiable causal mechanisms; derive
   each alternative on its own terms before comparing it with the others.
2. Apply this priority: mechanism compression before literature novelty, and
   literature novelty before impact.
3. For each mechanism, state its prediction, distinguishing observation, falsifier,
   scope, and conflicts with bound evidence or other mechanisms.
4. Record explicit remaining uncertainty, including uncertainty shared by every
   rival, after comparing the alternatives against the same evidence.
5. Identify observations that could discriminate between rivals; this procedure
   does not choose an experiment yet.

## Output

- Artifact: Return exactly one `RivalMechanismSet`.
- Required fields: `root_question_ref`, `mechanisms`, `predictions`, `falsifiers`,
  `conflicts`, `remaining_uncertainty`.
- Lineage: Set `root_question_ref` exactly to input `question_ref`.
- Evidence binding: Preserve the evidence reference supporting or challenging every
  mechanism.

## Completion

- Complete only with at least two surviving rivals.
- Every surviving rival must have at least one discriminating observation and a
  stated falsifier.
- If fewer than two rivals survive, return unresolved and seek additional sources;
  never complete this procedure.

## Forbidden

- Do not rank mechanisms by pilot score or use pilot-score ranking.
- Do not select a top winner.
- Do not retain unfalsifiable mechanisms.
- Do not choose an experiment yet.
