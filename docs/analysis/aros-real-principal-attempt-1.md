# AROS Real Principal Scientific Turn — Attempt 1

## Decision

Attempt 1 is preserved and rejected as incomplete. It is not commissioning
evidence and does not justify a scientific or product completion claim.

Runtime:

```text
/workspace/Arbor/.worktree/commissioning/aros-real-principal-run-1/
```

## What completed

- The real external model identified itself as `gpt-5.6-luna`.
- The Principal read the Question, local source, repository, Task adapter, and
  evaluator.
- It authored a scoped ScientificModel, rival, premeasurement prediction,
  controls, and one Idea.
- It admitted preregistration commit
  `168766230b43c2746af1cf3c369144eb69d234b6` before Task/Eval launch.
- It created, ran, and collected exactly one Task.
- Task candidate C:
  `5094666cf4c5335575e379b9c077e424fa4f1d83`.
- Task return R:
  `d3f0f60ae04cda95924de90d82a1fb3ad6382ac2`.
- It registered and ran exactly one Eval against C.
- The independent apparatus produced a valid one-sample metric `1.0` with
  receipt SHA-256
  `176dba2188c55321da87ed7df4c613952ff5f37462fa8c6637b8a28cf99460cc`.
- It authored postmeasurement Question/Model/Idea/Claim/NOW candidates with
  appropriately narrow scientific limitations.

## Why the attempt is incomplete

The primary Agent exhausted the exact 40-turn budget:

```text
stop_reason: max_turns
input_tokens: 948884
output_tokens: 22747
```

It admitted only the preregistration checkpoint. While constructing the final
proposal it repeatedly guessed incompatible Transition/EvidenceLink field
names because the instruction did not provide the exact machine schema. Final
semantic files and `T-REAL-ASSIMILATE` therefore remain dirty/untracked.

Canonical HEAD is only the last operational admission:

```text
864cfff39cb53232de4b56bcfc9c8d98cbaa58a5
```

The mechanical verifier correctly rejected the evidence at the primary session
boundary. No human-review packet was issued.

## Invalid restart evidence

The first driver incorrectly started a fresh model session after the incomplete
primary. That session began with zero messages and produced a scientifically
reasonable explanation, but it read dirty candidate semantic files that were
not canonical. It is therefore diagnostic output only, not restart continuity
evidence.

## Root causes and corrections

1. The driver now refuses restart unless the primary finishes and records
   exactly two checkpoint commits.
2. Effective provider/model/effort facts are read from the provider object,
   not the Principal runtime AgentConfig.
3. The instruction now supplies exact four-field TransitionProposal,
   three-field assimilation, strict three-key EvidenceLink, Claim headings, and
   Idea frontmatter contracts.
4. No model fallback or automatic second attempt was run.

Any Attempt 2 requires a new explicit human authorization and a new absent
runtime root.
