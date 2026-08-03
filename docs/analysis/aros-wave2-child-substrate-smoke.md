# AROS Wave 2 Child Task Substrate Smoke Evidence

This record commissions the public AROS child-task substrate against real Git,
tmux, process, filesystem, message, collection, and prune state. It records one
preserved environmental failure followed by a successful rerun after the
filesystem capability became explicit. It does not interpret a child result as
scientific truth and does not claim that `trusted-local` is a sandbox.

## Result

- Successful task base B: `e4db5f5602fca83dfd3003fc32fd36458feee06c`
- B tree: `e265d650584ca5f164c24388c3a004c0c2851f3d`
- Brief-only parent P: `cedeeb36af545c0ed51c0004f05fc49e1737fedf`
- P tree: `712cf9ed6f7670859a3fa5ecd3132bc602f5c14b`
- Read task C/R: B / `ddb1106473b620051e8c1b46f1a8c2945cf1f560`
- Write task C/R: `a20a46a9d6f077d90c03ac63521a4909383c09a2` /
  `26c7fe0ffbaafd1f80ee694446ea12c55b68d822`

Both adapters survived their launcher clients, ran concurrently in distinct
owned worktrees, produced strict B-C-R returns, and completed with exit code 0.
Collection changed no parent commit, tree, ref, or semantic AROS file. The
Principal rejected the synthetic write commit, no artifact was assimilated,
dirty prune failed closed without deleting anything, and clean prune removed
only the two successful worktrees while retaining both task branches at R.

## Source, entry, and environment

```text
Evidence time:       2026-08-03T09:05:16Z
Worktree:            /workspace/Arbor/.worktree/aros-wave2-child
Branch:              aros-wave2-child
Host:                Linux 6.8.0-124-generic x86_64 GNU/Linux
Workspace filesystem: FUSE device 57
Python:              Python 3.12.3
tmux:                tmux 3.4
Installed entry:     .venv/bin/aros
Entry SHA-256:       c46eecfe36059ae57346a79661d6c1619de3263071d05e1902d2538c9c632647
```

The installed entry shebang resolves to this worktree's `.venv/bin/python` and
imports `arbor.cli.aros_app:main`. Clean-environment import resolution was:

```text
arbor                         src/__init__.py
arbor.aros.store              src/aros/store.py
arbor.aros.tasks              src/aros/tasks.py
arbor.aros.task_runner        src/aros/task_runner.py
arbor.aros.task_tool          src/aros/task_tool.py
arbor.aros.principal          src/aros/principal.py
arbor.cli.aros_app            src/cli/aros_app.py
arbor.cli.commands.aros_cmd   src/cli/commands/aros_cmd.py
```

Every public AROS invocation used exactly this ambient environment:

```bash
env -i \
  HOME=/tmp/aros-wave2-task6-home \
  PATH=/usr/bin:/bin \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  TZ=UTC
```

No provider, mock, Arbor CLI, `uv` command, shell-interpolated adapter argv,
force cleanup, `git clean`, `git reset`, merge, or cherry-pick was used. The
adapters used only `/usr/bin/python3`, Python's standard library, and subprocess
argv lists. Their stdout and stderr receipts are both the empty-byte SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

The adapter Git commit argv supplied explicit `user.name` and `user.email` but
did not pass hook or GPG-signing overrides. Fresh observation found no configured
`core.hooksPath`, no configured `commit.gpgSign` (therefore Git's default false),
and no executable non-sample file under `/workspace/Arbor/.git/hooks`. This is
an observed repository/environment fact, not a claim that the commit commands
disabled hooks or signing themselves.

## Public CLI contract and exact actions

Clean-environment `aros --help`, `aros task --help`, and
`create|start|status|list|message|collect|preserve|prune --help` exited 0; the
task-level help also exposed `stop`. The final create contract was:

```text
Usage: aros task create [OPTIONS] -- ADAPTER [ARGS]...
```

Both immutable briefs were created before either was committed. The exact
option values were:

```text
read objective:  Inspect the Wave 2 v2 child-task base without modifying product material
read mode:       read_only
read key:        aros-wave2-task6-v2-read-e4db5f5602fca83dfd3003fc32fd36458feee06c
write objective: Write one synthetic Wave 2 v2 child-task artifact for principal review
write mode:      write
write key:       aros-wave2-task6-v2-write-e4db5f5602fca83dfd3003fc32fd36458feee06c
common:          --timeout-seconds 120 --shell --actor principal
deliverable:     tasks/TASK-ID/return.json
acceptance:      strict B-C-R topology and self-hashed return
adapter argv:    /usr/bin/python3 -c CODE
```

The create subprocess argv order was:

```text
aros task create --cwd ROOT --objective OBJECTIVE --mode MODE --idempotency-key KEY --timeout-seconds 120 --shell --deliverable tasks/TASK-ID/return.json --acceptance "strict B-C-R topology and self-hashed return" --actor principal -- /usr/bin/python3 -c CODE
```

`CODE` above is not a shell substitution. It is the byte-exact third
`adapter_argv` member frozen in each versioned brief. The read code is 6,851
bytes with SHA-256
`35cf3dfcea675f9cb683b933941a7fd79ebfec35498ef19f79cb8f57ce981e60`;
the write code is 6,846 bytes with SHA-256
`8e8c32b13de628906613a763eb64e32df9c85e607a0feae585fd3f29f2b0f93d`.
This preserves the exact executed argv without presenting an inaccurate shell
transcription.

The public state-changing calls were equivalent to these literal argv forms:

```text
aros task start READ_ID  --cwd ROOT --actor principal
aros task start WRITE_ID --cwd ROOT --actor principal
aros task message READ_ID  --message "Record the exact B tree, pyproject hash, and tracked-path count." --actor principal --cwd ROOT
aros task message READ_ID  --message "Return no product changes; C must equal B." --actor principal --cwd ROOT
aros task message WRITE_ID --message "Produce only the synthetic artifact, then a separate strict return commit." --actor principal --cwd ROOT
aros task collect READ_ID  --cwd ROOT
aros task collect WRITE_ID --cwd ROOT
aros task message WRITE_ID --message "Rejected synthetic child commit a20a46a9d6f077d90c03ac63521a4909383c09a2: commissioning-only artifact; do not assimilate or cherry-pick." --actor principal --cwd ROOT
aros task preserve READ_ID --cwd ROOT
aros task prune READ_ID    --cwd ROOT
aros task prune WRITE_ID   --cwd ROOT
aros task status READ_ID   --cwd ROOT
aros task status WRITE_ID  --cwd ROOT
```

Here `ROOT` is the worktree path above, `READ_ID` and `WRITE_ID` are the exact
IDs below, and the executable in every line was the absolute installed
`.venv/bin/aros`. A temporary 31,535-byte orchestration driver, SHA-256
`fab42b832f63d7ec08d5cc2790cc0f2c26a0ce90d57b595109852a12063a7cf0`,
issued those calls with subprocess argv lists. It and both gate files were
removed after evidence capture.

## Preserved negative operational evidence

The supplied pre-initialization HEAD was
`14460771f78cd9a995b3cc15b551a33825f79297`, tree
`b86a3ed8a620384ac22e9a128565f511046a5e97`. It had 433 tracked paths,
no `AROS.md`, and no `/.aros/` or `/.worktree/` ignore entries. Public
`aros task list` therefore exited 2 with `AROS mission does not exist`.

Public `aros init` used the mission:

```text
Implement and validate Agent-centric AROS while preserving the Design Book invariants.
```

It created only `AROS.md` and `memory/NOW.md`, preserved the existing
`AGENTS.md` byte-for-byte at SHA-256
`2ac842a39e9f0afb23b4e93adb2a681800191568d08ec761b4624ad8b66b20e7`,
and appended only the two required ignore entries. The independent initialization
commit/new original base was `b20bf3249a06002e6c33e598650186cb60f8d032`,
tree `e38618280d9aa365204139c92ef08b82f60ea68e`.

The first brief-only P was
`e4cbd32d14581ecb6168015745178a426c49711e`. Its real launch attempt exposed
that this FUSE filesystem normalized requested mode 0600 to 0666. A separate
mode probe observed `before=666`, `after chmod=666`, and
`fstat/lstat after fchmod=0o666`; the probe was removed. The then-current runner
failed closed before publishing any launch record:

```text
error: task stdout log must be a restrictive plain file: .../stdout.log
```

Those two tasks were never retried or cleaned:

| Preserved task | State | Brief hash | Ownership hash | Branch HEAD |
| --- | --- | --- | --- | --- |
| `TASK-20260803-inspect-the-wave-2-child-task-ba-f51c` | `worktree_ready` | `9171faaece414a05cbff778eb90eca79066af9cc9547c73fd766bf33cb4952da` | `9ff23c439d2f29a5b1ea04cae9dc993583322f1311abde6bd9fdacf46b7b4489` | `b20bf3249a06002e6c33e598650186cb60f8d032` |
| `TASK-20260803-write-one-synthetic-wave-2-child-57f7` | `worktree_ready` | `3009d2bd4096a7d6ed73dc4d4ea17680fd8c0e5135044ee606f2cf8a616cde7e` | `10cc26cc26ad4a590ea16a3c058b557b4a88bd8503dc7f10db80c04f081ef1c2` | `b20bf3249a06002e6c33e598650186cb60f8d032` |

Both owned worktrees remain registered and completely clean. Both lack
`launch.json`, `execution.json`, `adapter.json`, and `final.json`. The read
runtime retains its zero-byte mode-0666 `stdout.log`, SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
The write runtime never reached log creation.

Two independent isolated-linux RunService inspections of those worktrees also
remain versioned as factual process failures, not scientific negative results:

| Run | Manifest hash | Manifest file SHA-256 | Final file SHA-256 | Result |
| --- | --- | --- | --- | --- |
| `RUN-20260803-075733-check-inspect-task-worktree-8849` | `06e8b98129f0b2f8ea36fa79b73d1967be01b3017e6a456a7e92eb2716152380` | `4079ae9e744a88dafcc99e5dca8dac3bea45a5c25dd210e9d930933455590ac9` | `7b1ad4bc8e08dea1722ad85923b5d8315b6a93a5e1fcf9d7dbd7152e3f3ed9e8` | `failed_process`, no exit code, `Exception occurred in preexec_fn` |
| `RUN-20260803-075735-check-write-task-worktree-b583` | `7785ac86bb71c2c6c82d5155d99e65ab6646d5283078b16377074b4271053fec` | `56e65fe365d7908f7fb0db996065473ab84e5975b6a92cfdc000253a8d4c25cf` | `202b39da52ae3099942989638f08209c7662a083f17c13ba2601288dd5cd6732` | `failed_process`, no exit code, `Exception occurred in preexec_fn` |

The permission capability and create-once storage fixes, plus these negative
receipts, were checkpointed at successful base B. The successful rerun used new
task IDs and idempotency keys.

## Immutable v2 briefs and P

| Field | Read task | Write task |
| --- | --- | --- |
| Task ID | `TASK-20260803-inspect-the-wave-2-v2-child-task-f920` | `TASK-20260803-write-one-synthetic-wave-2-v2-ch-5093` |
| Mode | `read_only` | `write` |
| Request hash | `1c3891c95ce1ff13cd911a2a78b405a83a6b8a83c9d9211eb9063fcacb280791` | `42580db2c08d6edae11034832798b735c5e7ed7ea8c0fc81488d322e27d1e794` |
| Brief hash | `16e8cfa8a5b9b4b2b3a7d09a594e14a6ed5c42e1d059076e30a4a902e6d43ffe` | `d9cb2025bc317f9830c097c34aa0a09b99408a15551667e70974573c71ca14ed` |
| Brief file SHA-256 | `fc716aec03fd8d63d188ebe02d258172b38f7a3daac235c425e900f7d8a40ccd` | `6c0f854c655ea6d45e1f30bdbc37b9288c6da043ffccaa39756a0e5b635bc5d7` |

Both briefs bind actor `principal`, base B, `network=false`, `shell=true`, and
timeout 120 seconds. Immediately before create, B was clean and had 442 tracked
paths. P has B as its single parent and changes exactly:

```text
tasks/TASK-20260803-inspect-the-wave-2-v2-child-task-f920/brief.json
tasks/TASK-20260803-write-one-synthetic-wave-2-v2-ch-5093/brief.json
```

## Concurrent launch and filesystem capability

Both public start clients were launched concurrently. They exited 0 as PIDs
`3666062` and `3666063`. After both clients had been reaped, `/proc` still held
both adapter PIDs and fresh public status calls observed both tasks as
`running`.

| Identity | Read task | Write task |
| --- | --- | --- |
| Worktree | `.worktree/tasks/TASK-20260803-inspect-the-wave-2-v2-child-task-f920` | `.worktree/tasks/TASK-20260803-write-one-synthetic-wave-2-v2-ch-5093` |
| Ownership hash | `2fbb11b5d73ce4b21271bdf142f614d9c5927e580b517c90f8300bb996b7139f` | `9e17553034fabe28efa12c99e6e40f58069bd3ac95d79efe818da9f200d9f697` |
| Launch hash | `27b8fae142e8bd669bc9665a344f5103e0fef3ab7762ae113629a5e471b0cb46` | `5950e6e5905ce2b453ba3978ceffb1eb6b2955a2429e9dd1a2fa45627222dce9` |
| Runner PID/PGID | `3668004` / `3668004` | `3668322` / `3668322` |
| Runner start token | `linux-proc-start:291223441` | `linux-proc-start:291223571` |
| Execution hash | `a0be9e7b0bf61b3790a69e0041fcad07bd2a6b7218a4d806595fe0ecebecf1d5` | `a637a410fabeefa2baf89dd81a829e41664f14ad5d98c509f5f3cd84616f681f` |
| Adapter PID/PGID | `3668183` / `3668183` | `3668496` / `3668496` |
| Adapter start token | `linux-proc-start:291223506` | `linux-proc-start:291223641` |
| Adapter hash | `220143fa45b8ff35313c6c074940e4df59c375af35a43f1e942748f4e70f022e` | `6832b8cd8738fffb6c08fc6d1dbe001a69d49cab4946436b4f4aaa103a7f8109` |

Every launch recorded the same exact five-field capability:

```json
{
  "device": 57,
  "enforced": false,
  "mode_request_supported": true,
  "observed_mode": 438,
  "requested_mode": 384
}
```

The launch-level `filesystem_permissions_enforced` is `false`. Both immutable
final receipts copy that boolean and probe exactly. This is an explicit trusted
local limitation: mode requests succeed but this FUSE mount normalizes 0600 to
0666, so the Task service relies on its regular-file, single-link, stable-inode,
containment, schema, lineage, and hash checks rather than claiming permission
isolation.

Each `ownership.json` is a durable, non-expiring worktree ownership claim; it
can be released only by explicit clean prune. Each create-once `execution.json`
is the separate local execution lease, binding that ownership and launch to the
runner host, PID/PGID, process start token, claim time, and execution hash before
the adapter gate opens. Liveness is recovered from that exact identity. A dead
holder without a final receipt becomes `lost`; it neither transfers ownership
nor permits a second launch. Time-based/distributed leases are deferred to the
Operations wave.

## Messages and adapter gates

The messages were create-once files with contiguous 20-digit names and a strict
hash chain:

| Task | File | Message hash | Previous hash |
| --- | --- | --- | --- |
| Read | `00000000000000000001.json` | `7f4a2aa13802769788ad4948948fe12b444e30412db15742e794ff2694b8494b` | `null` |
| Read | `00000000000000000002.json` | `2b305cd6ce9b15b0ff9d89d6028ba33c2552f67cbfab96b43eaf591d58e039c2` | `7f4a2aa13802769788ad4948948fe12b444e30412db15742e794ff2694b8494b` |
| Write | `00000000000000000001.json` | `30c1d82f06dceb60cf9403331ce4c3fee22440ea8aa94613115dc9ee1257d200` | `null` |
| Write decision | `00000000000000000002.json` | `45b926277ae8ff9feec71c8d38ca2ffea5c4b5adec4d80bba8108a51f42b98cc` | `30c1d82f06dceb60cf9403331ce4c3fee22440ea8aa94613115dc9ee1257d200` |

Each adapter first validated all five injected `AROS_TASK_*` values against its
brief, recomputed the brief hash, verified its exact cwd/branch/B and complete
cleanliness, then waited up to 60 seconds for its distinct
`/tmp/aros-wave2-TASK-ID.go` gate. Both gates contained exactly `go\n`, were
published only after concurrent running/message evidence, and were removed
after both final receipts were validated.

The mailbox records were not used as a delivery or steering channel. The
separate release gates controlled adapter progress, so this commissioning proves
ordered create-once message persistence and hash chaining only; delivery and
acknowledgement are explicitly not claimed.

## B-C-R results

The read adapter inspected B without a product commit:

```text
tree:               e265d650584ca5f164c24388c3a004c0c2851f3d
pyproject SHA-256:  ff6b52e28b7e42001ea3d97c33cfaff60f8962fdff49471cc93b8fa4703ff672
tracked paths:      442
C:                  e4db5f5602fca83dfd3003fc32fd36458feee06c (C = B)
R:                  ddb1106473b620051e8c1b46f1a8c2945cf1f560 (R^ = C)
changed_files:      []
return hash:        0cba20c036e707f923c19fce8cc370ae288e75a12356647137a8905c87e7c58d
```

The write adapter committed only the exact bytes
`AROS Wave 2 Task 6 synthetic artifact\n` at
`artifacts/aros-wave2-task6.txt`, SHA-256
`f6ff59b3b27cafffb8e0b60ebbf682356393c2c1d436c8a1d5eeb8497fa43383`:

```text
C:                  a20a46a9d6f077d90c03ac63521a4909383c09a2 (C^ = B)
R:                  26c7fe0ffbaafd1f80ee694446ea12c55b68d822 (R^ = C)
changed_files:      ["artifacts/aros-wave2-task6.txt"]
return hash:        ee33db0d02dd64709edee5d5d976ce25f59f0972bad8f2df40dfb728e273a3e1
```

Each R is a single-parent commit that changes only
`tasks/TASK-ID/return.json`; both return tree entries are mode 100644. The read
return blob is Git object `2f3e42f1b85af9e73c2578d6d6c000cd32016261`;
the write return blob is `36df040db38414fad44d03934b77ac3b26930df8`.
Both child worktrees were completely clean at R. The final hashes are
`9ba54a932a0016dc72834728ebe1c4d7a1f787a69711232582347ea70dfd97c0`
and `b7e13d769feac1f0699c3cba1bfd9c2d2c41312326f23027fd8eeec74df13b43`.

## Collection, decision, and no assimilation

Immediately before collect, the parent was clean at P/tree P, both branches
were at R, and `artifacts/aros-wave2-task6.txt` was absent. Collection produced
only these untracked versioned records while keeping HEAD, tree, all refs, and
semantic files fixed:

| Task | Collection hash | File SHA-256 |
| --- | --- | --- |
| Read | `9d3e7877e4736247470d8074a070aa00af5a2a9bbb8e7f713b89e59a2f2853c8` | `0e16e9854ae1b2018012a204eb27dbafd619a70f9fdc98fe26e41070024645ef` |
| Write | `1d81b289bb064b663c00b048d82915ee6996ce726d0b0320c59eca4b094e093b` | `4fd5ceee5fd904cf29da477f80fa8313270450734545f0fbd07f19a1b6176774` |

The stable semantic file hashes around collect were:

```text
AROS.md       3b11869ad56c84e919eb2904fc4029ac60f43742b31c68df56abf9086b679495
memory/NOW.md d33fd4e1d2fce6841c341b19b6a8ec1871f7e2bcb025388e7448a55588e8c403
```

The Principal rejected read apply as a no-op because C=B. It explicitly
rejected write C in the attributed decision message shown above because the
artifact was commissioning-only and must not be assimilated or cherry-picked.
No merge or cherry-pick occurred, parent HEAD/tree remained P, and the artifact
remained absent.

## Dirty preservation and prune

After collection, one known untracked sentinel was added to the read worktree:

```text
path:    aros-wave2-task6-dirty-sentinel.txt
bytes:   62
SHA-256: 4f2486f225cb2a521a748863217f4abcd2414b0515bfb363658bac4837d3d766
```

Public preserve returned `clean=false`. Public prune exited 2 with:

```text
error: task worktree is not completely clean: /workspace/Arbor/.worktree/aros-wave2-child/.worktree/tasks/TASK-20260803-inspect-the-wave-2-v2-child-task-f920
```

The sentinel bytes/hash, worktree registration, and read branch HEAD R all
remained intact. Only the sentinel was then removed, using the same patch
mechanism that created it. Preserve returned `clean=true`, after which public
prune succeeded for both tasks:

| Task | Prune hash | Pruned hash | Retained branch tip |
| --- | --- | --- | --- |
| Read | `e96b5a1d42f5f08673fd566cc753c30b076a1a3da4effd43f5494577f6d2581d` | `bc244a451630b993f35f17482006a1aad69d9a601f5b7dbaa337b49ac84f050e` | `ddb1106473b620051e8c1b46f1a8c2945cf1f560` |
| Write | `04d87ec0dbdf13a7ca93a7320be88f30611a88564b4a867d9b952b0191b80b01` | `7a855974d585abd02953788d12f83ec6c40b7e311957e8439c7110524bd846ce` | `26c7fe0ffbaafd1f80ee694446ea12c55b68d822` |

Fresh public status for each task returned its strict pruned receipt byte-for-
field, and both successful worktree paths were absent. The original failed
worktrees remain registered and untouched.

## Authoritative hash ledger

| Record | Read hash | Write hash |
| --- | --- | --- |
| Request | `1c3891c95ce1ff13cd911a2a78b405a83a6b8a83c9d9211eb9063fcacb280791` | `42580db2c08d6edae11034832798b735c5e7ed7ea8c0fc81488d322e27d1e794` |
| Brief | `16e8cfa8a5b9b4b2b3a7d09a594e14a6ed5c42e1d059076e30a4a902e6d43ffe` | `d9cb2025bc317f9830c097c34aa0a09b99408a15551667e70974573c71ca14ed` |
| Ownership | `2fbb11b5d73ce4b21271bdf142f614d9c5927e580b517c90f8300bb996b7139f` | `9e17553034fabe28efa12c99e6e40f58069bd3ac95d79efe818da9f200d9f697` |
| Launch | `27b8fae142e8bd669bc9665a344f5103e0fef3ab7762ae113629a5e471b0cb46` | `5950e6e5905ce2b453ba3978ceffb1eb6b2955a2429e9dd1a2fa45627222dce9` |
| Execution | `a0be9e7b0bf61b3790a69e0041fcad07bd2a6b7218a4d806595fe0ecebecf1d5` | `a637a410fabeefa2baf89dd81a829e41664f14ad5d98c509f5f3cd84616f681f` |
| Adapter | `220143fa45b8ff35313c6c074940e4df59c375af35a43f1e942748f4e70f022e` | `6832b8cd8738fffb6c08fc6d1dbe001a69d49cab4946436b4f4aaa103a7f8109` |
| Final | `9ba54a932a0016dc72834728ebe1c4d7a1f787a69711232582347ea70dfd97c0` | `b7e13d769feac1f0699c3cba1bfd9c2d2c41312326f23027fd8eeec74df13b43` |
| Message 1 | `7f4a2aa13802769788ad4948948fe12b444e30412db15742e794ff2694b8494b` | `30c1d82f06dceb60cf9403331ce4c3fee22440ea8aa94613115dc9ee1257d200` |
| Message 2 / decision | `2b305cd6ce9b15b0ff9d89d6028ba33c2552f67cbfab96b43eaf591d58e039c2` | `45b926277ae8ff9feec71c8d38ca2ffea5c4b5adec4d80bba8108a51f42b98cc` |
| Return | `0cba20c036e707f923c19fce8cc370ae288e75a12356647137a8905c87e7c58d` | `ee33db0d02dd64709edee5d5d976ce25f59f0972bad8f2df40dfb728e273a3e1` |
| Collected | `9d3e7877e4736247470d8074a070aa00af5a2a9bbb8e7f713b89e59a2f2853c8` | `1d81b289bb064b663c00b048d82915ee6996ce726d0b0320c59eca4b094e093b` |
| Prune intent | `e96b5a1d42f5f08673fd566cc753c30b076a1a3da4effd43f5494577f6d2581d` | `04d87ec0dbdf13a7ca93a7320be88f30611a88564b4a867d9b952b0191b80b01` |
| Pruned receipt | `bc244a451630b993f35f17482006a1aad69d9a601f5b7dbaa337b49ac84f050e` | `7a855974d585abd02953788d12f83ec6c40b7e311957e8439c7110524bd846ce` |

The versioned briefs and collections are retained in `tasks/TASK-ID/`. Runtime
receipts remain under ignored `.aros/tasks/TASK-ID/`. Successful worktrees are
pruned, all four task branches are retained, and only the two preserved failed
worktrees remain registered.

## Automated verification

All commands used the installed `.venv` executables directly; no `uv` command
was invoked.

The receipts in this section are historical original-commissioning results for
code baseline `e4db5f5602fca83dfd3003fc32fd36458feee06c`; they predate the
post-commission hardening and subreaper containment work recorded below.

```bash
.venv/bin/pytest -o addopts= -q \
  tests/test_aros_tasks.py \
  tests/test_aros_task_runner.py \
  tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py \
  tests/test_aros_principal.py

.venv/bin/pytest -o addopts= -q \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_public_entry.py \
  tests/test_document_registry.py

.venv/bin/pytest -o addopts= -q

.venv/bin/ruff check \
  src/aros/store.py src/aros/tasks.py src/aros/task_runner.py \
  src/aros/task_tool.py src/aros/principal.py \
  src/cli/aros_app.py src/cli/commands/aros_cmd.py \
  tests/test_aros_store.py tests/test_aros_tasks.py \
  tests/test_aros_task_runner.py tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py tests/test_aros_principal.py \
  tests/test_document_registry.py

.venv/bin/ruff check src tests scripts
git diff --cached --check
git diff --quiet -- uv.lock
git diff --quiet e4db5f5602fca83dfd3003fc32fd36458feee06c -- uv.lock
```

Receipts:

```text
Focused Task/runner/TaskTool/CLI/Principal: 317 passed in 147.39s
Architecture/public-entry/registry:         226 passed in 9.45s
Final registry-only rerun:                   12 passed in 0.15s
Full suite:                                 1114 passed, 6 skipped in 178.22s
Relevant Ruff:                              All checks passed
Maintained src/tests/scripts Ruff:          All checks passed
Cached git diff-check:                      exit 0, no output
Working-tree uv.lock comparison:            exit 0, no output
B-to-working-tree uv.lock comparison:       exit 0, no output
```

The first architecture/public-entry/registry run correctly exposed one stale
documentation assertion: 225 tests passed and one still required `child task
substrate` to remain unavailable. The public Task expectation and the two
trusted-local capability markers were added to that assertion; its focused
red-to-green rerun passed 1/1 before the complete 226-test gate above.

A diagnostic `.venv/bin/ruff check .` also inspected directories outside the
maintained all-source gate and exited 1 on two pre-existing F401 findings:
`examples/subscribe_demo.py` imports `arbor.events.types` as `ev`, and
`skills/arbor-agent-tools/scripts/arbor_state.py` imports `shlex`. Both paths
are byte-unchanged from B and were deliberately not modified by this task.

## Post-commission hardening and final Wave 2 gate

Whole-wave review after the real smoke found operational races that the smoke's
short-lived adapters did not exercise: a normal leader could exit before its
same-PGID descendants, zombie-only groups could be misattributed as stopped,
timeout and stop publication could race finalization, and status could report
`lost` while the exact execution-claim runner was still alive finalizing. The
following reviewed commits close those gaps without changing the scientific or
collection semantics demonstrated above:

```text
a53600b  finalize only after non-zombie process-group drain
8c3a44b  wait for killed groups and serialize one stop delivery
d1ef6ff  resolve timeout/natural-exit and stop-result races
16b35ac  arbitrate stop publication against final receipts
f4165bd  keep live execution-claim runners out of transient lost state
ca4f138  use 64-bit task IDs with bounded collision retry
```

Follow-up containment commits `ea51f60`, `2868b94`, `2a41f35`, `722bf4b`, and
`c188484` establish the current boundary. Task adapters are trusted-local and
application-scoped, not a security sandbox. Network and shell capability flags
are audit declarations and are not enforced. Secrets and untrusted adapters are
unsupported. Daemonizing or new-session descendants that do not drain fail
closed as `lost` with no terminal receipt. V1 terminal truth covers the exact
PGID plus descendants reparented to the live subreaper. A new-session process
that outlives runner death is not claimed contained and cannot justify a clean
final receipt or prune. Delegated per-task cgroups belong to the shared
Operations process core, not the Wave 2 security claim.

The current implementation also uses atomic create-once JSON publication with
interrupted-alias recovery and durable absolute directory-chain fsync. Worktree
ownership remains non-expiring; the create-once execution claim is the local
one-attempt execution lease. The following historical receipt covers the
pre-containment Wave 2 hardening at code baseline
`757dc910b36c6727b2605292238dfa55eccfe57e`; it does not verify the follow-up
containment commits above:

```text
Full pytest:                              1133 passed, 6 skipped in 205.58s
Architecture/public-entry/registry:       226 passed
Maintained src/tests/scripts Ruff:        All checks passed
Git diff-check:                           exit 0
uv.lock comparison:                       unchanged
```

## Prior post-containment verification

The following receipts cover exact code baseline
`254f754a122bcaea6852abc55480eff339cbe889`. They remain valid evidence for the
trusted-local live-subreaper boundary at that code, but are superseded for
current code verification by the final post-review receipts below. The older
receipts above remain historical commissioning and pre-containment evidence.

```bash
.venv/bin/python -m pytest -o addopts= -q

.venv/bin/python -m pytest -o addopts= -q \
  tests/test_aros_task_runner.py tests/test_aros_tasks.py

.venv/bin/python -m pytest -o addopts= -q \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_public_entry.py \
  tests/test_document_registry.py

.venv/bin/python -m pytest -o addopts= -q \
  tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py \
  tests/test_aros_principal.py

.venv/bin/ruff check src tests scripts
git diff --check
git diff --quiet -- uv.lock
git diff --quiet e4db5f5602fca83dfd3003fc32fd36458feee06c -- uv.lock
```

Receipts:

```text
Full suite:                              1175 passed, 6 skipped in 251.13s
Task/runner:                             331 passed in 225.05s
Architecture/public-entry/registry:      227 passed in 12.45s
TaskTool/CLI/Principal:                   46 passed in 2.22s
Maintained src/tests/scripts Ruff:       All checks passed
Git diff-check:                          exit 0
Working-tree uv.lock comparison:         unchanged
Commissioning-baseline uv.lock comparison: unchanged
```

## Prior final Wave 2 verification

The final review added intent-bound targeted recovery for a prune interrupted
at the worktree-removal midpoint. Recovery may complete only the exact
prunable worktree registration bound by the persisted prune intent, rejects
unrelated prunable registrations, and never invokes global
`git worktree prune`.

The following receipts cover exact code baseline
`114da5959ae39be4b6977550ab291f9045d23679`. They remain valid final-Wave 2
evidence for that code, but are superseded for current code verification by the
post-status-fix receipts below.

```bash
.venv/bin/python -m pytest -o addopts= -q

.venv/bin/python -m pytest -o addopts= -q tests/test_aros_tasks.py

.venv/bin/python -m pytest -o addopts= -q tests/test_aros_task_runner.py

.venv/bin/python -m pytest -o addopts= -q \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_public_entry.py \
  tests/test_document_registry.py

.venv/bin/python -m pytest -o addopts= -q \
  tests/test_aros_task_tool.py \
  tests/test_aros_task_cli.py \
  tests/test_aros_principal.py

.venv/bin/ruff check src tests scripts
git diff --check
git diff --quiet -- uv.lock
git diff --quiet e4db5f5602fca83dfd3003fc32fd36458feee06c -- uv.lock
```

Receipts:

```text
Full suite:                              1177 passed, 6 skipped in 260.91s
Tasks:                                   258 passed in 122.38s
Task runner:                             75 passed in 111.16s
Architecture/public-entry/registry:      227 passed in 9.61s
TaskTool/CLI/Principal:                   46 passed in 0.93s
Maintained src/tests/scripts Ruff:       All checks passed
Git diff-check:                          exit 0
Working-tree uv.lock comparison:         unchanged
Commissioning-baseline uv.lock comparison: unchanged
```

## Stable Run status verification

At the pre-fix baseline, the timeout test reproduced a strict-read replacement
race at iteration 43. A deterministic replacement-during-read regression was
then RED. The fix is limited to replaceable Run status snapshots: RunService
performs at most three strict reads, while the default immutable `read_json`
contract remains unchanged. After the fix, the timeout stress passed 100/100
iterations and the affected five-suite gate passed 259 tests.

Non-blocking shared-core caveat: runner bootstrap strict read remains; actual
unresolved issue is launch vs unlocked reconcile transition and must be solved
by serialization or immutable prelaunch transition validation, not retry.

The following fresh receipts cover exact code baseline
`68b6ddddb1d1998b6b03380118129f0906ee8255`:

```bash
.venv/bin/python -m pytest -o addopts= -q

.venv/bin/python -m pytest -o addopts= -q \
  tests/test_aros_architecture_boundary.py \
  tests/test_aros_public_entry.py \
  tests/test_document_registry.py

.venv/bin/ruff check src tests scripts
git diff --check
git diff --quiet -- uv.lock
git diff --quiet e4db5f5602fca83dfd3003fc32fd36458feee06c -- uv.lock
```

Receipts:

```text
Baseline timeout reproduction:            strict-read race at iteration 43
Deterministic replacement-during-read:     RED before fix
Timeout stress:                            100/100
Affected five-suite gate:                  259 passed
Full suite:                                1178 passed, 6 skipped in 259.42s
Architecture/public-entry/registry:        227 passed in 9.72s
Strict transient/persistent/immutable:     3 passed in 0.71s
Maintained src/tests/scripts Ruff:         All checks passed
Git diff-check:                            exit 0
Working-tree uv.lock comparison:           unchanged
Commissioning-baseline uv.lock comparison: unchanged
```
