# AROS Live Principal Loop Cooperative E2E Evidence

## Result

Clean source commit `884439f` completed the real Arbor-native Agent loop:

```text
fresh Agent.run + Research Attention
-> Agent Task create/start/status/collect
-> real tmux child commit C + return commit R
-> Agent Eval.run backed by durable Run
-> valid MeasurementReceipt for C
-> Agent reads Claim/NOW and writes Claim/NOW/proposal
-> Agent Research transition_audit/checkpoint
-> cooperative Git CAS
-> primary Agent/provider destroyed
-> disposable assimilation index rebuilt from canonical Git
-> fresh Agent.run + Research Attention
```

The commissioning command and a separate verifier invocation both returned:

```json
{
  "commit": "3733acc61c06e652f0fd0dd90c07f81ed17bcc28",
  "enforcement_class": "cooperative",
  "eval_id": "EVAL-3f8827231c530d3c42fc30f85e399d8f3b26f5999bec9664cf49da389cab64c2",
  "schema_version": 1,
  "state": "verified",
  "task_id": "TASK-20260806-produce-one-deterministic-succes-9fb25637cb727da3"
}
```

This is live-Agent and cooperative evidence. It does not claim external-model
scientific quality, mediated authority, protected evaluation, or non-bypass
same-UID containment.

## Clean wheel

- Source commit: `884439fe8`
- Wheel: `arbor_agent-0.1.1.dev445+g884439fe8-py3-none-any.whl`
- Wheel SHA-256:
  `4b17dd0f4aedf8479e2e059bc779e7f4ac69113d52cd1645ea7e5c2b196e5075`
- Wheel environment used a normal dependency-resolving install from the
  existing `pyproject.toml`; it used no editable install, `.pth` dependency
  fallback, `PYTHONPATH`, or `uv`.

The retained evidence is:

```text
/workspace/Arbor/.worktree/commissioning/aros-live-agent-run-3/evidence.json
```

## Agent provenance

The primary object was `arbor.core.agent.Agent`. It finished after 16 provider
turns with this exact normalized tool sequence:

```text
Research.attention
Task.create
Task.start
Task.status
Task.status
Task.collect
Eval.run
Research.attention
Read Claim
Read NOW
Write Claim
Write NOW
Write proposal
Research.transition_audit
Research.checkpoint
```

The verifier requires the complete sequence and compares each final Git blob
byte-for-byte with its Agent `Write` argument:

```text
46b6a1868006df8712ca83e1ef8b1b9f81475c699b93af3a71da7524374692b3  knowledge/claims/C-0001.md
2eb75ef8a1b04602056cc4b7783368371d922c72b7498e931c6c7e7cfca51169  memory/NOW.md
bacaf1809d68c102eabddfa1df8a4b38d6cbef11ab89d1d9c95343ca301fefba  transitions/T-E2E-ASSIMILATE/proposal.json
```

The commissioning driver creates only the initial workspace, Task adapter,
evaluator, Claim baseline, and evaluator registration. It has no direct
TaskTool, EvalTool, semantic writer, TransitionAudit, or checkpoint path after
the Agent starts.

## Exact lineage

- Task candidate commit `C`:
  `32a38a1b0481eb20eb6d1a290a72b4d2eb2e9516`
- Task return commit `R`:
  `2809d85fac9fe49a7487e0e0cef3ac63cf6c0beb`
- Task collection SHA-256:
  `5414078422c8019d410550882c123b37cf07292bb06391f2970b1f1f486653b7`
- Eval candidate commit:
  `32a38a1b0481eb20eb6d1a290a72b4d2eb2e9516`
- Measurement: `principal_loop_quality=1.0`, `measurement_state=valid`
- Eval receipt SHA-256:
  `a90182a4c97e401914d167e200a314ad7caff5db09c5a76ac6d58abb26d15b0a`
- Assimilation base:
  `08e07918dc63524973cef0bba46420e077589663`
- Final assimilation commit:
  `3733acc61c06e652f0fd0dd90c07f81ed17bcc28`
- Cooperative admission receipt SHA-256:
  `2718961ef092daea9bbf50902f4cfa72e5fb7a5e61efe003e292f2d95c1eab51`

The final proposal contains one explicit Task assimilation and one explicit
measurement assimilation. The Claim contains one strict `supports`
EvidenceLink. Task collection and MeasurementReceipt identify the same
candidate commit.

## Restart behavior

The primary Agent and provider were garbage-collected before the restart Agent
was constructed. The restart Agent began with zero messages, invoked only
`Research.attention`, and finished in two turns with:

```text
Recovered admitted transition T-E2E-ASSIMILATE.
```

Its packet named final commit `3733acc`, contained transition
`T-E2E-ASSIMILATE` as the recent evidence delta, and had an empty
`unassimilated_returns` list. The driver used the existing explicit
`aros audit --rebuild-index` mechanical operation between sessions; the index
remains disposable and canonical Git remains authoritative.

## Reproduction

Build and normally install the current wheel into a fresh venv, then run:

```bash
<wheel-venv>/bin/python scripts/commission_aros_principal_loop.py \
  --aros <wheel-venv>/bin/aros \
  --runtime /absolute/absent/runtime

<wheel-venv>/bin/python scripts/verify_aros_principal_loop_commissioning.py \
  /absolute/absent/runtime/evidence.json
```

The runtime path must be absent. A failed scientific/process attempt is
preserved and a new attempt uses a new root.

## Failures observed before the accepted run

1. A dependency-free venv caused the isolated Task runner to fail importing
   declared runtime dependency `pydantic`; Task became `lost` and no scientific
   result was invented. The final run uses a normal wheel install with declared
   dependencies.
2. A valid primary transition followed by a missing disposable index caused
   the fresh Agent to see both returns conservatively with `index_incomplete`.
   The final driver invokes the existing explicit index rebuild before restart;
   it does not hide pending observations.

Neither failure retried or reinterpreted science under the same runtime root.

## What remains

- A real external model must perform a scientific turn on a non-fixture topic;
  this deterministic provider proves Agent-loop composition, not research
  quality.
- Same-UID cooperative execution is not protected authority. The Arbor-native
  broker/lease/fence domain and non-bypass commissioning remain.
- Protected Eval, Source Gateway, project-local Skills, AROS MCP/provider
  parity, full K/M/G, and final Arbor retirement remain outside this proof.

## Repository verification

After the retained live-Agent run:

- the focused AROS, architecture, public-entry, document, and commissioning
  suites exited 0;
- the exact unqualified `/workspace/Arbor/.venv/bin/python -m pytest -q`
  command exited 0;
- collection reported 2,130 tests; the run displayed 6 skips and no failures
  (2,124 passed, 6 skipped);
- Ruff reported `All checks passed!`;
- `git diff --check` exited 0 and the worktree was clean.
