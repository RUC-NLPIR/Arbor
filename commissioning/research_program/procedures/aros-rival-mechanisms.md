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

Read one `SourcePacket`, including `query`, `sources`, `retrieved_at`,
`content_refs`, `content_sha256s`, and `limitations`. Treat its citations and
limitations as evidence boundaries.

## Method

1. Produce at least two independently formed falsifiable causal mechanisms. Derive
   each alternative on its own terms before comparing it with the others.
2. Apply this priority: mechanism compression before literature novelty, and
   literature novelty before impact.
3. For each mechanism, state its prediction, distinguishing observation, falsifier,
   scope, and conflicts with bound evidence or other mechanisms.
4. Record explicit remaining uncertainty, including uncertainty shared by every
   rival, after comparing the alternatives against the same evidence.
5. Identify observations that could discriminate between rivals. This procedure
   does not choose an experiment yet.

## Output

Return exactly one `RivalMechanismSet` with `root_question_ref`, `mechanisms`,
`predictions`, `falsifiers`, `conflicts`, and `remaining_uncertainty`. Preserve the
evidence reference supporting or challenging every mechanism.

## Completion

Complete only when every surviving rival has at least one discriminating observation
and a stated falsifier, scope, conflict set, and remaining uncertainty.

## Forbidden

- Do not rank mechanisms by pilot score or use pilot-score ranking.
- Do not select a top winner.
- Do not retain unfalsifiable mechanisms.
- Do not choose an experiment yet.
