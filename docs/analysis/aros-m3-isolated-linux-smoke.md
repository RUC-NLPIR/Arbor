# AROS M3 Isolated Linux Smoke Evidence

This document records the real acceptance evidence and explicit limits of the
M3 `isolated-linux` capability profile.

## Environment decision

The original plan selected bubblewrap, but the real host rejected both `bwrap`
and `unshare` with `Operation not permitted`: the container lacks namespace
capabilities despite exposing the binaries. Treating a binary-presence check as
successful isolation would violate the Design Book and the per-module real-test
requirement.

After discussion with Claude, M3 instead uses primitives that were positively
probed and exercised in this environment:

- Linux Landlock ABI 4 for filesystem access;
- libseccomp for network, signalling, process creation and kernel escape
  syscall denial;
- `no_new_privs`, empty capability sets and bounding set;
- strict environment allowlist with fixed `PATH` and no inherited secrets;
- CPU, address-space, file-size, file-count, process and core-dump rlimits.

There is one fixed implementation, not a backend framework. If Landlock ABI 4
or libseccomp is unavailable, `start` fails before writing launch receipts and
never downgrades to `trusted-local`.

## Capability contract

`isolated-linux` is now the default for CLI and Principal `Run`; selecting
`trusted-local` must be explicit.

- workspace content is read-only except explicit existing writable paths;
- `.git`, `.aros`, `.worktree`, `runs`, `.env*`, secret/key/pem files and
  symlinks are not readable or enumerable;
- host filesystem paths outside the allowed system runtime and workspace view
  are denied;
- network socket and io_uring entry points are denied;
- signal, ptrace, process-memory, pidfd, namespace, mount, module, BPF, keyring
  and related escape system calls are denied;
- fork, clone, clone3, vfork and thread creation are denied in this first fixed
  profile, recorded as `process_policy=single-process-no-threads`;
- only allowlisted environment values cross the boundary; HOME/TMPDIR point to
  the first explicit writable directory or `/nonexistent`;
- runner reconstructs the policy from the frozen manifest and compares every
  policy field before exec.

This profile does not claim defense against host kernel compromise, host root
tampering with receipts, GPU isolation, domain-scoped egress, or workloads that
require subprocesses/threads. Those are explicit scope limits, not silent
fallbacks.

## Automated verification

```text
M0-M3 targeted tests: 84 passed
Full suite: 567 passed, 6 skipped
Ruff: passed
git diff --check: passed
```

Real isolation tests verify filesystem read/write boundaries, reserved and
sensitive paths, environment stripping, deny-all networking, io_uring denial,
signal/process/namespace denial, capability sets, no-new-privs, resource limits,
policy normalization and fail-closed capability probing. Durable tmux tests
also execute the policy through the real RunService/runner path.

## Real end-to-end smoke

The retained workspace is `.worktree/aros-m1-smoke`. The corrected evidence run
is `RUN-20260801-090841-isolated-boundary-v2-3354`, committed in that workspace
at `5951239`.

Without specifying a profile, CLI selected `isolated-linux`. The target process
attempted every boundary below while receiving only `isolated-output` as a
writable path:

| Attempt | Observation |
| --- | --- |
| Read root Design Book outside workspace | `EACCES` |
| Read committed `.env` sentinel | `EACCES` |
| Read `.git/config` | `EACCES` |
| Enumerate `.aros` | `EACCES` |
| Write workspace root | `EACCES` |
| Write `isolated-output/result.txt` | succeeded |
| Create network socket | `EPERM` |
| Signal a process | `EPERM` |
| Fork | `EPERM` |
| Start thread | rejected |
| Read `AROS_SECRET_SENTINEL` from environment | absent |

The run exited `completed`; its final receipt binds
`security_profile=isolated-linux`, `network_policy=deny-all`,
`process_policy=single-process-no-threads`, the writable path, environment
policy, limits, manifest and launch hashes, and stdout/stderr hashes.

An earlier smoke revealed that a Landlock root `READ_DIR` rule allowed names
inside `.aros` to be listed even though file content was denied. The root rule
was removed and replaced with explicit non-sensitive subtree rules; the
corrected smoke above proves `.aros` enumeration is now denied.

A final read-only Claude review found two additional pre-commit issues. The
seccomp policy now denies all io_uring setup/enter/register calls to prevent
network bypass, and any failure to drop a capability bounding-set entry now
fails closed instead of ignoring `EPERM`. Both have regression tests.

## Evidence hashes

```text
manifest  8ef300dfcc650809234be78770a820dd0c406d5775c4ccee3553c9547e58745f
final     c78ee84366c7df75e5837ae08a7f84da949b2b44c3731582e538b69920343c73
stdout    3d1af7f1a0729bf2e5fce98328d4d8f53ce0c0372e529c659c25e189e0747bac
```

## Exit result

M3 establishes a positively tested local isolation boundary for single-process
Linux experiments and makes unsafe local execution an explicit opt-in. M3 does
not yet establish evaluator independence; deterministic admission is the next
module.
