# AROS Task Process Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent trusted-local adapters that create new sessions from producing false terminal receipts or prune eligibility.

**Architecture:** Keep the existing exact adapter PGID as the fast path. On Linux, make the task runner a child subreaper before adapter launch; after the adapter leader exits, any surviving orphan descendants reparent to the runner and must drain before finalization. V1 fails closed with no final when descendants remain; delegated cgroup containment across runner death is deferred to the shared Operations process core.

**Tech Stack:** Python 3.10+, Linux `prctl`, `/proc`, tmux, pytest.

---

### Task 1: Detect escaped descendants before terminal publication

**Files:**
- Modify: `src/aros/task_runner.py`
- Modify: `tests/test_aros_task_runner.py`

- [ ] Write a RED real-process test where the adapter spawns a `start_new_session=True` child, exits 0, and the child waits on a release file. Assert no final while the child is live; after release, the child writes its marker and exits, then final may be completed.
- [ ] Add a RED hard-failure test for unavailable `PR_SET_CHILD_SUBREAPER`: adapter code must never execute and no false completed final may appear.
- [ ] Enable `PR_SET_CHILD_SUBREAPER` before opening the adapter gate. Add a small `/proc/<runner>/task/<runner>/children` reader and reap exited adopted children with `waitpid(-1, WNOHANG)`.
- [ ] Keep logs open and status nonterminal until the exact PGID is drained and no non-zombie adopted descendant remains.
- [ ] Run the focused tests and commit `fix(aros): detect escaped task descendants`.

### Task 2: Fail closed on escaped descendants during timeout, stop, or runner loss

**Files:**
- Modify: `src/aros/task_runner.py`
- Modify: `tests/test_aros_task_runner.py`

- [ ] Add RED tests for a persistent escaped descendant at timeout and stop. Existing PGID signals may be delivered, but final must remain absent and status must become factual `lost` if adopted descendants cannot drain.
- [ ] Add a RED runner-crash test: escaped descendant may outlive the runner, but no terminal receipt or prune eligibility is created.
- [ ] Add a bounded adopted-descendant drain check after normal exit and every stop/timeout path. On timeout, raise `TaskError` before log hashing/final if descendants remain.
- [ ] Fast-path `_claimed_group_is_live`: when the exact leader is present, non-zombie, and token/PGID match, return immediately without scanning all `/proc`; scan the PGID only after leader exit/zombie.
- [ ] Run Task/runner suites and commit `fix(aros): fail closed on uncontained task descendants`.

### Task 3: Publish the exact V1 containment claim

**Files:**
- Modify: `src/aros/task_tool.py`
- Modify: `src/aros/principal.py`
- Modify: `src/cli/commands/aros_cmd.py`
- Modify: `docs/aros/README.md`
- Modify: `docs/superpowers/specs/2026-08-02-aros-v1-product-and-migration-design.md`
- Modify: `docs/analysis/aros-wave2-child-substrate-smoke.md`
- Modify: relevant TaskTool/CLI/Principal/document tests

- [ ] Add tests requiring the operative TaskTool description, Principal prompt, and CLI capability help to say that network/shell flags are audit declarations, execution is trusted-local, secrets/untrusted adapters are unsupported, and daemonizing descendants cause fail-closed/lost behavior.
- [ ] State that V1 proves terminal truth only for the PGID plus descendants reparented to the live subreaper. A process that creates a new session and outlives runner death is not claimed contained; no clean final/prune may be inferred.
- [ ] Record delegated per-task cgroups as Operations-wave work, not a Wave 2 security claim.
- [ ] Run focused/full gates, Ruff, diff/lock checks, update NOW/evidence, and request whole-change reviews.
