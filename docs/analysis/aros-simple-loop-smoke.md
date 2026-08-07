# AROS Simple Agent Research Loop E2E Evidence

Status: verified cooperative commissioning evidence  
Date: 2026-08-07

## Scope

This commissioning proves the minimal Agent-centric loop through the installed
`aros` product:

```text
Attention
→ Principal prose preregistration
→ Checkpoint(message, paths)
→ one durable Task
→ one visible Eval
→ Principal Question/Model/Idea/Claim/NOW interpretation
→ Checkpoint with automatic observed trailers
→ destroy primary Agent/provider
→ fresh Agent Attention
```

It proves deterministic composition and continuity, not external-model research
quality or protected authority.

## Build identity

- Product source commit: `541b496f9cd6ffef80992a58aee27d584b95a1a6`.
- Clean wheel SHA-256:
  `d0a54b00008340da0eb1889ee12a33d42586fefdd5bca7e258ae8a4257a80e76`.
- Evidence SHA-256:
  `a7b533421c0e32ba736a788c2c2869772347fcc45cb4439a02757e09936ca619`.
- Ignored exact evidence:
  `/workspace/Arbor/.worktree/commissioning/aros-simple-loop-541b496/evidence.json`.
- The normal venv passed `pip check`; the installed wheel contained none of the
  deleted control-plane modules.

## Exact research lineage

- Preregistration commit: `92a68c5c2eac9aedf827772f591156ad1d540bf0`.
- Task:
  `TASK-20260807-produce-the-deterministic-succes-68b44c91eccafb2c`.
- Task candidate commit: `ceb7f5dcb45772cf9fc0dd4c51bdb425906ba41c`.
- Task return commit: `3ae266fe65c028f54c4d0664b41891a4f871a8d3`.
- Task collection SHA-256:
  `f3abe6501bb0fcd2edf6fd3e1d0ab5b597bae5897484c1405390f217e4156273`.
- Eval:
  `EVAL-adb38ec242d4647e61674487a3b69fa9ade2cf858d239f7baa5d35112aa5c9b7`.
- Eval receipt SHA-256:
  `7c0571ee575d990a4d028d02f765a3a9eb726343b86ebeadb7b759b9e1d1c48a`.
- Measurement: `valid`, metric `1.0`, same candidate commit as Task.
- Final parent: `841e8bd74722fb83989e15ed45635c0f86f057da`.
- Final commit: `2bbdb51efaa0078fdbc7f9d317c4cb4f64d6da64`.

The final commit changed exactly:

```text
ideas/I-E2E.md
knowledge/claims/C-0001.md
memory/NOW.md
model/CURRENT.md
questions/Q-0001/question.md
```

Its commit message contains exactly the Task collection and Eval receipt
`AROS-Observed:` trailers. Task/Run/Eval operational records were already cleanly
committed before the semantic checkpoint; final `git status --porcelain` was empty.

## Restart proof

The primary native `arbor.core.agent.Agent` ran 18 turns and was garbage-collected
with its provider before restart. A distinct Agent/provider started with zero prior
messages, called only `Attention`, and returned:

- `unread_returns=[]`;
- recent commit `2bbdb51efaa0078fdbc7f9d317c4cb4f64d6da64`;
- the exact two observed refs;
- the exact five semantic paths.

The standalone verifier was then invoked separately and returned `state=verified`.
