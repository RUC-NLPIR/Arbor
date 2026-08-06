# AROS Principal Loop Cooperative E2E Evidence

## Result

AROS completed one real Arbor-native cooperative vertical slice from the clean
source commit `19e4cf5`:

```text
aros wheel/console entry
→ Task brief checkpoint
→ real tmux Task adapter
→ child commit C + return commit R
→ Task collection checkpoint
→ real visible Eval backed by Run
→ valid MeasurementReceipt for C
→ explicit Task + Measurement assimilation
→ cooperative human-direct Git CAS
→ fresh-process Attention restart
```

The independent verifier returned:

```json
{
  "commit": "8bf357569fa7a784a0879afa0552cadeb29da659",
  "enforcement_class": "cooperative",
  "eval_id": "EVAL-3f8827231c530d3c42fc30f85e399d8f3b26f5999bec9664cf49da389cab64c2",
  "schema_version": 1,
  "state": "verified",
  "task_id": "TASK-20260805-produce-one-deterministic-succes-c581c220cc22bf86"
}
```

This is cooperative evidence only. The Principal and canonical Git shared one
writable OS identity. It does not prove mediated, protected, or non-bypass
authority.

## Exact lineage

- Task candidate commit `C`:
  `2fb60162d8869b756739fecaef3b45bb1bda5d2e`
- Task return commit `R`:
  `4fd16a450a11e4ae557381e92317ac989d334d7d`
- Eval candidate commit:
  `2fb60162d8869b756739fecaef3b45bb1bda5d2e`
- Measurement: `principal_loop_quality=1.0`, `measurement_state=valid`
- Final assimilation commit:
  `8bf357569fa7a784a0879afa0552cadeb29da659`
- Human-direct receipt SHA-256:
  `788ed24bd3f14d570a4c440257cb7c16a21f27888376354caf86950aebf5fee2`

The final proposal contained two separate assimilations. The Task collection
updated `memory/NOW.md`; the Eval receipt updated the Claim and NOW. The Claim
contained one strict `supports` EvidenceLink. The immutable Task observation ref
targets `R`; the immutable measurement ref targets `C`.

## Restart behavior

The driver launched 17 real `aros` subprocesses and recorded zero failures.
Every boot was a fresh process; no transcript or provider memory was replayed.

1. With the rebuilt disposable transition index, both assimilated observations
   were absent from `unassimilated_returns`, and
   `recent_evidence_delta` named `T-E2E-ASSIMILATE`.
2. With the cache moved aside, both observations conservatively reappeared and
   `warnings` contained `index_incomplete`.
3. A fresh explicit rebuild again removed both observations while preserving the
   latest evidence transition.

This proves canonical Git is authoritative and the cache is not.

## Reproduction

Install the current Arbor source so that the direct `aros` console entry exists,
then run:

```bash
python scripts/commission_aros_principal_loop.py \
  --aros "$(command -v aros)" \
  --runtime /absolute/ignored/runtime

python scripts/verify_aros_principal_loop_commissioning.py \
  /absolute/ignored/runtime/evidence.json
```

The driver requires an absent runtime path and never cleans an earlier failure
site. The retained local evidence for this run is:

```text
/workspace/Arbor/.worktree/commissioning/aros-e2e-final-run-1/evidence.json
```

## E2E blockers found and fixed

- `bae5ba9`: checkpoint runtime integrity now compares the portable Git
  executable mode class instead of non-portable Unix read/write bits.
- `1f68d66`: Task validation no longer compares a stale worktree-list HEAD
  snapshot while the child is committing; attached branch, actual tip, branch
  tip, common Git directory, and base ancestry remain enforced.
- `686177e`: an anchored `.git/` directory binds identity rather than mutable
  directory timestamps; linked-worktree `.git` marker files remain exact.
- `19e4cf5`: deterministic fixture, one-command driver, and independent verifier.

## What remains

- The semantic edits in this commissioning run were deterministic driver input,
  not a live LLM Principal turn.
- Task/Run/Eval operational records still require explicit cooperative
  checkpoints; automatic foreground operational admission remains Task 9 work.
- Same-UID cooperative execution is not protected authority. The Arbor-native
  broker/lease/fence authority domain and non-bypass commissioning remain.
- Protected Eval, Source Gateway, Skills/MCP/provider parity, and final Arbor
  compatibility retirement remain outside this proof.

## Repository verification

After the clean-wheel commissioning run:

- focused Principal Loop and commissioning suites exited 0;
- architecture, public-entry, and document-registry gates exited 0;
- Ruff reported `All checks passed!` and `git diff --check` exited 0;
- the exact unqualified full command `/workspace/Arbor/.venv/bin/python -m
  pytest -q` exited 0 in 833.44 seconds;
- collection reported 2,105 tests; the run displayed 6 skips and no failures
  (2,099 passed, 6 skipped).
