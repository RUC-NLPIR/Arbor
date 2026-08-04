# AROS Principal Loop Mediated Commissioning Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Prove one real OpenCode Principal can execute Task→Eval→Measurement→explicit Assimilation→ProContract admission→Git CAS, lose its process/session context, and resume from a bounded canonical Attention packet without bypassing authority.

**Architecture:** A host broker process owns canonical Git and the pinned AROS runtime. A nonroot OpenCode worker container mounts only the full-clone candidate and its own state; canonical Git, broker credentials, and human routes are absent. Narrow AROS tools communicate with the broker over one authenticated private Unix-socket capability, while ProContract admission remains in the worker and returns exact receipt/fence bytes for broker-side finalize. The Principal has file read/write and the four bounded AROS tools but no Bash or human-direct route.

**Tech Stack:** Python 3.10+, Bun/TypeScript SDK Next embedded host, OpenCode ProContract adapter, real model/provider, Git full clone candidate, AROS Task/Run/Eval, tmux, isolated-linux Eval, process-level SIGKILL recovery probes.

---

## Dependencies and non-claims

This plan begins only after:

1. docs/superpowers/plans/2026-08-04-aros-principal-loop-core.md is complete and its core commit is fixed.
2. docs/superpowers/plans/2026-08-04-aros-opencode-procontract-admission.md is complete and its OpenCode commit is fixed.
3. Both repositories are clean and their focused/full verification evidence is recorded.

This commissioning does not prove:

- protected/hidden Eval Gate D;
- untrusted same-UID arbitrary-process containment;
- provider child profiles or a security-sandboxed child Agent;
- Source Gateway fallback, Skills lifecycle, MCP parity, or Arbor retirement.

The Principal is a real OpenCode Agent. The child Task uses the current trusted-local worktree-isolated adapter substrate; do not upgrade that claim to a security sandbox.

Never run uv. OpenCode tests run only from package directories. The final public command remains aros; the embedded TypeScript host is a temporary provider adapter until provider parity is separately commissioned.

## New commissioning artifacts

Create in Arbor:

- commissioning/principal_loop/task_adapter.py — deterministic B-C-R candidate adapter.
- commissioning/principal_loop/evaluation/score.py — independent scalar evaluator.
- commissioning/principal_loop/AGENTS.md — Principal boot/authority/checkpoint rules for the fixture.
- commissioning/principal_loop/AROS.md — fixture mission and success criteria.
- commissioning/principal_loop/question.md — initial load-bearing question template content.
- commissioning/principal_loop/claim.md — initial Claim before measurement.
- scripts/commission_aros_principal_loop.py — creates repos, setup records, invokes embedded host, kills/restarts, captures receipts.
- scripts/aros_principal_loop_broker.py — private finite RPC broker for Attention, Task/Run/Eval, prepare, and finalize.
- scripts/verify_aros_principal_loop_commissioning.py — independent exact verifier.
- tests/test_aros_principal_loop_commissioning_scripts.py
- docs/analysis/aros-principal-research-loop-smoke.md after the real run.

Create in the OpenCode aros-admission worktree:

- packages/sdk-next/script/aros-principal-commission.ts — embedded host, tool adapters, context source, exact Contract/binding formation.
- packages/sdk-next/test/aros-principal-commission.test.ts — local host wiring and fail-closed tests without claiming live mediation.

Runtime output lives under an ignored broker root:

~~~text
.worktree/commissioning/aros-principal-loop/
  canonical.git-worktree/
  candidate/
  opencode-data/
  broker/
    socket/
  evidence/
  crash-cases/
~~~

## Fixed fixture

The Task candidate changes exactly one tracked file:

~~~text
candidate-mode.txt
~~~

from:

~~~text
baseline
~~~

to:

~~~text
success
~~~

The evaluator reads candidate-mode.txt from the exact candidate commit and emits exactly:

~~~json
{"schema_version":1,"metric":1.0,"sample_count":1}
~~~

Its visible manifest uses:

~~~json
{
  "schema_version": 1,
  "evaluator_id": "principal-loop",
  "evaluator_version": "1",
  "visibility": "visible",
  "apparatus_commit": "<filled by setup>",
  "apparatus_paths": [
    {
      "path": "commissioning/principal_loop/evaluation/score.py",
      "blob_sha256": "<filled by setup>"
    }
  ],
  "scorer_argv": [
    "python3",
    "../apparatus/commissioning/principal_loop/evaluation/score.py"
  ],
  "scorer_cwd": ".",
  "inputs": ["candidate-mode.txt"],
  "environment_ref": "isolated-evaluator-v1",
  "seed_policy": {"kind": "fixed", "seed": 7},
  "resource_limits": {"timeout_seconds": 120},
  "success_exit_codes": [0],
  "raw_outputs": ["stdout", "stderr"],
  "metric_output": {
    "source": "scorer_stdout",
    "parser": "aros.scalar-metric-v1",
    "metric_name": "principal_loop_quality",
    "minimum": 0,
    "maximum": 1,
    "minimum_samples": 1
  },
  "known_limitations": ["commissioning fixture, not a scientific benchmark"],
  "calibration_refs": []
}
~~~

The final transition has two separate assimilations:

~~~json
{
  "schema_version": 1,
  "base_commit": "<canonical HEAD>",
  "workspace_paths": [
    "knowledge/claims/C-0001.md",
    "memory/NOW.md"
  ],
  "assimilations": [
    {
      "observation_ref": "tasks/<TASK-ID>/collected.json",
      "affected_paths": ["memory/NOW.md"],
      "rationale": "memory/NOW.md#Assimilated task return"
    },
    {
      "observation_ref": "eval/evaluations/<EVAL-ID>/receipt.json",
      "affected_paths": ["knowledge/claims/C-0001.md", "memory/NOW.md"],
      "rationale": "knowledge/claims/C-0001.md#Evidence links"
    }
  ]
}
~~~

The checkpoint always includes proposal.json as transition metadata; it is not repeated in workspace_paths. Do not add a fifth proposal field.

## Task 1: Build deterministic fixtures and an independent verifier

**Files:**

- Create: commissioning/principal_loop/task_adapter.py
- Create: commissioning/principal_loop/evaluation/score.py
- Create: commissioning/principal_loop/AGENTS.md
- Create: commissioning/principal_loop/AROS.md
- Create: commissioning/principal_loop/question.md
- Create: commissioning/principal_loop/claim.md
- Create: scripts/verify_aros_principal_loop_commissioning.py
- Create: tests/test_aros_principal_loop_commissioning_scripts.py

- [ ] **Step 1: Write RED fixture/verifier tests**

Add:

~~~python
def test_scorer_emits_one_strict_metric(tmp_path):
    (tmp_path / "candidate-mode.txt").write_text("success\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCORER)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "metric": 1.0,
        "sample_count": 1,
    }


def test_verifier_rejects_unrelated_task_and_measurement(tmp_path):
    evidence = _minimal_evidence(tmp_path, task_commit="1" * 40, eval_commit="2" * 40)
    result = subprocess.run(
        [sys.executable, str(VERIFIER), str(evidence)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "candidate_commit" in result.stderr
~~~

Also test:

- task adapter writes strict B-C-R return with return commit as sole return-file change;
- evaluator fails closed for baseline/missing input;
- verifier requires two assimilations and both disappear from restarted packet;
- verifier rejects cooperative enforcement, synthetic admission, same session, missing closure hash, or missing canonical CAS evidence.
- verifier requires OpenCode Session tool-call events for Task/Eval/file edits/Research audit/checkpoint and rejects driver-authored semantic bytes.
- verifier requires distinct broker/worker OS identities and proves canonical/credential paths are absent from worker mounts.
- verifier compares ProContract canonical receipt/fence bytes against Python decoding and the shared UTF-8 golden vector.

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py
~~~

- [ ] **Step 3: Implement the scorer**

Read candidate-mode.txt as strict UTF-8, require exact bytes success newline, and write the one JSON document. No Agent prose, environment score, or CLI argument may set metric.

- [ ] **Step 4: Implement strict B-C-R adapter**

The adapter reads AROS_TASK_* environment, verifies task/base/worktree identity, writes candidate-mode.txt, commits candidate C with explicit local Git identity, writes tasks/TASK-ID/return.json with exact current Task schema and self-hash, then commits return R as the sole changed path. It never touches the parent/canonical checkout.

- [ ] **Step 5: Implement the verifier**

The verifier loads only exact evidence paths and independently calls current strict AROS validators. It asserts:

~~~python
assert collected["child_commit"] == measurement["candidate_commit"]
assert measurement["measurement_state"] == "valid"
assert measurement["metric"] == 1.0
assert measurement["sample_count"] == 1
assert admission["decision"] == "allow"
assert admission["enforcementClass"] == "mediated"
assert restarted_packet["unassimilated_returns"] == []
~~~

It also verifies proposal/audit/admission/Claim/NOW are in one canonical commit and that both assimilation observation refs are absent from pending.

- [ ] **Step 6: Verify GREEN and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py
/workspace/Arbor/.venv/bin/ruff check \
  commissioning/principal_loop scripts/verify_aros_principal_loop_commissioning.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git diff --check
git add commissioning/principal_loop scripts/verify_aros_principal_loop_commissioning.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git commit -m "test(aros): add principal loop commissioning fixture"
~~~

## Task 2: Build the private broker and OpenCode worker host

**Files:**

- Create in OpenCode: packages/sdk-next/script/aros-principal-commission.ts
- Create in OpenCode: packages/sdk-next/test/aros-principal-commission.test.ts
- Create in Arbor: scripts/aros_principal_loop_broker.py
- Modify in Arbor: tests/test_aros_principal_loop_commissioning_scripts.py

- [ ] **Step 1: Write RED host tests**

Add:

- registers Research, Task, Run, Eval and one aros/research-attention context source;
- exposes no Bash when Research binding omits process.execute;
- passes only proposal/message/maxChars from model input;
- calls the captured reserveToolAction facade only after decoding checkpoint; read actions never reserve;
- Task/Run/Eval adapters reserve exactly once before their first mutating broker RPC;
- invokes pinned AROS prepare, calls research.admitTransition, then invokes finalize with exact receipt bytes;
- refuses finalize when admission is deny or subject differs;
- context source and attention tool return the same packet hash;
- default boundary cannot claim mediated.
- worker receives no canonical path and all AROS operations traverse the authenticated broker socket.
- broker rejects wrong peer identity, nonce, operation, candidate path, or replay.

- [ ] **Step 2: Run and verify RED**

From packages/sdk-next:

~~~bash
bun test test/aros-principal-commission.test.ts
~~~

- [ ] **Step 3: Implement the finite host broker**

The Python broker accepts broker-owned command-line arguments for:

~~~text
canonical root
candidate root
pinned Python executable
pinned AROS source/bootstrap
trusted execution-closure hash
evidence directory
~~~

It listens on a private Unix socket directory mounted into the worker, authenticates peer credentials plus a one-use in-memory startup nonce, and accepts only typed attention, task, run, eval, transition_audit, prepare, and finalize messages. Paths are fixed broker configuration or candidate-relative validated refs; canonical path, human-direct, arbitrary argv, and generic shell are not protocol fields. Spawn pinned AROS with -I, clear PYTHONPATH/PYTHONHOME, set a fixed PATH, and insert only pinned source in the trusted bootstrap. Hash interpreter, bootstrap, AROS audit/validator files, and controlled environment into the binding closure.

- [ ] **Step 4: Implement the nonroot OpenCode worker**

Run the embedded host in a nonroot container/user namespace. Mount candidate and worker state only, plus the private broker socket capability. Do not mount canonical, host Git common dir, broker evidence, host credentials, or human route. The TypeScript host receives candidate root and broker socket/nonce through host startup, not model context.

- [ ] **Step 5: Implement the Research tool handshake**

For checkpoint:

~~~text
broker runs checkpoint_bridge prepare
decode one PreparedCheckpoint
call opencode.research.reserveToolAction with trusted Tool.Context and action name
call opencode.research.admitTransition with the returned reservationID
require decision=allow and exact subject/audit hashes
call opencode.research.revalidateAdmission without another charge
broker runs checkpoint_bridge finalize
write exact receipt and finalize-fence bytes to broker
decode one commit result
~~~

The worker does not spawn checkpoint_bridge directly; each line above is a finite broker RPC. Attention and transition_audit remain read-only. Task/Run/Eval adapters expose only existing AROS service actions needed by this scenario.

- [ ] **Step 6: Register Attention as SystemContext**

Use one source key aros/research-attention. The worker obtains ProContract attentionContext without charging, sends that exact authority/budget/obligation JSON through the private broker attention operation, and receives the one Python-built packet. Its encoded value is exact packet JSON. baseline and update both render the complete bounded current packet, not a TypeScript-patched field set or pointer. This source is observed at new Session Context Epoch after start, compaction, or model switch.

- [ ] **Step 7: Verify GREEN and commit both repositories**

~~~bash
cd /workspace/opencode/.worktree/aros-admission/packages/sdk-next
bun test test/aros-principal-commission.test.ts test/embedded.test.ts
bun typecheck
git diff --check
git add script/aros-principal-commission.ts test/aros-principal-commission.test.ts
git commit -m "test(opencode): add aros mediated host"
~~~

Then in Arbor:

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py
/workspace/Arbor/.venv/bin/ruff check scripts/aros_principal_loop_broker.py
git add scripts/aros_principal_loop_broker.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git commit -m "test(aros): add mediated admission broker"
~~~

## Task 3: Create a fresh canonical/candidate authority domain

**Files:**

- Create: scripts/commission_aros_principal_loop.py
- Modify: tests/test_aros_principal_loop_commissioning_scripts.py

- [ ] **Step 1: Write RED setup tests**

Add:

- candidate is a full clone, not a linked worktree;
- candidate has no origin, alternates, canonical path, credential, or shared common Git directory;
- canonical ref and broker database are outside candidate;
- worker mount inventory excludes canonical, broker evidence/credentials, and host Git data;
- no Task brief is pre-seeded; the mediated Principal must create and operationally admit it;
- Eval manifest apparatus commit/blob hash are exact;
- issued Contract budget equals requested values without widening.

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py
~~~

- [ ] **Step 3: Implement clean setup**

The driver:

1. creates a fresh Git canonical workspace;
2. copies fixture files and runs aros init;
3. writes Question/Claim/NOW/frontier/evaluator manifest;
4. commits apparatus and baseline;
5. creates candidate with git clone --no-local;
6. removes origin and proves no objects/info/alternates;
7. creates separate OpenCode worker state and host broker/evidence directories;
8. launches the broker and nonroot worker with disjoint mounts/identities;
9. registers the evaluator through the broker in candidate runtime.

Do not use a linked .worktree as the Principal boundary.

- [ ] **Step 4: Form the exact Research Contract**

The worker host issues an immediate Contract with exact goal/brief, authority filesystem.read/filesystem.write only, finite turns/actions/deadline, and no budget widening. The boundary attestation is accepted as mediated only after the broker verifies worker identity/mount proof and the worker proves its candidate Location matches. Binding includes canonical workspace identity/ref but never reveals the canonical path. It grants checkpoint/task/run/eval, evaluator principal-loop@1, and the pinned audit closure.

- [ ] **Step 5: Verify setup and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_principal_loop_commissioning_scripts.py
/workspace/Arbor/.venv/bin/ruff check \
  scripts/commission_aros_principal_loop.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git diff --check
git add scripts/commission_aros_principal_loop.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git commit -m "test(aros): orchestrate mediated commissioning"
~~~

## Task 4: Execute the real Task→Eval lineage

**Files:**

- Runtime evidence only; do not edit production code in this task.

- [ ] **Step 1: Start the real Principal**

Require:

~~~bash
export AROS_COMMISSION_MODEL="provider/model"
export AROS_COMMISSION_RESTART_MODEL="provider/model"
/workspace/Arbor/.venv/bin/python scripts/commission_aros_principal_loop.py start
~~~

The driver fails closed if model credentials, tmux, isolated-linux prerequisites, pinned commits, or clean worktrees are missing.

- [ ] **Step 2: Have the Principal start and inspect Task**

The Principal sees the packet and fixed adapter/acceptance boundary. It calls Task create. The owning service generates an empty-assimilation operational intent, and the host uses the same ProContract/checkpoint/CAS core to admit the brief. Verify candidate and canonical HEAD advance together and become clean before Task start. The Principal then calls only bounded Task actions: start, status, preserve. The deterministic adapter creates candidate C and return R in the Task worktree.

- [ ] **Step 3: Evaluate C before collection dirt can block Eval**

Derive:

~~~bash
R=$(git -C "$CANDIDATE/.worktree/tasks/$TASK_ID" rev-parse HEAD)
C=$(git -C "$CANDIDATE/.worktree/tasks/$TASK_ID" rev-parse HEAD^)
~~~

The Principal calls Eval run on C. Eval internally creates the sole durable Run through RunService.prepare_bundle/start. Do not launch an unrelated standalone Run.

- [ ] **Step 4: Observe and collect**

The Principal calls Eval status/observe/audit, then Task collect. The host records:

~~~text
Task brief/collection hashes
Task-brief operational proposal/audit/admission/commit hashes
B, C, R commits
Eval ID and receipt hash
Run ID, manifest/final hashes
candidate/apparatus commits
metric/parser/sample count
raw output receipts
~~~

- [ ] **Step 5: Assert exact lineage immediately**

~~~bash
/workspace/Arbor/.venv/bin/python scripts/verify_aros_principal_loop_commissioning.py \
  "$EVIDENCE_DIR" --phase observations
~~~

Expected: exit 0 and explicit child_commit == candidate_commit. Stop if unequal.

## Task 5: Execute real Principal assimilation and canonical admission

**Files:**

- Runtime candidate semantic files and transition only.

- [ ] **Step 1: Rebuild Attention**

The Principal calls Research attention. Both Task collection and Eval receipt must appear in unassimilated_returns with provenance, stale-base, diff, apparatus, and counterevidence pointers.

- [ ] **Step 2: Require observation before interpretation**

The Principal reads Task return, child diff, Eval receipt, apparatus manifest, raw output, current Claim, Question, NOW, and counterevidence. Reviewer PASS and process exit zero are explicitly insufficient.

- [ ] **Step 3: Author only changed meaning**

The Principal:

1. adds the strict measurement EvidenceLink JSON line to C-0001;
2. records the Task-return rationale and remaining uncertainty in NOW;
3. writes the exact four-field proposal with two assimilations;
4. leaves unrelated dirty canary bytes untouched.

The driver records Claim/NOW/proposal hashes immediately before the Principal turn and never opens those paths for writing afterward. The verifier correlates changed hashes with OpenCode file-tool and Research tool-call events from the active Session/assistant message IDs; a prewritten proposal or driver mutation fails commissioning.

- [ ] **Step 4: Audit and checkpoint**

The Principal calls transition_audit, inspects mechanically_valid testimony, then calls checkpoint. The host performs prepare→ProContract allow→finalize. The broker imports exact candidate/Task objects without updating any pre-admission ref, then uses one update-ref transaction whose canonical CAS condition also creates the audited immutable observation ref.

- [ ] **Step 5: Verify the canonical commit**

The independent verifier asserts:

~~~text
old canonical ref -> exactly one new commit
new commit parent == old ref
tree == audited candidate tree + admission.json
proposal/audit/admission/Claim/NOW/new observation closure all present
Task observation ref points to return commit R
Eval observation ref points to candidate commit C
R^ == C
collected.return_commit == R
collected.child_commit == MeasurementReceipt.candidate_commit == C
AdmissionReceipt subject/audit/binding/closure/fence/budget all match
stored admission/fence bytes equal ProContract canonical bytes and Python golden decoding
unrelated candidate dirt remains byte-identical and uncommitted
ProContract Contract remains active; admission did not discharge it
~~~

## Task 6: Destroy and restart the Principal

**Files:**

- Runtime process/DB/evidence only.

- [ ] **Step 1: Destroy the first host process**

Send SIGKILL to the embedded host after canonical CAS and record PID/start token/exit. Do not use a graceful same-session continuation as evidence.

- [ ] **Step 2: Start a fresh host**

Start a new broker and worker OS process against the same canonical/candidate/ProContract DB after lease recovery. Before admitting another mutation, broker reconcile finishes any canonical→candidate projection and confirms candidate HEAD equals canonical HEAD while preserving runtime/dirty canary. Require a new Session or prompt/attempt fence. Disable provider memory and do not load the first transcript.

- [ ] **Step 3: Switch model/context epoch**

Use AROS_COMMISSION_RESTART_MODEL and force a new Session Context Epoch. The Attention source must be reloaded from canonical workspace/runtime, not copied from old system text.

- [ ] **Step 4: Verify the new packet**

The packet must:

- retain active/focus Question, updated Claim, counterevidence, uncertainty, obligations, budget, authority, and blockers;
- show the latest EvidenceLink delta;
- omit both Task collection and Eval receipt from unassimilated_returns;
- remain at most 8,000 UTF-8 characters;
- contain no first-session ID/transcript summary as required scientific memory.

- [ ] **Step 5: Ask the fresh Principal to explain**

The new Principal explains why the Claim changed, what remains unknown, and why continue/stop/pivot is now worth considering. Treat this prose only as a commissioning observation; verify every stated fact against packet/files/receipts.

## Task 7: Prove non-bypass and fail-closed authority

**Files:**

- Runtime evidence and independent verifier cases.

- [ ] **Step 1: Prove the model has no unrestricted process tool**

Capture exact advertised tool definitions. Bash, shell, human-direct checkpoint, contract issue/attest/release, and raw canonical-ref mutation must be absent.

- [ ] **Step 2: Run candidate boundary probes**

From model-visible tools/probes, prove:

- external canonical path read/write is denied;
- candidate .git does not share canonical common dir;
- candidate has no canonical remote/alternate;
- changing candidate refs cannot change canonical refs;
- broker credential/database/authority-domain state and canonical mount are absent from worker/candidate files and model environment;
- human-direct route is not representable.

- [ ] **Step 3: Pollute candidate audit environment**

Add fake arbor modules and executables under candidate PATH/PYTHONPATH names. Re-run audit. The pinned python -I bootstrap and trusted closure hash remain unchanged; candidate code cannot audit itself.

- [ ] **Step 4: Exercise authority denials**

In fresh cases, attempt checkpoint with:

~~~text
expired lease
wrong prompt/attempt fence
wrong workspace/canonical ref
missing checkpoint capability
exhausted action budget
disallowed evaluator policy
wrong ResearchContractBinding hash
wrong audit closure hash
~~~

Each yields a typed deny receipt and unchanged canonical ref. Read-only attention/audit still works.

- [ ] **Step 5: Verify all probes**

Run the independent verifier with --phase authority. Expected: exit 0 only when every denial/non-bypass receipt is present and no forbidden canonical mutation occurred.

## Task 8: Execute the real crash matrix

**Files:**

- Runtime crash-cases and evidence only.

- [ ] **Step 1: Prepare one identical audited transition per case**

Use fresh canonical/candidate/Contract roots for every crash point. Do not reuse a scientific Eval attempt; reuse only copied immutable fixture observations.

- [ ] **Step 2: SIGKILL at each durable marker**

The driver enables the broker-only CheckpointBarrier control FD. At each case the broker fsyncs the named create-once marker, sends READY on the control FD, and blocks before the next operation. The driver confirms the exact marker/READY pair and sends SIGKILL instead of ACK at:

~~~text
before allow receipt
allow receipt before CAS
commit object before CAS
CAS after ref update before candidate/index repair
CAS conflict against another exact ref update
CAS after ref update before event/cache
~~~

Use real SIGKILL and fresh processes, not monkeypatched exceptions.

- [ ] **Step 3: Reconcile in a fresh process**

Expected:

~~~text
first three cases: old HEAD, edits preserved
post-CAS cases: one complete new HEAD, only admitted paths repaired
CAS conflict: old intent remains stale, no rebase/retry
missing event/cache: rebuilt from admitted Git
~~~

An allow receipt before failed CAS remains charged and unused. It may be reused only if every live fence/binding/subject still matches.

- [ ] **Step 4: Verify matrix**

Run verifier --phase crash. It checks refs, parents, trees, index entries, dirty canary, receipts, events, and absence of duplicated Task/Run/Eval attempts.

## Task 9: Run full verification and publish exact evidence

**Files:**

- Create: docs/analysis/aros-principal-research-loop-smoke.md
- Modify: docs/document_registry.json
- Modify: docs/aros/README.md
- Modify: memory/NOW.md

- [ ] **Step 1: Re-run Arbor verification**

~~~bash
cd /workspace/Arbor/.worktree/aros-principal-loop
/workspace/Arbor/.venv/bin/python -m pytest -q
/workspace/Arbor/.venv/bin/ruff check src/aros tests/test_aros_*.py \
  scripts/commission_aros_principal_loop.py \
  scripts/verify_aros_principal_loop_commissioning.py
git diff --check
~~~

- [ ] **Step 2: Re-run OpenCode package verification**

~~~bash
cd /workspace/opencode/.worktree/aros-admission/packages/schema
bun test test/pro-contract-admission.test.ts
bun typecheck

cd /workspace/opencode/.worktree/aros-admission/packages/core
bun script/migration.ts --check
bun test test/pro-contract-research.test.ts test/pro-contract.test.ts \
  test/session-runner-tool-registry.test.ts test/application-tools.test.ts \
  test/session-runner.test.ts test/location-layer.test.ts
bun typecheck

cd /workspace/opencode/.worktree/aros-admission/packages/sdk-next
bun test test/aros-principal-commission.test.ts test/embedded.test.ts
bun typecheck
~~~

- [ ] **Step 3: Run final exact verifier**

~~~bash
cd /workspace/Arbor/.worktree/aros-principal-loop
/workspace/Arbor/.venv/bin/python scripts/verify_aros_principal_loop_commissioning.py \
  .worktree/commissioning/aros-principal-loop/evidence --phase all
~~~

Expected: exit 0 with exact IDs/hashes/counts and no warnings.

- [ ] **Step 4: Request independent reviews**

Request:

1. Design Book/spec compliance review.
2. ProContract authority/non-bypass/security review.
3. Simplicity/complexity-inversion review.
4. Final evidence/claim-scope review.

Resolve every Critical/Important and rerun affected verification.

- [ ] **Step 5: Write and register evidence**

Record exact:

~~~text
Arbor base/final commits and source hash
OpenCode dev base/final commits and binary/source hash
model/provider/variant for both sessions
OpenCode Session/assistant/tool-call events proving Principal-authored edits and transition
contract/revision/spec/binding/lease/session/prompt/attempt IDs
budget before/charge/remaining
Task B/C/R and collection hash
Eval/Run/apparatus/measurement/raw-output hashes
proposal/audit/admission/commit/tree hashes
old/new canonical refs
Attention packet hashes and sizes
non-bypass deny receipts
six crash-case receipts
all test/typecheck/lint commands and counts
explicit residual non-claims
~~~

Register the smoke document as current/informative/on_demand only after all reviews pass. Update docs/aros/README.md from Not yet implemented to the exact commissioned capability; do not claim protected Eval, source, Skills, MCP, provider parity, or Arbor retirement.

- [ ] **Step 6: Commit evidence**

~~~bash
git add docs/analysis/aros-principal-research-loop-smoke.md \
  docs/document_registry.json docs/aros/README.md memory/NOW.md
git commit -m "docs(aros): commission mediated principal loop"
~~~

## Final commissioning gate

This plan is complete only when all of the following are proven by current exact evidence:

- one real Principal receives the bounded packet without transcript;
- one real Task collection and one real Eval MeasurementReceipt share the exact candidate commit;
- the Principal explicitly assimilates both, with EvidenceLink only for the measurement;
- TransitionAudit contains no scientific verdict;
- ProContract owns the allow receipt and exact authority/budget/fence decision;
- Git CAS is the only canonical linearization point;
- the first host is killed and a fresh model/session resumes from canonical state;
- both observations are no longer pending while uncertainty remains visible;
- no Bash/human route/canonical credential/ref authority is model-reachable;
- every authority denial and real crash point fails closed;
- all Arbor/OpenCode tests, typechecks, lint, independent reviews, and exact verifier pass.

Even after this gate, keep the overall AROS goal active: Source Gateway, protected Eval, project Skills/MCP/provider parity, K/M/G migration, default-path switch, and Arbor retirement remain.
