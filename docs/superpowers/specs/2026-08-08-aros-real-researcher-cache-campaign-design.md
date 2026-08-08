# AROS Real Researcher Cache-Policy Campaign Design

Status: approved commissioning-only capability design; not an implementation claim
Date: 2026-08-08
Highest authority: `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`

## 1. Decision

Before Phase 0B or Mission Supervisor implementation, AROS will run one
commissioning-only capability experiment outside `src/aros`. The experiment
tests whether a real external-model Researcher can independently perform an
adaptive experimental research loop inside one existing AROS Task.

The campaign uses cache admission/eviction research over libCacheSim and real
production traces. It is not a scalar benchmark-optimization campaign. The
scientific target is mechanism discovery: identify a failure regime, distinguish
competing explanations, develop a minimal repair, and test transfer without
using the sealed set as an exploration oracle.

This campaign does not change the public product, add a Supervisor, or count as
Phase 0B completion. Any successful adapter, prompt, or procedure remains a
commissioning artifact until later product design and review.

## 2. Exact Root Question

> 在固定 metadata、摊销 O(1) 更新和吞吐约束下，S3-FIFO/SIEVE 的
> quick-demotion 与 lazy-promotion 机制在什么 workload regime 下失效？能否用一个
> 最小的 size/phase-aware 机制修复这些边界，并在未见生产 traces 上保持收益？

The driver must pass this question exactly. It must not provide candidate
hypotheses such as one-hit filtering, object-size effects, phase drift, or
cross-organization overfitting. Those are useful hidden review considerations,
not Researcher inputs.

## 3. Capability claim under test

The experiment tests this narrow claim:

> Given one bounded TaskBrief, a real Researcher can formulate falsifiable rival
> mechanisms, choose and revise experiments from observed evidence, survive a
> zero-message restart through workspace memory, preregister a discriminating
> confirmation, and return a reproducible scoped Claim without scientific
> steering from the Principal.

The experiment does not test:

- Mission Supervisor wakeups or indefinite operation;
- asynchronous multi-Researcher portfolio management;
- protected same-UID authority;
- project Skill promotion;
- MCP transport;
- general scientific superiority over Arbor;
- publication quality or state-of-the-art cache performance.

## 4. Subjects and authority

```text
Human / Campaign Driver
  -> exact question, repo, data manifests, budget, factual evaluators
Principal
  -> one immutable TaskBrief
Researcher session 1
  -> autonomous exploratory work and evidence-linked follow-up
destroy Researcher/provider
Researcher session 2
  -> zero-message recovery, preregistration, confirmation, Task return
Independent Reviewer
  -> fresh-context reproduction and adversarial critique
Principal
  -> accept, narrow, or reject; freeze policy and Claim package
Temporal-sealed R3
  -> one cross-organization transfer measurement
campaign ends
```

### 4.1 Driver

The driver may create only:

- the exact Root Question;
- a pinned libCacheSim repository and build environment;
- immutable dev, visible-validation, and R3 data manifests;
- source/paper entry points;
- factual R0–R3 evaluators;
- baseline calibration records;
- the approved resource and capability envelope;
- deterministic commissioning and verification machinery.

After the Researcher starts, the driver may not write a hypothesis, choose an
experiment, edit candidate code, interpret a result, draft a Claim, or repair a
Researcher report.

### 4.2 Principal

The Principal produces one TaskBrief and then stops scientific steering. During
execution it may respond only to:

- a request to reallocate or expand budget;
- a capability-boundary conflict;
- a blocker the Researcher cannot safely resolve.

It may stop the Task for budget or safety reasons. It may not suggest a
mechanism, policy structure, trace, cache size, experiment, or interpretation.

After Researcher and Reviewer returns, the Principal may accept, narrow, or
reject the Claim. It may not send the candidate back for scientific revision in
the same campaign.

### 4.3 Researcher

The Researcher is a real provider-backed Agent running inside one isolated AROS
Task worktree. It owns its scientific inner loop and may:

- read the allowed papers, code, and documentation;
- formulate and revise rival mechanisms;
- add exploratory instrumentation;
- build code and launch bounded CPU Runs;
- inspect raw and derived measurements;
- perform evidence-driven follow-up experiments;
- write research notes, preregistration, policy code, tests, and return report;
- request more budget or report a blocker through the Task mailbox.

It may not access R3, undeclared trace data, external write APIs, or canonical
Principal scientific files.

### 4.4 Reviewer

The Reviewer is a fresh external-model session with a separate worktree. It
receives no Principal or Researcher transcript. It receives only the TaskBrief,
preregistration, exact commits, source refs, R0–R2 receipts, raw outputs, and the
Researcher report.

The Reviewer may reproduce and critique. It cannot modify the candidate or
admit a canonical Claim.

## 5. Resource and permission envelope

The campaign total budget is:

```text
CPU budget              24 core-hours
maximum concurrent CPU   8 cores
Researcher turns       120
maximum concurrent Runs  3
workspace/artifact disk 20 GB
GPU                      unavailable
```

The Principal may reallocate this budget inside the Task. Exceeding any mission
total requires explicit human approval with achieved evidence, remaining
uncertainty, requested increment, and expected information gain.

Network policy:

- public papers, documentation, and source code may be read;
- authenticated research reads are allowed only if supplied by the host;
- experimental data is restricted to the frozen dev and visible-validation
  manifests;
- downloading additional traces is forbidden;
- publishing, uploading, purchasing compute, or modifying a remote system is
  forbidden without approval.

Credentials remain host-owned and must not enter prompts, logs, commits, or
receipts.

## 6. Repository and code boundary

The campaign uses a pinned libCacheSim source commit. Research has two code
tracks.

### 6.1 Exploratory track

The Researcher may add local instrumentation needed to measure mechanism
variables, including one-hit behavior, reuse distance, queue residence, ghost
hits, admission precision, and phase regret. Exploratory builds and measurements
must be labeled and cannot directly support a formal Claim.

### 6.2 Confirmatory track

The confirmation candidate may modify or add only:

- the candidate cache policy implementation;
- policy registration/build wiring strictly necessary to instantiate it;
- candidate-specific unit or invariant tests.

The following are fixed by the apparatus:

- simulator core;
- trace parsing and window construction;
- baseline implementations;
- evaluator and metric parser;
- data manifests;
- cache-size definitions;
- warm-up and measurement intervals.

The verifier must compare the confirmation commit against the pinned base and
reject any out-of-scope change.

## 7. Data manifest and contamination boundary

The driver freezes three disjoint data layers before Researcher launch.

### 7.1 Dev

- at least two organization or application sources;
- continuous windows with explicit warm-up;
- at least three cache sizes in the declared operating range;
- all bytes, origins, licenses, windows, and hashes recorded.

### 7.2 Visible validation

- paths are visible to the Researcher;
- applications or time periods differ from dev;
- no R3 organization/application appears;
- cache-size range matches dev and R3.

### 7.3 R3 temporal seal

- at least one unseen organization or application source;
- bytes and paths are not present in the Task worktree or TaskBrief;
- R3 is not run until the candidate policy, Claim, Reviewer response, and
  reproduction package are frozen in Git;
- it runs exactly once;
- its output cannot trigger candidate or Claim revision in this campaign.

Random request sampling is forbidden because it destroys reuse dynamics. Every
window is continuous and records its warm-up boundary. Hash and provenance audit
must reject overlap or duplication among the three layers.

## 8. Factual evaluation ladder

| Rung | Purpose | Target cost |
| --- | --- | ---: |
| R0 | build, unit tests, capacity conservation, determinism, memory safety, complexity audit | 1–5 seconds |
| R1 | three continuous windows × three cache sizes | 10–60 seconds |
| R2 | full dev/visible portfolio, baselines, ablations, mechanism observations | 1–5 minutes |
| R3 | one temporal-sealed cross-organization transfer measurement | 15–60 minutes |

R0 first measures SIEVE and S3-FIFO on the host. It freezes the eligible
baseline calibration and hard constraints before the Researcher acts:

- candidate throughput is at least 90% of the declared reference policy under
  the same apparatus;
- metadata stays inside the preregistered per-object/global budget;
- update work remains amortized O(1), confirmed by code/operation audit;
- capacity, determinism, memory, and scope checks pass.

The evaluator returns a Pareto vector rather than one score:

```text
object miss ratio
byte miss ratio
throughput
CPU time per request
metadata bytes per object and global metadata
```

Mechanism observations are factual diagnostics, not optimization targets:

- one-hit fraction;
- reuse-distance distribution;
- queue residence;
- ghost-hit and admission precision;
- phase-boundary regret;
- gap to declared baselines and oracle where available.

Only the Principal and Reviewer interpret scientific meaning.

## 9. Exploration and confirmation discipline

Exploratory Runs may be adaptive and need not be preregistered. They must retain
exact code, command, data, environment, output, and negative-result receipts.

Before the confirmation Run, the Researcher must checkpoint:

- at least two falsifiable rival mechanisms developed independently;
- the mechanism expected to survive and why;
- discriminating predictions;
- explicit falsifiers;
- controls and ablations;
- primary Pareto comparisons and hard constraints;
- transfer prediction and scope;
- stopping and rerun rules;
- exact confirmation evaluator version.

The confirmation evaluator and primary measurements cannot change after that
checkpoint. A new experiment requires a new action and idempotency key; the
original result remains immutable.

## 10. Observable Researcher capability gate

The campaign verifier must find all of these in model-authored artifacts and
receipts:

1. at least two independently authored falsifiable rival mechanisms;
2. at least three distinct experiment receipts;
3. at least one later experiment whose written rationale cites a specific prior
   observation and changes the next action;
4. a preregistration commit before the confirmation request;
5. evidence eliminating at least one rival mechanism;
6. one scoped surviving Claim supported by the confirmation evidence;
7. negative results and deviations, not only the best metric;
8. exact policy, tests, commands, environment, budget use, and remaining
   uncertainty;
9. a valid B-C-R Task return lineage;
10. no Principal scientific message after the immutable TaskBrief.

The driver may verify structure and provenance. It may not mechanically decide
whether the mechanism interpretation is scientifically correct.

## 11. Mandatory zero-message Researcher restart

After at least one evidence-driven follow-up experiment:

1. destroy the Researcher and provider objects;
2. record that no prior messages are supplied;
3. construct a fresh Researcher for the same Task worktree;
4. provide only the immutable TaskBrief and a bounded workspace/runtime packet;
5. require it to explain the current rivals, evidence, decisions, budget, and
   next experiment from durable files;
6. continue to preregistration, confirmation, and Task return.

Restart fails if the new Researcher:

- requires transcript replay;
- cannot explain why the prior follow-up occurred;
- repeats a completed experiment without new scientific reason;
- loses negative results or constraints;
- changes the Root Question or evaluator.

## 12. Independent review

The Reviewer must:

- rebuild the exact confirmation candidate;
- independently rerun the primary R2 measurement;
- compare raw and parsed outputs;
- inspect workload leakage, warm-up, cache-size, and contamination boundaries;
- test at least one alternative explanation or counterexample;
- audit hard constraints and amortized O(1) update behavior;
- check that the Claim wording does not exceed evidence scope;
- record unresolved objections and reproduction refs.

The Principal must answer every material objection by accepting, narrowing, or
rejecting the Claim. It cannot modify the policy or ask the Researcher to do more
science in this campaign.

## 13. Temporal-sealed transfer

After the policy, Claim, Reviewer report, Principal response, and reproduction
package are frozen, the host runs R3 once. R3 produces a factual receipt with the
same Pareto vector and hard-constraint facts.

R3 is an evaluation outcome, not an exploration tool. The campaign ends after
the receipt. No Task, Run, policy, preregistration, or Claim is changed in
response.

## 14. Outcome dimensions

The campaign reports three independent outcomes.

### 14.1 Researcher capability

Pass requires rival formation, adaptive evidence-linked action, zero-message
recovery, preregistration, confirmation, and a reproducible return. This can
pass even if the scientific result is negative or transfer fails.

### 14.2 Review robustness

Pass requires independent reproduction and no unanswered fatal Reviewer
objection. A correctly narrowed Claim may pass; an unreproduced or misleading
Claim fails.

### 14.3 Held-out transfer

R3 reports whether hard constraints and the preregistered directional transfer
prediction hold on unseen organizations/applications. It is reported separately
from Researcher capability and is never used to revise the campaign.

No aggregate scalar or single campaign pass hides these dimensions.

## 15. Failure semantics

The campaign distinguishes:

- source/network transport failure;
- provider transport failure;
- build or process failure;
- timeout, stop, lost, or uncontained descendants;
- invalid evaluator or data manifest;
- budget exhaustion;
- underpowered or contaminated measurement;
- scientific negative result;
- Reviewer refutation;
- transfer failure;
- authority or external-write blocker.

Only a bounded transport retry may repeat the same external call. Researcher
experiments, confirmation, Reviewer runs, and R3 require new actions and keys.
R3 never retries in the same campaign. Missing commits, hashes, receipts, or
environment bindings make the result operationally invalid rather than
scientifically negative.

## 16. Anti-cheating and provenance

The retained verifier must bind:

- exact AROS source, wheel, installed distribution, entrypoint, and interpreter;
- exact libCacheSim source commit and candidate diff;
- complete data manifests, hashes, windows, and split non-overlap;
- effective Principal, both Researcher sessions, and Reviewer model identities;
- complete normalized tool sequences and model-authored Write bytes;
- absence of Principal scientific messages after TaskBrief creation;
- every source, Task, Run, Eval, checkpoint, and Reviewer receipt;
- exact resource usage and budget decisions;
- Researcher/provider destruction and zero-message restart;
- Reviewer reproduction and Principal response;
- R3 request occurring after the frozen package and exactly once;
- final Git cleanliness and preservation of failed/dirty work.

The campaign fixture may write setup material only before the first Agent starts.
After start, direct fixture access to scientific writers, Task/Eval tools,
Researcher artifacts, Reviewer output, or canonical checkpoint paths is a
verification failure.

## 17. Delivery and exit

All new code for this campaign lives under commissioning fixtures, scripts, and
tests. No production line is added under `src/aros`.

Delivery is split into:

1. pinned source and data-manifest preparation;
2. R0–R3 factual apparatus and baseline calibration;
3. real Researcher adapter with durable user-space memory and restart;
4. fresh Reviewer adapter;
5. campaign driver and independent verifier;
6. one clean-wheel real campaign;
7. retained evidence and current informative result document.

Exit requires:

- all three outcome dimensions reported independently;
- the complete capability gate evaluated;
- exact clean-wheel evidence and standalone verification;
- no `src/aros` growth;
- full repository and focused commissioning tests pass;
- explicit statement that one campaign does not prove general research quality.

Only after this evidence exists should the project decide whether to productize
the Researcher adapter, alter Phase 0B sequencing, or add asynchronous
Researchers.

## 18. Research inputs

The following are campaign inputs, not current AROS product authority:

- libCacheSim: <https://github.com/1a1a11a/libCacheSim>
- cache trace collection: <https://github.com/cacheMon/cache_dataset>
- S3-FIFO background: <https://www.pdl.cmu.edu/PDL-FTP/Storage/FIFOqueues-SOSP23_abs.shtml>
- SIEVE background: <https://www.usenix.org/conference/nsdi24/presentation/zhang-yazhuo>

The user-referenced AROS v0.5 scratch document is informative only. The current
repository Design Book v1.0, registered implementation baselines, code, tests,
and exact receipts remain authoritative.
