# Legacy Arbor Baseline Dataflow Evidence

This document records one real, one-cycle legacy Arbor run used to establish the
replacement boundary for AROS. It is evidence about the current implementation,
not a normative architecture. The Design Book remains the target specification.

## Run identity

| Field | Value |
| --- | --- |
| Target | `.worktree/baseline-arbor`, copied from `examples/algotune_knn` |
| Session | `baseline-dataflow` |
| Provider/model | `openai-responses` / `gpt-5.4-mini` |
| Budget | 1 completed experiment, 900 seconds |
| Baseline commit | `caa3113` on `main` |
| Candidate commit | `33b7043` on `coordinator/n1-use-partial-selection-instead-of-c8ea47b2` |
| Accepted commit | `9c1441b` on `trunk` |
| Dev result | `1.0022 -> 1.1041` |
| Held-out result | candidate `1.1026`; independent rerun after completion `1.1029` |
| Scope check | `main..trunk` changes only `solution.py` |
| Runtime | 150 seconds, 32 LLM calls, 404,421 total tokens |

The complete local artifacts remain under:

```text
.worktree/baseline-arbor/.arbor/sessions/baseline-dataflow/
```

## Observed end-to-end flow

1. `arbor run` started an intake conversation in
   `.arbor/conversations/conv_20260801_075001/`. The intake Agent inspected an
   explicitly approved path set, produced a `LaunchPlan`, and the CLI rendered a
   second Research Contract confirmation.
2. The CLI resolved config, created the session directory, EventBus, dashboard
   state and `CoordinatorOrchestrator`.
3. The orchestrator created `idea_tree.json`, built one persistent Coordinator
   `core.Agent`, and attached Tree, Executor, Git and finding tools.
4. At each clean Agent turn boundary, `messages.jsonl` and `checkpoint.json`
   were rewritten before the next LLM call. Every LLM/tool/event was also
   appended to `events.jsonl`.
5. The Coordinator read the benchmark and ran `bash eval.sh dev`, obtaining
   `1.0022`. It then created three IdeaTree nodes before writing the baseline,
   eval commands and dataset information into `tree.meta`.
6. `RunExecutorParallel` received three intended directions but truncated the
   batch to one because `max_cycles=1`. Nodes 2 and 3 remained pending.
7. Executor 1 created a Git worktree under `/tmp/coordinator-worktrees-0/`,
   instantiated another `core.Agent`, edited only `solution.py`, and auto-committed
   commit `33b7043` before its final correctness and dev measurements.
8. The Executor reported a dev score in prose. A separate LLM call
   (`parse_executor_report`) converted that prose into `1.1041`, node state and
   `experiments/1/metrics.json`. Another LLM call propagated the insight upward.
9. The first merge failed because the production `arbor run` path had no
   configured trunk branch. The Coordinator used Bash to create `trunk`, then
   retried `GitMergeBranch`.
10. The merge tool evaluated the source branch on the test split in a detached
    worktree. A separate LLM call (`parse_eval_score`) converted stdout into
    `1.1026`; the tool merged the branch into `trunk` at `9c1441b`.
11. The Coordinator separately repaired `tree.meta` and node state, recorded a
    finding, then finalization derived `run_stats.json`, `trajectory.jsonl`,
    `REPORT.md` and experience files from events plus the IdeaTree.

## Actual state ownership

| State | Writer | Role in legacy Arbor |
| --- | --- | --- |
| Intake `messages.jsonl` and `meta.json` | intake REPL | Planning-chat continuity |
| Coordinator `messages.jsonl` | checkpoint hook | Provider transcript replay |
| `idea_tree.json` | Tree tools | Canonical scientific/navigation state |
| `checkpoint.json` | orchestrator | Resume metadata and cached Git/process view |
| Git branches/commits | Agent GitManager and merge tool | Canonical code artifact lineage |
| `events.jsonl` | EventBus file subscriber | Durable audit stream, not recovery truth |
| experiment report/metrics | Executor plus LLM parser | Derived experiment summary |
| reports/trajectory/experience | finalizers | Derived views |

There is therefore no single legacy source of truth. Resume requires transcript,
IdeaTree, checkpoint metadata and Git to remain mutually consistent.

## Structural findings that drive AROS

### Measurement and state mutation are not atomic

The baseline measurement existed at Coordinator turn 3, but `tree.meta` stayed
empty until turn 10, after ideation and execution. Scientific actions therefore
ran against a state view that said `baseline=N/A`. Measurement must instead
produce a deterministic receipt immediately; Principal interpretation may happen
later without losing the observation.

### Budget is hidden until dispatch

`TreeView(constraints)` did not expose the one-cycle remaining budget. The
Coordinator created three nodes, then `RunExecutorParallel` silently truncated
execution to one. Two permanently pending nodes and the Coordinator's own
confusion were the result. AROS boot/inspect must expose operational budget while
leaving action selection to the Principal.

### Mechanical Git setup leaked into Agent reasoning

The final checkpoint still records `trunk_branch: null`. The first merge failed,
and the Agent created `trunk` through Bash. The independent Coordinator CLI has
Git bootstrap logic that the production `arbor run` path does not share. AROS
must have one deterministic Git service used by every adapter.

### Evaluation is not an independent apparatus

Both primary measurements were accepted through LLM parsing: dev from Executor
prose and test from `parse_eval_score`. The run preserved no test manifest, raw
test stdout, evaluator/environment hash or final measurement receipt. The test
was performed on the source branch before merge, not on a sealed post-merge
commit. This cannot satisfy AROS evaluator integrity.

### Final checkpoint is not runtime truth

The final checkpoint records `trunk_branch: null`, truncates the active branch to
`ordinator/...`, and contains no exact HEAD, command, evaluator, environment or
output hashes. It is useful replay metadata but cannot be the AROS operational
receipt.

### Process lifetime remains session-bound

The Executor ran inside the Coordinator call and its worktree was removed before
the tool returned. The run completed successfully, but terminating the Principal
would cancel this control path. Durable AROS runs and material child tasks must
be independent processes discoverable from manifests and receipts.

## Reuse and retirement boundary

Reuse `core.Agent`, provider adapters, basic file tools, low-level Git/process
helpers, content hashing and EventBus fan-out. Replace the orchestration assembly:

- do not load `CoordinatorOrchestrator`, Tree tools, fixed Arbor Cycle prompts,
  insight propagation or LLM metric parsers in the AROS Principal path;
- make editable workspace files canonical for scientific state;
- make run/eval/task manifests and receipts canonical for operational state;
- retain legacy sessions only as historical input until migration audit passes;
- delete legacy semantic orchestration only after equivalent AROS behavior has
  passed commissioning.

## Evidence hashes

The following hashes identify the inspected final artifacts:

```text
events.jsonl       afbda867a041d82ce52f39ce26713c12c6e1114948f9b84cf8b2b8facc1f2fbd
idea_tree.json     98f34d17e4bc76d7464325ed5d378ff1d919445cf52cc377d6acee7c5a48f875
checkpoint.json    a62115f4fd00dcfa4b11bb52bd968b64077dd63568e301d867ffd6b91fe52f40
metrics.json       2046211d41a6374b5100c0c6b28bb815b3a892aeaa29b2cdb217ef7bc3f67b8f
REPORT.md          839024abfc8cfd3736ecce0d07b869403c30464f125282a2e407248fc1d5e43b
run_stats.json     f612c9068123b335be228dbb6df11b2386dd2abce7ab6402deaca2397899750f
trajectory.jsonl   cabb416e743e72d0850af20f972c3f95e02b5d6addeeb92e321dc842cc7d9915
```
