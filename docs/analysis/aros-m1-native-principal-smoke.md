# AROS M1 Native Principal Smoke Evidence

This is the real-provider acceptance record for the M1 bootable workspace and
native Principal module. It is implementation evidence, not a replacement for
the Design Book.

## Scope

M1 adds the explicit `arbor aros` path while leaving legacy `arbor run`
unchanged. The path directly constructs `core.Agent`; it does not import or
instantiate Coordinator, IdeaTree, Executor, MCP session state, convergence or
legacy report finalizers.

The Principal receives only workspace-scoped Read/Grep/Glob/Edit/Write/Inspect
tools by default. Bounded foreground Bash is an explicit trusted-local opt-in.
Automatic Git commits are disabled and Agent runtime state is written beneath
`.aros/agent`, not `.arbor`.

## Automated verification

```text
M0 + M1 targeted tests: 29 passed
Full suite: 512 passed, 6 skipped
Ruff: passed
git diff --check: passed
```

Coverage includes idempotent initialization, Git-root enforcement, bounded boot
context, transcript/provider-memory exclusion, Git status/worktree discovery,
exact Principal tool surface, direct workspace writes, shell opt-in, path and
symlink escape denial, runtime-directory isolation and top-level CLI routing.

## Real-provider smoke

The smoke workspace is retained locally at `.worktree/aros-m1-smoke`.

1. `arbor aros init` created a new Git-backed workspace with mission:
   `Prove that a fresh native Principal can persist and recover project state
   without a transcript.`
2. A real `openai-responses/gpt-5.4-mini` Principal was instructed to update
   `memory/NOW.md` and create `evidence/m1-smoke.txt`.
3. Its first incorrect attempt to read `/workspace/Arbor/AROS.md` and
   `/workspace/Arbor/memory/NOW.md` was blocked by the workspace path authorizer.
   It corrected to the authorized `.worktree/aros-m1-smoke` paths.
4. The Principal wrote and re-read both durable files, then the workspace state
   was committed as `4ff41e4`.
5. `.aros/agent` was moved aside to remove the first session runtime. A second
   fresh Principal started with no transcript replay and recovered, in two
   turns, the exact mission, current state, uncertainty and evidence token:
   `M1_PRINCIPAL_WROTE_DURABLE_EVIDENCE`.
6. The final workspace was Git-clean and contained no `.arbor`, IdeaTree or
   Coordinator artifacts.

## Exit result

M1 satisfies the narrow continuity gate: a native Principal can directly write
versioned project state, lose its runtime session, and recover the required
project state from the workspace alone. Durable experiments, evaluator receipts,
child isolation and Principal leases remain explicitly outside M1 and are not
claimed by this result.
