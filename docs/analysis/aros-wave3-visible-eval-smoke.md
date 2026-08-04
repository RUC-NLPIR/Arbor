# AROS Wave 3 Visible Evaluation Smoke Evidence

This record commissions the public, visible AROS evaluation path against real
Git commits, detached worktrees, tmux, isolated Linux execution, Run receipts,
measurement parsing, observation, audit, dirty preservation, and broker-loss
recovery. It does not interpret a metric as a scientific verdict and does not
claim protected evaluation, MCP parity, migration adapters, or Arbor
retirement.

## Result

The direct `aros` CLI, imported from the Wave 3 worktree at code baseline
`265aee93808d533d51005790e242c0222612ed10`, demonstrated all of the following:

- a strict registered descriptor froze an apparatus at a commit distinct from
  the candidate commit;
- one exact Run-backed scorer produced metric `0.73`, sample count `20`, and a
  `valid` measurement receipt using `aros.scalar-metric-v1`;
- active stderr, status, and lineage were independently observable through the
  public `status`, `observe`, and `audit` actions;
- a clean completed bundle removed both detached checkouts and both Git
  registrations;
- an externally dirtied candidate still had process state `completed`, but the
  measurement became `invalid_eval` and both checkouts were preserved;
- after the injected bytes were recorded and removed, the same clean-only
  bundle helper used by Eval removed the restored dirty-case bundle;
- killing only the foreground Eval broker made the evaluation permanently
  `lost` while its linked Run remained independently `running` and observable;
- replaying the same lost idempotency key created no Eval or Run, while a new
  key created exactly one new Eval and one new Run;
- explicitly stopped lost Runs became `cancelled`, but no missing measurement
  receipt was reconstructed; and
- the remaining exact clean bundles were removed only after evidence capture.

One preserved environmental process failure preceded the successful run. The
strict schema accepts the current virtual-environment Python path as a fixed
launcher, but the isolated bundle cannot read that virtual environment's
`pyvenv.cfg`. That attempt produced a valid `failed_process` / `not_available`
receipt and was never retried. A separately committed evaluator version changed
only the launcher to fixed `python3`; no AROS source or isolation policy was
changed for commissioning.

## Source, entry, and environment

```text
Evidence date:       2026-08-04 UTC
Worktree:            /workspace/Arbor/.worktree/aros-wave3-eval
Branch:              aros-wave3-eval
Code baseline:       265aee93808d533d51005790e242c0222612ed10
Host:                Linux 6.8.0-124-generic x86_64 GNU/Linux
Python:              Python 3.12.3
tmux:                tmux 3.4
Direct entry:        /workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/aros
Entry SHA-256:       3048cb1115ab8e19458f57ccea688d64955acbbc81dec207baf0e9d7494ae905
Commissioning repo:  /workspace/Arbor/.worktree/aros-wave3-eval/.worktree/commissioning/aros-wave3-visible-eval
```

The commissioning location is ignored by the product worktree's
`/.worktree/` rule. The direct entry shebang names this worktree's virtual
environment and imports `arbor.cli.aros_app:main`. Every AROS invocation set
this worktree source explicitly:

```bash
PYTHONPATH=/workspace/Arbor/.worktree/aros-wave3-eval/src \
  /workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/python -c \
  'import arbor, inspect, sys; import arbor.aros.eval; import arbor.cli.commands.aros_cmd; print(sys.executable); print(arbor.__file__); print(inspect.getfile(arbor.aros.eval)); print(inspect.getfile(arbor.cli.commands.aros_cmd))'
```

Its output was:

```text
/workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/python
/workspace/Arbor/.worktree/aros-wave3-eval/src/__init__.py
/workspace/Arbor/.worktree/aros-wave3-eval/src/aros/eval.py
/workspace/Arbor/.worktree/aros-wave3-eval/src/cli/commands/aros_cmd.py
```

No `uv`, mock service, `arbor aros` forwarding route, protected apparatus,
force removal, `git clean`, `git reset`, merge, cherry-pick, or M4 whole-port
was used.

## Exact committed fixture

All fixture file edits, including the later external dirty and release files,
were made with `apply_patch`. Git commands only initialized, staged, committed,
or inspected those bytes.

| Label | Commit | Tree | Purpose |
| --- | --- | --- | --- |
| Candidate A | `bd00b7c8c2ab339d253a775be78d5c5c84081cda` | `3944cfb6bc9949457e02efcca4e799acefa22716` | clean candidate with `candidate-mode.txt=success` and runtime ignores |
| Apparatus B | `714fcd7c8d7a5091a8913f9d60a2f687eed0c7c6` | `49f887a876687348ced8ee6c9933144d09be2ab0` | standard-library scorer |
| Manifest C | `14bc1afaebb296cc56bf1a5657fd6ae49e618850` | `c0037a2bd62336653745b8edce76bca4d5fd35d4` | evaluator `quality/1`, absolute current-venv launcher |
| Runnable manifest C2 | `1c407576edb03dc5d5c8f01f7f98cb31296a4c1b` | `9cb8e0b4f6ae5f42039b4ae58efd90a8afa84333` | evaluator `quality/2`, fixed `python3` launcher |
| Wait candidate D | `be6cfc81f94ed61bfb1230c38774d96d55a6474e` | `11d1310f673b910ddbf80bc9b4624d6dd3152669` | `candidate-mode.txt=wait` for controlled dirty/lost evidence |

The apparatus path is exactly `evaluation/score.py`, with SHA-256
`cc6fbace33e56a404770ecc732d70deab4668e64b4bb5bd4bddaf8e037c1c86c`.
Both manifests bind apparatus commit B and that exact blob. Their Git blob
bytes and working-tree bytes match:

| Version | Manifest path | Manifest blob SHA-256 | Scorer argv |
| --- | --- | --- | --- |
| 1 | `eval/suites/quality/1/manifest.json` | `03aa55bd624a43f6d8f66fb32dd0f2f7837e24870718b4dea3e0b5e888e5df3e` | `[/workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/python, ../apparatus/evaluation/score.py]` |
| 2 | `eval/suites/quality/2/manifest.json` | `fc4d277da358d6fe6c3de6cf90dee3ddd01668547f00b6f4f444170d7f2b19a8` | `[python3, ../apparatus/evaluation/score.py]` |

Both manifests contain exactly the strict visible-manifest field set. Version
2 differs in `evaluator_version`, manifest path/commit, and launcher only; it
does not change the apparatus commit or blob.

## Exact direct commands and bounded control

These fixed aliases make the expanded commands readable:

```bash
SOURCE=/workspace/Arbor/.worktree/aros-wave3-eval/src
PYTHON=/workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/python
AROS=/workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/aros
PROJECT=/workspace/Arbor/.worktree/aros-wave3-eval/.worktree/commissioning/aros-wave3-visible-eval
CANDIDATE_A=bd00b7c8c2ab339d253a775be78d5c5c84081cda
CANDIDATE_D=be6cfc81f94ed61bfb1230c38774d96d55a6474e
```

Registration used these exact public commands:

```bash
env PYTHONPATH="$SOURCE" "$AROS" eval register \
  --manifest eval/suites/quality/1/manifest.json \
  --actor commissioning-registrar --cwd "$PROJECT"
env PYTHONPATH="$SOURCE" "$AROS" eval register \
  --manifest eval/suites/quality/2/manifest.json \
  --actor commissioning-registrar --cwd "$PROJECT"
```

The four direct execution commands were:

```bash
env PYTHONPATH="$SOURCE" "$AROS" eval run quality 1 "$CANDIDATE_A" \
  --idempotency-key visible-commission-1 \
  --actor commissioning-principal --cwd "$PROJECT"
env PYTHONPATH="$SOURCE" "$AROS" eval run quality 2 "$CANDIDATE_A" \
  --idempotency-key visible-commission-2 \
  --actor commissioning-principal --cwd "$PROJECT"
env PYTHONPATH="$SOURCE" "$AROS" eval run quality 2 "$CANDIDATE_D" \
  --idempotency-key visible-dirty-1 \
  --actor commissioning-principal --cwd "$PROJECT"
env PYTHONPATH="$SOURCE" "$AROS" eval run quality 2 "$CANDIDATE_D" \
  --idempotency-key visible-lost-1 \
  --actor commissioning-principal --cwd "$PROJECT"
```

The authorized new action used the same last argv with only the idempotency key
changed to `visible-lost-2`. Every `eval run` above was the foreground process
in its own managed PTY; the orchestration yielded that PTY instead of adding a
shell background operator. Consequently the broker PID frozen in
`execution.json` was the direct CLI Python PID.

All active-state polls were bounded to 20 seconds, sampled every 50
milliseconds, and required all three predicates before any external action:

```text
.aros/evaluations/EVAL-ID/run.json exists
.aros/runs/RUN-ID/status.json has state == running
.aros/runs/RUN-ID/stderr.log contains the required active marker
```

The success marker was `scorer-active`; dirty and lost markers required
`broker-ready`. Terminal polls used the same bound and accepted only
`completed|failed_process|timed_out|cancelled|lost`. A failed bound aborted the
scenario; it did not relaunch it.

Public inspection used the literal forms below with each exact Eval ID:

```bash
env PYTHONPATH="$SOURCE" "$AROS" eval status EVAL-ID --cwd "$PROJECT"
env PYTHONPATH="$SOURCE" "$AROS" eval observe EVAL-ID \
  --stream stderr --max-bytes 4096 --cwd "$PROJECT"
env PYTHONPATH="$SOURCE" "$AROS" eval audit EVAL-ID --cwd "$PROJECT"
```

The two broker kills and attributed Run stops were exactly:

```bash
kill -KILL 1986878
env PYTHONPATH="$SOURCE" "$AROS" run stop RUN-20260804-053808-run-eb7d \
  --reason commissioning --actor commissioning-principal --cwd "$PROJECT"
kill -KILL 1994720
env PYTHONPATH="$SOURCE" "$AROS" run stop RUN-20260804-053935-run-d537 \
  --reason commissioning --actor commissioning-principal --cwd "$PROJECT"
```

Cleanup never reconstructed an Eval action. After checking each candidate and
apparatus with `git status --porcelain=v1`, it loaded the frozen Run manifest,
used the runner's exact manifest-to-binding validator, and called Eval's same
clean-only helper:

```python
manifest = json.loads((root / "runs" / run_id / "manifest.json").read_text())
_, repository, bundle = _execution_bundle_binding(root, manifest)
removed = remove_clean_execution_bundle(repository, bundle)
```

Any `False` result would have preserved the bundle and failed commissioning.

## Registration receipts

| Field | Version 1 | Version 2 |
| --- | --- | --- |
| Manifest commit | `14bc1afaebb296cc56bf1a5657fd6ae49e618850` | `1c407576edb03dc5d5c8f01f7f98cb31296a4c1b` |
| Manifest blob SHA-256 | `03aa55bd624a43f6d8f66fb32dd0f2f7837e24870718b4dea3e0b5e888e5df3e` | `fc4d277da358d6fe6c3de6cf90dee3ddd01668547f00b6f4f444170d7f2b19a8` |
| Apparatus commit/tree | `714fcd7c8d7a5091a8913f9d60a2f687eed0c7c6` / `49f887a876687348ced8ee6c9933144d09be2ab0` | same |
| Descriptor SHA-256 | `9bb03b0f7fd15ecef17d569c49f6546053d0d0766a4c8011fcf2cb6e93e0d1ea` | `555205a4eefa32868e6f8c1260678d4b47ea7a53ee0ea2f9186dcd84d00c6d2f` |
| Registered at | `2026-08-04T05:31:41.240Z` | `2026-08-04T05:34:19.868Z` |
| Actor | `commissioning-registrar` | `commissioning-registrar` |

## Preserved absolute-venv process evidence

The version-1 key `visible-commission-1` maps to:

```text
Eval: EVAL-96238843fd85c0d493f514292918cbc9cfd58ebea2b0f2cdbbc0fc8fc2fe8085
Run:  RUN-20260804-053154-run-db41
```

The scorer never reached user code. Python reported:

```text
Fatal Python error: init_import_site: Failed to import the site module
PermissionError: [Errno 13] Permission denied: '/workspace/Arbor/.worktree/aros-wave3-eval/.venv/pyvenv.cfg'
```

This is a process/environment result, not a scientific measurement. The
terminal projection was `completed / failed_process / not_available`, with
`metric=null`, `sample_count=null`, and `bundle_cleanup_state=removed`.
`aros eval audit` returned `valid=true` and `issues=[]`.

| Record | Record/self hash | Exact file SHA-256 |
| --- | --- | --- |
| Request | `63c40fcd6306c82788a16fadda1a03cdce872fc5157a1bc0fc8a7bdf2304d98d` | `359aef9675330217306047219534615360e14ccbf8d7f3931e931265346ac5ca` |
| Execution | `665843f5c54ebc71288fdccb00b03a8e15913639791aae0b6c263ea320086913` | `e66a8fe9d76cf1d991a643a1ce38a38d72b36419afb479e7f1a2372adebc8568` |
| Run link | `a701ccbbc858b793058ce20245ad1db3f35bd6e252f29efeb791442f7b6706e6` | `50e365cdedba8d8addd32170893504f3ade2c1551ff40b4a267d1f9bf04b8134` |
| Run manifest | `1c71155b1cb3d044b6f18b26150f1189a4409a307559b2c6ea8f197898e0e6a2` | `a32004f57f7b41d917db87269bd0dd3d193fe626ae64aea9543f44599732da30` |
| Run final | `8f01d7622e6e742b8d9149f9c7c09cc1699ee9a51a7cc13a22a7581c0bade42a` | `34d682ed21374cd0b50781374f543651ed4988fbc5101765c9e12ea28f19be0d` |
| Bundle | `9aa903e71a440ab9ea814d02bf53889b73097545eea06e813a6566cde9bee6e6` | n/a: portable binding hash |
| Measurement receipt | `d998566e5abccb23786bb773df8068574e5c66d7676ff5ed313885636b0d07f6` | `1982ba8fe9c195c589c9cd10f6e4b80cafcb37f204924cd74c2719f3a9626bb4` |
| stdout | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` (0 bytes) | same |
| stderr | `c55e47c939bbd8fe8d46667e74db2ea64c5b5117329e4d9ce1f965d97a37b5f2` (660 bytes) | same |

Both bundle paths and registrations were absent after clean automatic removal.
The key was not retried; version 2 used a new evaluator identity and new key.

## Successful exact measurement

The version-2 key `visible-commission-2` maps to:

```text
Eval: EVAL-cf3b95febac18f7ec32b7a6e700cd81e0e5d6fd6e4289a013d69bdef7f6a22a4
Run:  RUN-20260804-053425-run-6bf6
Candidate: bd00b7c8c2ab339d253a775be78d5c5c84081cda
Apparatus: 714fcd7c8d7a5091a8913f9d60a2f687eed0c7c6
```

The bounded active poll observed linked Run state `running` and the active
stderr marker. Public observe returned exactly:

```text
scorer-active mode=success
```

The direct status/audit calls completed just after the eight-second scorer
window closed; they factually returned `completed / completed / valid`,
`receipt_ref` present, and `audit valid=true, issues=[]`. The exact scorer
stdout was the one accepted metric document:

```json
{"schema_version":1,"metric":0.73,"sample_count":20}
```

The measurement receipt records `metric=0.73`, `sample_count=20`,
`parser=aros.scalar-metric-v1`, and `bundle_cleanup_state=removed`.

| Record | Record/self hash | Exact file SHA-256 |
| --- | --- | --- |
| Descriptor | `555205a4eefa32868e6f8c1260678d4b47ea7a53ee0ea2f9186dcd84d00c6d2f` | `0841f20c01445c64be0a87007a5c2ae80af9e32b2a2f40f2030edb3d8d228afc` |
| Request | `e21b7c42c70ed8bb711668958a6a598b65d8c4f95b93c5b9d054aeec8341e45f` | `a95b6ab3d2f948205c823edc80e07c998d1207e58d9d59a971e6437c5a360c6b` |
| Execution | `8d3c769aefeb526f5a356724f438217f232793c416e5193b701faebb9d2cea62` | `ea150afee6166c5b91cd58ab46290d75f9b7a1f8b0e4d547af3c51f3e97a8ae5` |
| Run link | `8e6d731804257683ef4915a2a3ab24feb4b0839229a09fb3217d75d319e80d1d` | `4385c663d38ee1e5637d9cfd4bcd4c118cd65f643cbdb85f684b81eb0ae19fe2` |
| Run manifest | `397f8b346e04d85f350e19ab44d7e89b39f3cfae48018cf45d293fa2722780b8` | `fee59ac4af55979e9a8c06262cf1856ed9ae166e4fc2341fb7bf5f9731c56167` |
| Run final | `ac0824c9ded565bc4ed5fd24840dd8fbe9c018bfdec24ddd1ee409735fab658d` | `93ad814bd991be8c22f34499cc8a1818698913049e9949434cf7a07b0f641de1` |
| Bundle | `9aa903e71a440ab9ea814d02bf53889b73097545eea06e813a6566cde9bee6e6` | n/a: portable binding hash |
| Measurement receipt | `bd1dd68018725c1922f699943a87b16315779a9cb6d5c45cdb4583c7ca49ad4d` | `84538d0c94b151665ed346a7fbb16d618e38b2ba5c2d6f1b60343dda8b065cfb` |
| stdout | `3c23697adcaac8b32745a384aea6454d99efbaaa62830b03fcdaaa85ac3b42bf` (53 bytes) | same |
| stderr | `9c714d89b5b3bfe7c5a001a7de46f912da77ebff20da1bd55e6ea3169286b9c8` (27 bytes) | same |

After receipt publication, `candidate`, `apparatus`, and their bundle root all
tested absent. `git worktree list --porcelain` contained only the commissioning
project, so both detached registrations were absent. The project status was
clean.

## Dirty candidate preservation and restoration

Candidate D and key `visible-dirty-1` map to:

```text
Eval: EVAL-f48b6c590093924f39a417f37cda56f3a18c0be12e51ec4fa742dcad62f430c0
Run:  RUN-20260804-053558-run-ef87
```

Before external modification, direct public evidence returned:

```text
evaluation_state=running
referenced_process_state=running
measurement_state=not_available
reason=execution claim is live
audit valid=true, issues=[]
```

Public observe returned:

```text
scorer-active mode=wait
broker-ready
```

Only then did `apply_patch` add
`candidate/external-dirty.txt` with these exact bytes:

```text
UTF-8: externally injected dirty candidate evidence\n
Bytes: 45
Hex: 65787465726e616c6c7920696e6a65637465642064697274792063616e6469646174652065766964656e63650a
SHA-256: 9f497c9211b28cc6a9365200f807af47c65550e3553ba1e740e93afdc2852d7b
```

`git status --porcelain=v1` in the candidate reported exactly
`?? external-dirty.txt`; the apparatus remained clean. `apply_patch` then added
`tmp/release`, allowing the scorer to finish. Public observe subsequently
contained `release-observed`.

The terminal receipt deliberately separates facts: process state is
`completed`, measurement state is `invalid_eval`, metric and sample count are
`null`, and cleanup state is `preserved`. Both detached checkout directories
and both exact Git worktree registrations remained present.

| Record | Record/self hash | Exact file SHA-256 |
| --- | --- | --- |
| Request | `0cc340e410789513c5b2a4ec0dfa3473f87236ede09ba0efb470a45d68fd4cf7` | `09b9ad9fb9d71b26f0f184c6392c8eece6791e470dcb40de939d259d090c3e1f` |
| Execution | `4f5d719d8b4f5d6b4b7e2ed67ffc0719cd853e415b072e77d3d15bcedbf46ef1` | `b661589427a4dd005b3eeb0c203b32226dd9c63e82a7af90f3f1e9e78d5fd942` |
| Run link | `b801d7ceccec998c663b87df6050ac03c38e474004940a23a975ca3d772be068` | `cff20fc24606477ad91c275f38fd5d19c0f3a74335c9b311bc46ce27132bd3d7` |
| Run manifest | `5bce910a4951fed2fb938020e0fc237d4b740ec88cc1a29ecd629b7a1cda53f6` | `08b0026a78e22c87e61a51523d48180ee7e112c45b87700190e5c2680312abe5` |
| Run final | `deb61e410b3b9cb25f1c930697ff9ca9b80a28f478be57071e6ec0a7ad66ef6d` | `492e028cf90f792de61716a876680c6c8bb1901839f37b6741b5ab4e956e4736` |
| Bundle | `a8e556bf6865ddf59a26d6bfb47957c2966b6775d859b27c5b2a355d14f2f10d` | n/a: portable binding hash |
| Measurement receipt | `4d055a11bf8fc38e271f886528431d128e32b245232ac94a27179a08335cabef` | `b3fd709d94536886444f53bf571b9d017121b037da4d71963538b9e749ed6780` |
| stdout | `3c23697adcaac8b32745a384aea6454d99efbaaa62830b03fcdaaa85ac3b42bf` (53 bytes) | same |
| stderr | `b91a6a7b3cb1f6dc28f05bcdd3b0ffa41480e725c0ad2274511df63ab2665eb7` (54 bytes) | same |

After evidence capture, `apply_patch` deleted only the injected file. Both
checkouts then had empty porcelain status. The exact frozen binding was loaded
from `RUN-20260804-053558-run-ef87/manifest.json`; the clean-only helper returned
`removed=True`. Both paths and registrations became absent, and the
commissioning project was clean.

## Lost broker, no retry, and new action

Before lost commissioning, the three prior requests and three prior Run
manifests produced these exact inventory facts:

```text
Eval count/list SHA-256: 3 / 57c2716e03d896066660037e8babc79d14a09286fad5a3770dd5f2e2d62260f0
Run count/list SHA-256:  3 / 370ebfa7cad02e1325b3df4d3376467242a960f64fe1a9ba1db47b246a422ea5
```

### Original lost request

Key `visible-lost-1` produced:

```text
Eval: EVAL-0beba2e9dcbe2777b8e6f060502f3b386c7dcac8c0cbba1321afaae7b821f46d
Run:  RUN-20260804-053808-run-eb7d
Broker PID/start: 1986878 / linux-proc-start:298645578
Run process PID/PGID/start: 1987535 / 1987535 / linux-proc-start:298645923
```

After `run.json`, Run `running`, and `broker-ready` were all present, only PID
`1986878` received `SIGKILL`. The direct CLI session exited 137. A fresh direct
Eval status then returned:

```text
evaluation_state=lost
referenced_process_state=running
measurement_state=not_available
reason=execution claim lock was released
receipt_ref=null
```

At the same time, direct `aros run status` still reported the exact process
identity above as `running`; public Eval observe still returned the two active
stderr lines, and audit returned `valid=true, issues=[]`.

Before same-key replay, counts were 4 Eval / 4 Run, with list hashes
`be81ceabf5b52f40429de0e198281ddbef111e6d5a5f2dee4049156f824bcae9`
and `df0dc4acd3fad365270d773afb762fc2f6837cfcb0af9ff5dac2efba2ab09588`.
The same direct `eval run` argv returned the same lost Eval and Run. Counts and
both hashes remained byte-for-byte unchanged, and no measurement receipt
existed.

The attributed Run stop delivered only `TERM`. A bounded poll reached
`cancelled` with exit code `-15`. Eval remained `lost`, but its independently
derived referenced process state advanced factually to `cancelled`.

### New Principal action

Changing only the key to `visible-lost-2` created:

```text
Eval: EVAL-eebb7bd5a1242d5c87217dbcd79615472efdf8caa862bf144af907e59e5131cf
Run:  RUN-20260804-053935-run-d537
Broker PID/start: 1994720 / linux-proc-start:298654282
Run process PID/PGID/start: 1995260 / 1995260 / linux-proc-start:298654626
```

The inventory became exactly 5 Eval / 5 Run. Each new ID occurred once; list
hashes became
`2a1e19838ddfb32c05ec3b5f7c4093cff692714880ade63051b73096f90258d0`
and `88bf7a3688919b839eb457451177e56e96ed2d23c170b90376ef74f04392445e`.
No other request or Run was created.

After the same three active predicates, only PID `1994720` received `SIGKILL`;
the CLI exited 137. Eval became `lost` while direct Run status still reported
`running`. The attributed stop delivered `TERM`, and the bounded terminal poll
reached `cancelled` with exit code `-15`.

### Lost lineage hashes and terminal cleanup

| Record | Lost 1 | Lost 2 |
| --- | --- | --- |
| Request self-hash | `c2a741fa53b4b251d600d9370e2e67b0f97d9a3a4dd6993d765aea64788dccc5` | `e3a8244d59b66547e3f8f954bb80f96f88b53cf3fb339802ff5c3de91f06a97b` |
| Request file SHA-256 | `9f6c28bb52aa6aa39977eef5601ed108f875d7e479577737fa3ea2ceb6957847` | `cd316665d616a8a248ddc2c22d71216e3efe83af1024a99524d4c89a59df0618` |
| Execution self-hash | `1ba72a1aa9d06eb1249de178c538eb5af1405788948ecb895620048c15387a8e` | `35bc6f39623425437960453c314d46e24852817488bd2c1a9a44ac6a2e91576f` |
| Execution file SHA-256 | `37fb549725ec38da49739ec2d33176913438873113d339a3f167ef149844ed77` | `8990dcd70b7710cd478469783a6fb2515da5b11b698eff8fcb8ee783af3f606a` |
| Run-link self-hash | `dbc578bcb9e5cea1529fbb928330c3c44c21b0bb04a35d2ba6ba77834f2ad6d9` | `f2f94a12522dd96946d0d7eac03b7666637f4d50cfe3f95f121206a555ffc4d5` |
| Run-link file SHA-256 | `9cdeb4b394e5b15d04484ac8f27b941975d2a7b93133eb7dc9e81d93f5f334c8` | `70360d350fb4d2fd8f83d0ca6f764cf97fc88ac598e5e191766023f704be7220` |
| Run manifest self-hash | `26139920605ead2e5b19430745abbf4cd74b8e405461688ea2f5cfcb9e07bf9d` | `e0d6c09007ad72cb1dca982b57ab1fbdcfe75e6fccb21edc4a8affd3b7676451` |
| Run manifest file SHA-256 | `b85ea8d3331942ef10f8d07b0cf269c5566de80c7f61087f3b15d8975e8fd3e2` | `5e14940942c2cac413eba9ba3b1ac889c842241caf3276daababeb95719705e4` |
| Run final canonical SHA-256 | `42c833fdce665c2a1b08ff3d97c8f8b9b5f6df3fb3e2eff831f7610965810500` | `2c441870c13f60d582be8bc95d8acad66b175a48ba738bc69c862f757208ac3a` |
| Run final file SHA-256 | `737bffde156d7fa3d5d4a3e252d94a1c74d2b032cc230d844c63ea08dc2a1814` | `aa10bac9d255b6235647e23fd30358a9ff79bbf99908a4306a7f734bf38d4663` |
| Bundle SHA-256 | `a8e556bf6865ddf59a26d6bfb47957c2966b6775d859b27c5b2a355d14f2f10d` | same |
| stdout | 0 bytes / `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | same |
| stderr | 37 bytes / `fdaa3e94891b004fd3e53ca2636aa6879a6c18127c650984ce03ecdf0099970d` | same |
| Measurement receipt | absent | absent |

All four lost checkouts had empty porcelain status after terminal Run
observation. The clean-only helper returned `true` for both bundles. Their
paths and registrations became absent. A final same-key invocation still
returned the original Lost-1 Eval/Run with referenced process `cancelled`;
inventories remained exactly 5/5 with the same final list hashes, both bundle
paths remained absent, and both measurement receipts remained absent.

## Automated verification and independent review

The exact repository gates were:

```bash
/workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/python -m pytest \
  -o addopts= -q tests/test_aros_eval_records.py tests/test_aros_eval.py \
  tests/test_aros_eval_tool.py tests/test_aros_eval_cli.py \
  tests/test_aros_worktrees.py tests/test_aros_runs.py
/workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/python -m pytest \
  -o addopts= -q
/workspace/Arbor/.worktree/aros-wave3-eval/.venv/bin/ruff check src tests scripts
git diff --check
git diff --exit-code -- uv.lock
```

The module gate reached `422 passed in 61.31s`; the fresh whole suite reached
`1594 passed, 6 skipped in 323.32s`; Ruff reported `All checks passed!`; and
both diff gates exited 0. The focused registry gate reached `16 passed`.

A fresh internal Design Book/spec reviewer found one Important transcription
error: the successful Run-link file hash omitted two hex characters. After the
hash was corrected from the preserved file, its post-fix focused gate reached
`17 passed` and it approved with no remaining Critical or Important findings.

Only after that approval, a fresh quality/security/simplicity reviewer checked
all 83 documented hashes, live cleanup/process invariants, inventory hashes,
registry metadata, and architecture scope. Its focused registry and
architecture-boundary gates reached `16 passed` and `205 passed`; it approved
with no Critical or Important findings. Both reviews explicitly confirmed no
Eval-owned process stack, retry/attempt history, semantic verdict, protected
admission claim, or M4 whole-port.

## Limits and non-claims

- The absolute current-venv launcher is schema-valid but is not executable in
  the current isolated bundle because Python must read the outside-allowlist
  `pyvenv.cfg`. Fixed `python3` is the commissioned launcher. This compatibility
  limit is preserved as process evidence, not hidden by retry or policy change.
- The scorer is a deterministic commissioning fixture, not a scientific
  benchmark. `valid` means its declared machine document passed mechanical
  verification; it does not mean the candidate is good, admitted, or better.
- The active success poll proved running state and active stderr; the direct
  status/audit subprocesses completed just after the scorer reached terminal
  state. The dirty scenario separately captured direct public status, observe,
  and audit while the linked Run was still running.
- Lost Eval state and referenced Run state are intentionally distinct. Stopping
  the Run changes only independently observed process truth; it does not create
  an Eval receipt or revive finalization.
- Protected evaluation registration/admission, hidden file descriptors,
  disclosure budgets, migration adapters, MCP parity, and Arbor retirement
  remain unavailable and are not claimed by this evidence.

## Exit result

The real direct-CLI evidence closes the visible deterministic evaluation gate:
exact candidate/apparatus commits produce a Run-backed factual measurement,
dirty material is preserved, status/observe/audit remain factual, and broker
loss never retries or reconstructs a measurement. Gate C-D protected admission
remains a separate follow-on implementation and commissioning obligation.
