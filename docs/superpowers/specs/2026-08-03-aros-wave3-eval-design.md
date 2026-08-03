# AROS Wave 3 Evaluation Design

Status: approved implementation refinement  
Date: 2026-08-03  
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`  
Parent design: `2026-08-02-aros-v1-product-and-migration-design.md`

## 1. Goal

Wave 3 adds deterministic visible evaluation and protected admission without
moving scientific judgment into the kernel or creating a third process,
worktree, or recovery stack.

The Principal chooses the evaluator, candidate, controls, and interpretation.
AROS freezes exact inputs, executes the apparatus, parses one declared machine
output, publishes factual receipts, enforces disclosure, and reports `lost`
without retrying.

## 2. Non-negotiable decisions

1. Visible evaluation composes `RunService`; Eval never owns tmux, process
   status, stop arbitration, or process-final receipts.
2. Protected admission is foreground. Hidden file descriptors never cross
   tmux or appear in a Run manifest.
3. Lost evaluation never relaunches with the same idempotency key. A retry is a
   new Principal action with a new key and a new evaluation ID.
4. The parser accepts exactly one bounded JSON metric document. Worker prose,
   thresholds, and Agent opinion cannot set the measurement.
5. There is no `admitted`, `rejected`, `better`, or `worse` scientific verdict.
   `admit` means permission to invoke protected apparatus only.
6. Candidate and apparatus are materialized at separate exact commits. Git
   hooks, ambient config, fsmonitor, and checkout filters cannot change bytes.
7. Dirty or ambiguous worktrees are preserved. Cleanup never force-discards
   unexplained material.
8. The experimental `aros-m4-hardening` branch is a historical source of pure
   validators and adversarial tests only. No commit or complete module is
   cherry-picked.

## 3. Staged architecture

### 3.1 Mechanical prerequisites

Wave 3 first closes the known Run launch/reconcile transition race by
serializing public reconciliation with the per-run lifecycle lock. Runner
bootstrap remains a strict single read; retrying it could accept stale `lost`
state and launch anyway.

Only two general mechanical modules are extracted before visible Eval:

- `receipts.py`: pure self-hash and content-receipt helpers;
- `worktrees.py`: pinned Git repository/checkout binding, exact detached clean
  materialization, validation, and clean-only removal.

`store.py` remains the persistence authority. Run and Task retain their state
machines, locks, recovery, error types, and service-specific schemas.

### 3.2 Visible flow

```text
Principal selects versioned evaluator + exact candidate commit
  -> Eval freezes request and creates a bound candidate/apparatus execution bundle
  -> RunService executes the scorer in isolated-linux
  -> Run owns process identity, logs, timeout, stop, final, and lost truth
  -> Eval verifies Run receipt and parses exact scorer stdout
  -> Eval publishes one create-once measurement receipt
  -> Principal interprets the receipt
```

Eval may derive status from its request, Run ID, Run state, and measurement
receipt. It does not mirror Run process state into a second authority.

The execution bundle is one directory below `.worktree/eval/<eval-id>/` with
separate exact `candidate/` and `apparatus/` detached checkouts. Its binding
records both Git directories, commits, and tree hashes. Run receives the bundle
root, a cwd below `candidate/`, exact argv that references the apparatus by a
bundle-relative path, and both roots as read-only isolation inputs. Neither
checkout is writable; temporary files live in a separate ephemeral Run temp
root. Runner revalidates the bundle immediately before process launch, while
Run control state and logs remain in the primary workspace.

### 3.3 Protected flow

Protected target/scorer execution is sequential and foreground:

```text
reserve disclosure + acquire exclusive activity gate
  -> exact candidate target emits output to an anonymous broker-owned FD
  -> target process group is fully reaped; output is sealed, fsynced, and hashed
  -> exact trusted scorer receives sealed output + hidden inputs as read-only FDs
  -> strict parser consumes scorer stdout
  -> external protected store publishes the sole protected receipt
  -> public status projects only disclosure-approved aggregate fields
```

The `EvalService.admit` call itself is the broker. It holds one per-evaluation
execution lock for the entire call and invokes target/scorer only through the
shared process primitives. It catches known process/parser failures and
publishes their factual receipt. A hard broker exit releases the lock; absent a
receipt, status becomes `lost` and no caller may take over that request.

Tmux is not used for protected phases because an existing tmux server cannot
reliably transport arbitrary hidden FDs. Before protected admission is exposed,
one small synchronous `processes.py` is extracted and the existing Run runner
is migrated to it in the same behavior-preserving commit.

`processes.py` owns only:

- exact `Popen` arguments with `shell=False`, `start_new_session=True`, and
  `close_fds=True`;
- declared `pass_fds`;
- PID/PGID/start-token capture and verified liveness;
- TERM/KILL signalling and leader reaping;
- parent-death signal installation for foreground protected children.

It owns no carrier, lifecycle state machine, heartbeat, receipt, retry,
worktree, parser, disclosure, or scientific policy. Run timeout/stop precedence
stays in `runner.py`; target/scorer sequencing stays in the protected broker.
Task subreaper/cgroup work remains outside this extraction.

## 4. Records and identity

### 4.1 Versioned evaluator

A visible evaluator manifest is an ordinary tracked file under:

```text
eval/suites/<evaluator-id>/<version>/manifest.json
```

It strictly records:

- evaluator ID and version;
- visibility (`visible` or `protected`);
- exact apparatus commit and frozen apparatus file hashes;
- exact scorer command and working directory;
- protected target command when applicable;
- explicit inputs or opaque protected handles;
- environment reference, seed policy, resource limit, timeout, and success
  exit codes;
- raw stdout/stderr policy;
- one metric name, parser version, numeric range, and minimum sample count;
- disclosure policy, limitations, and calibration references.

Unknown and missing fields are rejected. Protected full manifests live only in
the protected store; the project receives an opaque versioned descriptor with
apparatus and disclosure hashes.

### 4.2 Evaluation request

The evaluation ID is deterministically derived from the full SHA-256 of the
idempotency key. A create-once `request.json` binds:

- evaluator descriptor hash;
- candidate and apparatus commits/trees;
- exact commands and cwd;
- actor, environment, seed, limits, and created time;
- request self-hash.

A separate create-once local execution claim records broker PID/start token and
holds a per-evaluation flock during the sole attempt. The claim never transfers
or authorizes relaunch. A create-once `run.json` separately binds the request
hash, Run ID, Run manifest hash, and execution-bundle hash. While the visible
Eval execution lock is held, status reports its current Run/finalization facts.
Once that lock is released without a measurement receipt,
`evaluation_state=lost` immediately, regardless of whether the linked Run is
prepared, running, or terminal. `referenced_process_state` continues to report
the Run independently for observation and audit.
For protected Eval, a released execution lock with no protected receipt is
`lost`.

The same key with the same request returns the existing state. The same key
with different inputs is rejected. A lost request remains lost forever.

Visible launch ordering is fixed:

1. publish and validate `request.json`, then acquire the sole execution lock;
2. materialize and validate the execution bundle;
3. call `RunService.prepare` without launching;
4. publish `run.json` with the exact Run and bundle hashes;
5. call `RunService.start` and wait/observe through RunService.

A crash before step 4 may leave a prepared, unlinked Run, which remains
discoverable for audit but is never started or adopted automatically. A crash
after step 4 leaves an exact linked prepared/running Run. In either case the
evaluation request is immediately `lost` after its lock is released; a linked
Run keeps its independent process state, and the same key may not prepare,
launch, attach, or finalize a second evaluation attempt.

### 4.3 Metric document

Scorer stdout contains exactly one UTF-8 JSON object:

```json
{"schema_version": 1, "metric": 0.73, "sample_count": 20}
```

The parser rejects prose, multiple documents, duplicate/extra/missing keys,
booleans, NaN/infinity, unbounded integers, oversized output, out-of-range
metrics, and negative or non-integral sample counts. Diagnostics belong on
stderr.

`minimum_samples` yields the mechanical state `underpowered`; it never yields a
scientific pass/fail judgment.

## 5. State and recovery

Evaluation lifecycle, referenced process truth, and measurement truth remain
separate:

```text
evaluation_state:
  prepared | running | finalizing | completed | lost

referenced_process_state:
  prepared | launched | running | completed | failed_process |
  timed_out | cancelled | lost

measurement_state:
  valid | underpowered | invalid_eval | not_available
```

Terminal measurement receipts use `evaluation_state=completed`. Their valid
process/measurement pairings are:

- `completed` with `valid`, `underpowered`, or `invalid_eval`;
- `failed_process`, `timed_out`, or `cancelled` with `not_available`.

`evaluation_state=lost` has no terminal measurement receipt. Its status may
still report the last referenced Run state, including `completed`, together
with `measurement_state=not_available`; this is an operationally lost
evaluation, not a contradiction or a scientific result.

Visible process truth is the referenced Run receipt. Protected process truth is
the foreground broker/phase identities plus the single external protected
receipt. A dead holder with no receipt is `lost`. Recovery may repair a derived
pointer from an existing receipt; it may not parse a new measurement, create a
terminal receipt, or launch another process.

## 6. Protected authority and disclosure

The protected store is outside the Principal-visible workspace and requires an
explicit administrator capability. It owns:

- full protected evaluator manifests;
- registered input handles and inode/content identities;
- sealed target/scorer raw output;
- atomic disclosure reservations;
- the sole protected terminal receipt.

Disclosure is aggregate-only in v1. Public APIs never expose protected paths,
file descriptors, raw hashes that enable dictionary comparison, hidden item
output, scorer stdout/stderr, or full protected manifests.

A disclosure unit is reserved before protected handles are resolved or opened.
Same-request replay reuses the reservation; lost requests retain it. Budget
denial is a factual pre-launch error, not a measurement.

All AROS-mediated trusted-local Run, Task, and Principal shell execution takes
a shared activity lock; protected admission takes the lock exclusively. This
prevents ordinary AROS tool misuse, not arbitrary hostile same-UID processes.

## 7. Isolation and FD discipline

For protected target/scorer phases:

- target can read only its exact candidate checkout and system runtime;
- target stdout is an anonymous broker-owned FD and reveals no store path;
- the broker seals target output only after the target process group is gone;
- target is fully gone before scorer starts;
- scorer can read only exact apparatus plus declared inherited FDs;
- scorer receives no candidate checkout path;
- hidden inputs use `O_RDONLY|O_NOFOLLOW` and are revalidated by inode/hash;
- procfs is not readable, so inherited FD backing paths cannot be discovered;
- stdin is `/dev/null`; only declared FDs survive `close_fds=True`;
- Landlock, seccomp, no-new-privileges, capability drop, and resource limits
  fail closed;
- foreground children use `PDEATHSIG` with the post-`prctl` parent check;
- protected pre-exec performs all credential/capability changes first, installs
  `PDEATHSIG=SIGKILL`, rechecks the expected parent, then installs
  no-new-privileges, Landlock, and seccomp;
- seccomp denies later `prctl`, fork/clone/vfork, unshare/setns, and setsid so
  untrusted code cannot clear the death signal or escape the phase boundary;
- unconfirmed reap means no terminal receipt.

The v1 claim covers isolated children and ordinary AROS-mediated tool misuse.
It does not protect against root/kernel compromise, arbitrary hostile same-UID
processes, remote multi-tenancy, or complete GPU isolation.

## 8. Public system calls

One `EvalService` backs Principal, CLI, and later MCP adapters:

```text
register  validate/freeze a visible recipe or install a protected descriptor
run       foreground visible evaluation via RunService
admit     foreground protected target/scorer measurement
status    return receipt/process facts; never retry or interpret
observe   bounded visible stdout/stderr only
audit     validate hashes/lineage without repair or interpretation
```

The target CLI is:

```text
aros eval register|run|admit|status|observe|audit
```

Protected registration/audit require administrator authority. `admit` is not
an acceptance decision. MCP parity is Wave 6 and must reuse these exact schemas.

## 9. Salvage boundary

The following M4 ideas may be reimplemented with current abstractions and
ported adversarial tests:

- strict finite scalar parser;
- apparatus blob freezing and hook/filter rejection;
- protected store, FD pinning, and aggregate disclosure;
- evaluator-specific Landlock FD rules;
- malicious target/scorer and hidden-leak tests.

The following are prohibited imports or ports:

- attempt indexes, transition history, supersession, and automatic retry;
- Eval-owned subprocess/worktree/cancellation/finalization stacks;
- M4 EvalService, EvalTool, CLI, or recovery engine wholesale;
- changes to frozen legacy modules;
- M4 store code that predates current create-once durability;
- semantic thresholds, ranking, admission verdicts, or automatic assimilation.

## 10. Delivery gates

### Gate A: mechanical seams

- launch/reconcile transition is serialized and runner bootstrap stays strict;
- receipt extraction is byte-for-byte compatible;
- attached Task and new detached Eval checkout tests pass;
- Run can execute in a two-checkout bound execution bundle, revalidate both
  exact trees, and keep control receipts in the primary workspace.

### Gate B: visible evaluation

- strict registration/request/parser/receipt tests pass;
- exact candidate and apparatus commits are revalidated before execution;
- Eval contains no direct `Popen`, tmux, `killpg`, or process-final writer;
- process failure, invalid eval, underpowered, valid negative, and lost are
  distinct;
- same lost key never launches again;
- real CLI smoke records Run and measurement receipt hashes.

### Gate C: protected prerequisites

- Run behavior is unchanged after `processes.py` extraction;
- broker-death, timeout, TERM/KILL, FD survival, and reap tests pass;
- an adversarial child cannot clear `PDEATHSIG`; killing the broker leaves no
  live target/scorer descendant and no false terminal receipt;
- activity lock and disclosure reservation are atomic and fail closed;
- protected isolation tests prove path/FD separation.

### Gate D: protected admission

- malicious target cannot read hidden data, alter scorer output, signal scorer,
  or weaken exact apparatus;
- public projection is aggregate-only and protected observe is rejected;
- concurrent requests cannot exceed disclosure budget;
- lost does not refund or retry;
- real protected smoke records exact commits, hashes, leakage scan, and threat
  limits.

Every gate requires RED-GREEN tests, focused regression, a real behavior smoke,
full regression, exact evidence, spec review, and quality/security review.

## 11. Explicit non-goals

- evaluator daemon or eval-specific tmux carrier;
- attempt history or automatic retry;
- arbitrary parser plugins or multiple primary metrics;
- item-level protected disclosure;
- semantic pass/fail, ranking, optimization, or assimilation;
- automatic anomaly Agent or polling farm;
- generic Operations framework, backend plugin system, or cluster scheduler;
- hostile same-UID or root security claim.
