# AROS Native Start and Local Intake E2E Evidence

## Result

The clean wheel from product commit `9a51787` completed the native entry:

```text
aros start --question ... --material local.md
-> new main Git workspace
-> exact Q-0001 + source bytes/hash/extraction/provenance
-> one AROS Intake commit
-> bounded canonical Attention
-> real arbor.core.agent.Agent
-> Agent reads Question and extracted source
-> primary Agent destroyed
-> initialized aros start restart with zero messages
-> Research.attention recovers Q-0001
```

The command and a separate verifier invocation both returned:

```json
{
  "commit": "a826f280ac7ebccaff2134ab122528f27d00c435",
  "question_id": "Q-0001",
  "schema_version": 1,
  "source_id": "SRC-7c6e586645f21eda",
  "state": "verified"
}
```

Retained evidence:

```text
/workspace/Arbor/.worktree/commissioning/aros-native-start-run-1/evidence.json
```

## Clean wheel

- Wheel: `arbor_agent-0.1.1.dev456+g9a5178773-py3-none-any.whl`
- SHA-256:
  `0dc68243f6f55eae050fb2850cc218e121277e9b74b976a355918a0487a61a39`
- Normal dependency-resolving install into a fresh venv.
- No editable install, `.pth`, PYTHONPATH fallback, or `uv`.

## Exact bootstrap

- Human Question:
  `What mechanism explains the deterministic local observation?`
- Initialization commit:
  `a826f280ac7ebccaff2134ab122528f27d00c435`
- Commit author:
  `AROS Intake <aros-intake@local.invalid>`
- Source content SHA-256:
  `7c6e586645f21eda27e9ada68ea14962c3ab1c0e5d18b4b04dee06727a356838`
- Source paths:
  `original.md`, `extracted.md`, and `metadata.json` under
  `sources/papers/SRC-7c6e586645f21eda/`.

The verifier reads exact Git objects and rejects a changed Question, source
hash, metadata hash, empty extraction, invented bootstrap Idea/Claim, wrong
author, missing Agent reads, transcript reuse, or legacy command resurrection.

## Agent and restart

The primary native Agent began with zero messages and called exactly:

```text
Read questions/Q-0001/question.md
Read sources/papers/SRC-7c6e586645f21eda/extracted.md
```

The restart Agent also began with zero messages and called only
`Research.attention`. No provider memory or primary transcript was supplied.

## Public surface

- Direct `aros` exposes `start` and no `init` command.
- Legacy `arbor` does not mount an `aros` command.
- `src/cli/app.py` has no AROS forwarding warning or sunset exception.

## Boundary

This proves native product intake, canonical local source provenance, Principal
boot, and restart. It does not prove remote sources, an external-model
scientific result, child research, Source Gateway, Skills/MCP parity, or
protected authority.
