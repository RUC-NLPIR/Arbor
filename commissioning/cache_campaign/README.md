# Cache-campaign substrate operator contract

This directory is a commissioning-only factual apparatus. It prepares and
measures cache-policy evidence; it does not interpret a Claim, score a
campaign, demonstrate Researcher capability, or establish scientific quality.
All source and trace provisioning is host work performed before an Agent is
started.

## Authority and roots

Use two physically separate roots:

```bash
export TASK=/absolute/path/to/researcher-task-root
export HOST=/absolute/path/to/host-only-cache-campaign
export LIBCACHESIM=/absolute/path/to/host-provisioned/libCacheSim
export BASE=da022c2945146e9577d91375a48d53850d7041a3
```

`TASK` is the task-visible project root. Dev and visible-validation manifest
paths may be visible there. `HOST` holds the candidate manifest input, source
receipt, private R3 manifest and bytes, calibration, retained index, and R3
output. `HOST`, the private trace paths, and the R3 authority files must remain
outside `TASK`. Trace bytes, raw outputs, private manifests, and ledgers must
never be added to Git.

The host must provision the exact libCacheSim remote and pinned commit before
running these commands. The preparation command validates and builds that
existing checkout; it never clones, fetches, resets, or cleans it.

## Prepare source and freeze data boundaries

```bash
python scripts/prepare_aros_cache_source.py \
  --checkout "$LIBCACHESIM" \
  --receipt "$HOST/source.json"

python scripts/freeze_aros_cache_manifests.py \
  --input "$HOST/candidate-input.json" \
  --task-root "$TASK" \
  --task-output "$TASK/manifests" \
  --host-output "$HOST/sealed"
```

The freezer writes `TASK/manifests/task.json` with dev/visible facts and only
the private-manifest commitment. It writes `HOST/sealed/r3.json` separately.
Every trace window is continuous; random request sampling is forbidden.

## Run all six R0 policy checks

Calibration requires one successful R0 receipt for every comparison policy,
not only the two constraint references:

```bash
for POLICY in LRU ARC WTinyLFU Sieve S3FIFO BeladySize; do
  SLUG=$(printf '%s' "$POLICY" | tr '[:upper:]' '[:lower:]')
  python scripts/run_aros_cache_eval.py \
    --rung r0 \
    --checkout "$LIBCACHESIM" \
    --candidate "$BASE" \
    --base "$BASE" \
    --policy "$POLICY" \
    --source-receipt "$HOST/source.json" \
    --output "$HOST/r0-$SLUG"
done
```

Each R0 retains build/test, deterministic simulation, sanitizer, capacity,
metadata allocation-probe, scope, interposer, command, source, artifact, and
evidence-inventory facts independently. Operational metadata is measured, but
allocation coverage and amortized O(1) compliance remain
`pending_independent_review` until an independent source/allocation audit.

## Run R1 and the fourteen R2 calibration inputs

R1 is the first three dev windows at all three fractions. Every portfolio run
requires the explicit task root:

```bash
python scripts/run_aros_cache_eval.py \
  --rung r1 \
  --task-root "$TASK" \
  --checkout "$LIBCACHESIM" \
  --candidate "$BASE" \
  --policy Sieve \
  --task-manifest "$TASK/manifests/task.json" \
  --source-receipt "$HOST/source.json" \
  --r0-receipt "$HOST/r0-sieve/receipt.json" \
  --output "$HOST/r1-sieve"
```

Run five R2 repetitions for each constraint reference and one R2 comparison
for each other policy:

```bash
for POLICY in Sieve S3FIFO; do
  SLUG=$(printf '%s' "$POLICY" | tr '[:upper:]' '[:lower:]')
  for REPETITION in 1 2 3 4 5; do
    python scripts/run_aros_cache_eval.py \
      --rung r2 --task-root "$TASK" --checkout "$LIBCACHESIM" \
      --candidate "$BASE" --policy "$POLICY" \
      --task-manifest "$TASK/manifests/task.json" \
      --source-receipt "$HOST/source.json" \
      --r0-receipt "$HOST/r0-$SLUG/receipt.json" \
      --output "$HOST/r2-$SLUG-$REPETITION"
  done
done

for POLICY in LRU ARC WTinyLFU BeladySize; do
  SLUG=$(printf '%s' "$POLICY" | tr '[:upper:]' '[:lower:]')
  python scripts/run_aros_cache_eval.py \
    --rung r2 --task-root "$TASK" --checkout "$LIBCACHESIM" \
    --candidate "$BASE" --policy "$POLICY" \
    --task-manifest "$TASK/manifests/task.json" \
    --source-receipt "$HOST/source.json" \
    --r0-receipt "$HOST/r0-$SLUG/receipt.json" \
    --output "$HOST/r2-$SLUG"
done
```

Do not replace a failed or noisy receipt. Preserve it and begin a new campaign
calibration identity.

## Freeze calibration

The calibrator accepts exactly six R0 receipts and fourteen R2 receipts: five
each for Sieve and S3FIFO, then one each for LRU, ARC, WTinyLFU, and
BeladySize.

```bash
python scripts/calibrate_aros_cache_baselines.py \
  --task-manifest "$TASK/manifests/task.json" \
  --r0-receipt "$HOST/r0-lru/receipt.json" \
  --r0-receipt "$HOST/r0-arc/receipt.json" \
  --r0-receipt "$HOST/r0-wtinylfu/receipt.json" \
  --r0-receipt "$HOST/r0-sieve/receipt.json" \
  --r0-receipt "$HOST/r0-s3fifo/receipt.json" \
  --r0-receipt "$HOST/r0-beladysize/receipt.json" \
  --receipt "$HOST/r2-sieve-1/receipt.json" \
  --receipt "$HOST/r2-sieve-2/receipt.json" \
  --receipt "$HOST/r2-sieve-3/receipt.json" \
  --receipt "$HOST/r2-sieve-4/receipt.json" \
  --receipt "$HOST/r2-sieve-5/receipt.json" \
  --receipt "$HOST/r2-s3fifo-1/receipt.json" \
  --receipt "$HOST/r2-s3fifo-2/receipt.json" \
  --receipt "$HOST/r2-s3fifo-3/receipt.json" \
  --receipt "$HOST/r2-s3fifo-4/receipt.json" \
  --receipt "$HOST/r2-s3fifo-5/receipt.json" \
  --receipt "$HOST/r2-lru/receipt.json" \
  --receipt "$HOST/r2-arc/receipt.json" \
  --receipt "$HOST/r2-wtinylfu/receipt.json" \
  --receipt "$HOST/r2-beladysize/receipt.json" \
  --output "$HOST/baseline-calibration.json" \
  | tee "$HOST/calibration-publication.json"
```

Retain the printed `calibration_sha256` separately. Later comparisons and R3
require that external digest, and the calibration file must remain read-only.

## Optional temporal-sealed R3

R3 is allowed only after the candidate, Claim, preregistration, Reviewer report,
Principal response, reproduction descriptor, candidate R0, and candidate R2
are frozen. The ledger is not a caller-chosen file under `HOST`: it is the fixed
per-UID path
`<passwd-home>/.local/state/aros/cache-campaign-r3/r3-<authority-id>.consumed.json`.
The authority ID is SHA-256 of the concatenated frozen commit, R3 commitment,
candidate commit, `/`, and policy. Supply that exact canonical path to the CLI.

```bash
export CALIBRATION_SHA256=$(python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["calibration_sha256"])' \
  "$HOST/calibration-publication.json")
export AUTHORITY_ID=$(python -c \
  'import hashlib,json,sys; p=json.load(open(sys.argv[1])); print(hashlib.sha256((p["frozen_commit"]+p["r3_commitment_sha256"]+p["candidate_commit"]+"/"+p["policy"]).encode()).hexdigest())' \
  "$HOST/frozen-package.json")
export PASSWD_HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
export R3_LEDGER="$PASSWD_HOME/.local/state/aros/cache-campaign-r3/r3-$AUTHORITY_ID.consumed.json"

python scripts/run_aros_cache_r3.py \
  --frozen-package "$HOST/frozen-package.json" \
  --host-r3-manifest "$HOST/sealed/r3.json" \
  --calibration "$HOST/baseline-calibration.json" \
  --calibration-sha256 "$CALIBRATION_SHA256" \
  --source-receipt "$HOST/source.json" \
  --candidate-r0-receipt "$HOST/candidate-r0/receipt.json" \
  --checkout "$LIBCACHESIM" \
  --ledger "$R3_LEDGER" \
  --output "$HOST/r3-result"
```

The host consumes the ledger before launch. R3 never retries in the same
campaign, including after process or publication failure. R3 facts may not be
used to revise the policy, Claim, package, or preregistration.

## Retained verification

The retained index has exact schema version 1 and names the checkout, task
root, source receipt, public/private manifests, six R0 receipts, one or more R1
receipts, fourteen R2 receipts, read-only calibration plus its externally
retained digest, and either `r3: null` or the five R3 paths described by the
verifier tests.

```bash
python scripts/verify_aros_cache_substrate.py "$HOST/retained/index.json"
```

Exit 0 means the retained substrate is structurally verified. Exit 2 means an
input, byte, Git, schema, chronology, or provenance binding is invalid. The
output reports source, data-boundary, R0, R1, R2, calibration, optional R3, and
unresolved-audit states independently. It deliberately emits no overall
scientific pass or campaign score.
