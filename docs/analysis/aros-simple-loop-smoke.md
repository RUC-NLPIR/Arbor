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
they are not rewritten by later product maintenance. The first post-commission
fix `26fe611fc88252a4667d24b0db92b742f654712e` separately fixes the stop/final
publication race found by the post-full stress gate: a delivered stop waits for
its validated matching `cancelled` final, while delivered-false remains immediate.

The first `/workspace/Arbor/.venv/bin/python -m pytest -q` run was
**non-authoritative**: that shared venv's editable finder targeted the
`/workspace/Arbor` main checkout, so child interpreters lacked the current
`task_adapter`, `runner`, and `processes` implementation from this worktree. It
collected 1,961 tests and reported 1,903 passed, 6 skipped, and 52 failed; more
than 50 failures were cascading stale-module failures. This was environment
contamination, not a product result.

The historical interim clean gate for `26fe611f` produced
`arbor_agent-0.1.1.dev487+g26fe611fc-py3-none-any.whl`, SHA-256
`aa32f63dad0587a7cb50b3ead01dfd092c51a705172c1ac0f2dc05849dad28f4`.
It collected 1,964 tests: 1,958 passed, 6 skipped, 0 failed, exit 0; its
adopted-descendant public-stop test passed 20/20 consecutive runs. This result is
retained as history, not presented as current.

The historical second clean gate for
`14d8268ae5e82a11c872e6052027f7cf064a7337` extends that fix by repeatedly
signaling nested adopted descendants after escalation and by treating ESRCH
during `/proc` stat as disappearance while other observation errors remain
fail-closed. Its clean detached checkout produced
`arbor_agent-0.1.1.dev489+g14d8268ae-py3-none-any.whl`, SHA-256
`0647e4fef99672d71881700409d5404268c2eaf10f6f3283b8fb12d1f1dbcd7a`.
It collected 1,969 tests: 1,963 passed, 6 skipped, 0 failed, exit 0; the nested
stop, nested timeout, and ESRCH set passed 20/20 consecutive iterations. This
result is also retained as history, not labeled current.

The historical third clean gate for
`c11eed140ca99ec6ff0d5e8d60243242411fd624` restricts repeated KILL to an
already-delivered stop or triggered timeout. A failed direct KILL is never retried
or hidden; receipt and final preserve `delivered=false` truth. Its clean detached
checkout produced `arbor_agent-0.1.1.dev491+gc11eed140-py3-none-any.whl`,
SHA-256
`d183d03350e228b1956d73303d9c48fb1c1933d7f126359e0b5e4b4ac148ac55`.
It collected 1,970 tests: 1,964 passed, 6 skipped, 0 failed, exit 0; its four-test
process-truth set passed 20/20 consecutive iterations. This result is retained as
history, not labeled current.

Current authoritative code source
`a07f50fce557ea1b89c0e6d87836b407dce44922` records `KILL` only after a
successful refreshed delivery and at most once in each stop/timeout signal
sequence. Its clean detached checkout produced
`arbor_agent-0.1.1.dev493+ga07f50fce-py3-none-any.whl`, SHA-256
`e0b4d92c2015ff0a21dee20e3b6fc520880e403d09443bf7ad3b37cf5bbd29f1`.
The complete product package tree matched the clean source, and a new isolated
`--no-compile` installation matched the wheel, contained no bytecode, imported
`task_adapter`, `runner`, `processes`, and `runs` from site-packages, and passed
`pip check`. From that exact-wheel environment:

- the current authoritative full repository pytest collected 1,972 tests: 1,966
  passed, 6 skipped, 0 failed, exit 0;
- the refreshed-KILL stop/timeout, direct-KILL-false, nested stop/timeout, and
  ESRCH regression set passed 20/20 consecutive iterations, totaling 120 test
  invocations;
- current recursive `src/aros` is exactly 17,699 physical lines, within the approved
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
