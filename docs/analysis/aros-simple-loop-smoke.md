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
→ one Task executed by its Task-owned Run
→ one visible Eval
→ Principal Question/Model/Idea/Claim/NOW interpretation
→ Checkpoint with automatic observed trailers
→ destroy primary Agent/provider
→ fresh Agent Attention
```

The model tool sequence contains no `Run` call: `Task(action="start")` creates the
single idempotent trusted-local Task-owned Run. Task still owns its brief,
worktree, mailbox, return, collection, preservation, and pruning; Run alone owns
process, log, stop, timeout, lost, and descendant truth.

This evidence proves deterministic composition and continuity. It does not prove
external-model research quality, a real Researcher inner loop, async portfolio,
Source Gateway, Independent Reviewer, budgets/Mission Supervisor, Skills/MCP,
protected authority, or Arbor retirement. AROS remains limited; this is not a
“strictly better than Arbor” claim.

## Build and evidence identity

- Product source commit:
  `4f1eb3df2578f2ba45c8d542ff8fe0e07a37fe10`.
- Clean wheel SHA-256:
  `41320cd866d62395c38b9b56c3a8c26029a1da8ce34ed46a010f808ed137d386`.
- Evidence SHA-256:
  `b5f35280d05e24f528d337b3916f6d72a3d394beb526e7fa06febcae57c96820`.
- Ignored exact evidence:
  `/workspace/Arbor/.worktree/commissioning/aros-simple-loop-task-run-4f1eb3d-1/evidence.json`.
- Recursive `src/aros` source count: exactly `17,700` physical lines. This meets
  the human-approved Phase 0A interim gate with the old carrier absent; Phase 0B
  still requires the original program-wide `src/aros <= 12,000 LOC` gate before
  Phase A Mission Supervisor work.

The standalone verifier binds the source commit to the wheel, installed
distribution tree, `RECORD`, console entrypoint, and evidence. The wheel contains
`task_adapter.py` and `task_run.py`, contains no `task_runner.py`, and the installed
distribution contains no bytecode. It also verifies mode-normalizing filesystem
and controlled Git optional-lock facts without treating them as protected
authority. Both its embedded run and a hostile standalone `-I -S` run returned
`state=verified`.

## Post-commission current-product gate

The retained evidence and hashes above remain bound to Task 8 source `4f1eb3d`;
they are not rewritten by later product maintenance. Current source
`26fe611fc88252a4667d24b0db92b742f654712e` separately fixes the stop/final
publication race found by the post-full stress gate: a delivered stop waits for its
validated matching `cancelled` final, while delivered-false remains immediate.

The first `/workspace/Arbor/.venv/bin/python -m pytest -q` run was
**non-authoritative**: that shared venv's editable finder targeted the
`/workspace/Arbor` main checkout, so child interpreters lacked the current
`task_adapter`, `runner`, and `processes` implementation from this worktree. It
collected 1,961 tests and reported 1,903 passed, 6 skipped, and 52 failed; more
than 50 failures were cascading stale-module failures. This was environment
contamination, not a product result.

A clean detached checkout of `26fe611f` produced
`arbor_agent-0.1.1.dev487+g26fe611fc-py3-none-any.whl`, SHA-256
`aa32f63dad0587a7cb50b3ead01dfd092c51a705172c1ac0f2dc05849dad28f4`.
Its complete product package tree matched the clean source, and a new isolated
`--no-compile` installation matched the wheel, contained no bytecode, imported
`task_adapter`, `runner`, `processes`, and `runs` from site-packages, and passed
`pip check`. From that environment:

- the authoritative full repository pytest collected 1,964 tests: 1,958 passed,
  6 skipped, 0 failed, exit 0;
- the adopted-descendant public-stop test passed 20/20 consecutive runs;
- current recursive `src/aros` is 17,697 physical lines, within the approved
  Phase 0A `<= 17,700` interim gate.

## Exact research and Task-owned Run lineage

- Preregistration/base B commit:
  `9fa6985727f6483665e9138ae28b4c6ad5d564db`.
- Task:
  `TASK-20260807-produce-the-deterministic-succes-fae93271e91aa823`.
- Task-owned Run:
  `RUN-20260807-204009-task-task-20260807-produce-the-d-500d`.
- Run manifest SHA-256:
  `813b0d8d30fc312c14b9816cd345c09ebefbe635ab19d177c87c1747ff2b86df`.
- Run final SHA-256:
  `1bf1969feb484814e15a5f446104a821b62d9c415fbd5dbda9c104ac95eda638`.
- Task candidate C commit:
  `5c502096d697023e13f395421b59c08c7fd50051`.
- Task return R commit:
  `b1a997a5597cc3304d6bebbb1116d0f21b4a9f87`.
- Task collection SHA-256:
  `9acd36de01ef71d394b300b7c029e948948833ee1c56fe6333963b3db8fda662`.
- Eval:
  `EVAL-adb38ec242d4647e61674487a3b69fa9ade2cf858d239f7baa5d35112aa5c9b7`.
- Eval receipt SHA-256:
  `888cfbb29283753835a11efe2452c726aff6de5663d2155a625cfa455def5abe`.
- Measurement: `valid`, metric `1.0`, candidate commit C above.
- Final parent: `b39a570a91ab675e55dd73464fedf5a25abc52de`.
- Final research commit:
  `7ca93fc634d08e4598016f7b4e9ad2fd276e4c57`.

The collection binds the exact TaskRun manifest/final and B-C-R return lineage.
The final checkpoint changed exactly:

```text
ideas/I-E2E.md
knowledge/claims/C-0001.md
memory/NOW.md
model/CURRENT.md
questions/Q-0001/question.md
```

Its commit message contains exactly these three `AROS-Observed:` refs:

```text
eval/evaluations/EVAL-adb38ec242d4647e61674487a3b69fa9ade2cf858d239f7baa5d35112aa5c9b7/receipt.json
runs/RUN-20260807-204009-task-task-20260807-produce-the-d-500d/final.json
tasks/TASK-20260807-produce-the-deterministic-succes-fae93271e91aa823/collected.json
```

Thus Principal observes both the Task-owned Run final and Task collection, plus
the Eval receipt. Task/Run/Eval operational records were committed before the
semantic checkpoint; final project `git status --porcelain` was empty.

## Restart proof

The primary native `arbor.core.agent.Agent` was destroyed with its provider before
restart. A distinct Agent/provider started with zero prior messages, called only
`Attention`, and returned:

- `unread_returns=[]`;
- recent commit `7ca93fc634d08e4598016f7b4e9ad2fd276e4c57`;
- the exact three observed refs above;
- the exact five semantic paths.

The retained standalone verifier returns `state=verified` for this exact evidence.
