# AROS Live Principal Loop Cooperative E2E Evidence

## Result

Clean product commit `9a51787` completed the real Arbor-native Agent loop after
native Question-centered intake replaced the old initialization command:

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
  "commit": "a0f10647738e16eb5b9d0f9a7a2926c8d35813d3",
  "enforcement_class": "cooperative",
  "eval_id": "EVAL-3f8827231c530d3c42fc30f85e399d8f3b26f5999bec9664cf49da389cab64c2",
  "schema_version": 1,
  "state": "verified",
  "task_id": "TASK-20260806-produce-one-deterministic-succes-20ecf7da52eefed7"
}
```

This is live-Agent and cooperative evidence. It does not claim external-model
scientific quality, mediated authority, protected evaluation, or non-bypass
same-UID containment.

## Clean wheel

- Source commit: `9a5178773`
- Wheel: `arbor_agent-0.1.1.dev456+g9a5178773-py3-none-any.whl`
- Wheel SHA-256:
  `0dc68243f6f55eae050fb2850cc218e121277e9b74b976a355918a0487a61a39`
- Wheel environment used a normal dependency-resolving install from the
  existing `pyproject.toml`; it used no editable install, `.pth` dependency
  fallback, `PYTHONPATH`, or `uv`.

The retained evidence is:

```text
/workspace/Arbor/.worktree/commissioning/aros-principal-loop-native-intake-run-1/evidence.json
```

## Agent provenance

The primary object was `arbor.core.agent.Agent`. It finished after 15 provider
turns with this exact normalized tool sequence:

```text
Research.attention
Task.create
Task.start
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
f8f4e2ef30552fb8b30fb9e2fcf6f43864e0f903ca566a1cc30377caf9a35e49  knowledge/claims/C-0001.md
652ecef4974ed6c576b619efd2852a33725c7c7889c017eaa671d292c9f4f8ba  memory/NOW.md
9560bd5702fa80fd53c9444386939e504cf5071cfcdf0d4de8cbd9fa990ef824  transitions/T-E2E-ASSIMILATE/proposal.json
```

The commissioning driver creates only the initial workspace, Task adapter,
evaluator, Claim baseline, and evaluator registration. It has no direct
TaskTool, EvalTool, semantic writer, TransitionAudit, or checkpoint path after
the Agent starts.

## Exact lineage

- Task candidate commit `C`:
  `062f069b85297d723f53137e00aaa8092642b961`
- Task return commit `R`:
  `58dcc055258c25318e47831a31fab3a341371210`
- Task collection SHA-256:
  `e88dabd34f66269dda3cd37ca03dc637084ce1a93eb09f59d0273de521467453`
- Eval candidate commit:
  `062f069b85297d723f53137e00aaa8092642b961`
- Measurement: `principal_loop_quality=1.0`, `measurement_state=valid`
- Eval receipt SHA-256:
  `6a5bd5f153f83ed40b98099d361765afee92e4d869bb1f6a137dbea6b34dab58`
- Assimilation base:
  `bdfc68b192f5d1a7d9e3a6a7ba573e9a76d77cad`
- Final assimilation commit:
  `a0f10647738e16eb5b9d0f9a7a2926c8d35813d3`
- Cooperative admission receipt SHA-256:
  `806cac45ae99d68c403dd66b936b4eb1307c40e0b0443e79dfa0c3f03dda03ff`

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

Its packet named final commit `a0f1064`, contained transition
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
- collection reports 2,146 tests; the full run displayed 6 skips and no
  failures (2,140 passed, 6 skipped);
- Ruff reported `All checks passed!`;
- `git diff --check` exited 0 and the worktree was clean.
