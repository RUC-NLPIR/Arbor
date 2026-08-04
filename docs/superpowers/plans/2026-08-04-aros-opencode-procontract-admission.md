# AROS OpenCode ProContract Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a narrow OpenCode adapter that binds an AROS Research Contract to one candidate Location, reserves exact host-fenced tool actions, emits ProContract-owned AdmissionReceipts, and exposes no scientific semantics or model-controlled authority fields.

**Architecture:** The normative ProContract obligation kernel remains unchanged. A revision-bound ResearchContractBinding, action reservation, and admission receipt live in the OpenCode adapter and append to the existing ProContract hash chain. A local Research application tool consumes host-only reservation context; actual K/Q/I parsing, TransitionAudit, and Git checkpoint remain in the pinned AROS service.

**Tech Stack:** TypeScript, Effect, Drizzle/SQLite, Bun tests, OpenCode V2 Tool Registry, SDK Next local embedding, AROS JSON subprocess boundary.

---

## Authority and constraints

- Read /workspace/opencode/AGENTS.md before execution.
- Base work on OpenCode dev, not main. Create a clean branch named aros-admission in an isolated worktree at execution time.
- Do not run tests from /workspace/opencode. Run them from packages/schema, packages/core, or packages/sdk-next.
- Use bun typecheck from each package; never invoke tsc directly.
- Use the migration generator from packages/core; do not hand-edit schema.gen.ts or migration.gen.ts.
- Do not modify packages/core/src/pro-contract/kernel.ts.
- Do not add Claim, EvidenceLink, Question, research frontier, scientific verdict, or automatic retry state to ProContract.
- Do not add a public Protocol/Server principal admission route in this plan.
- Do not regenerate Client or legacy JavaScript SDK.
- The default local authority boundary is cooperative. Only an injected external broker boundary may attest mediated.

## File boundaries

Create:

- packages/schema/src/pro-contract-admission.ts — direct-entrypoint wire codecs only.
- packages/schema/test/pro-contract-admission.test.ts
- packages/core/src/pro-contract/research.ts — immutable bindings, budget snapshot, admission transaction.
- packages/core/src/pro-contract/research-records.ts — canonical JSON bytes/hashes and ProContract receipt/fence encoding.
- packages/core/src/pro-contract/research-boundary.ts — cooperative/mediated boundary attestation seam.
- packages/core/src/pro-contract/research-attention.ts — host-registered session/Location-specific Attention source.
- packages/core/src/pro-contract/ledger.ts — shared append to the existing ProContract hash chain.
- packages/core/test/pro-contract-research.test.ts
- One generated migration under packages/core/src/database/migration/.

Modify:

- packages/core/src/pro-contract/sql.ts — research binding, action reservation, admission receipt tables.
- packages/core/src/pro-contract.ts — delegate current event/ledger append to ledger.ts without changing behavior.
- packages/core/src/pro-contract/open-code.ts — exact tool reservation with host fence and budget snapshots.
- packages/core/src/tool/registry.ts — remove existing ProContract authorization from lookup/settlement.
- packages/core/src/tool/read.ts, glob.ts, grep.ts, edit.ts, write.ts, apply-patch.ts, bash.ts, todowrite.ts, and contract-control.ts — reserve inside trusted leaf executors.
- packages/core/src/session/runner/llm.ts — advertise Research only for matching revision-bound capabilities.
- packages/core/src/session/projector.ts — reset Context Epoch on a real model switch.
- packages/core/test/pro-contract.test.ts
- packages/core/test/session-runner.test.ts
- packages/core/test/location-layer.test.ts
- packages/sdk-next/src/opencode.ts — local-only research admission facade.
- packages/sdk-next/test/embedded.test.ts
- Generated packages/core/schema.json, schema.gen.ts, and migration.gen.ts through the migration script only.

## Fixed adapter contracts

ResearchContractBinding is adapter authority state, not a ProContract Spec extension:

~~~ts
export interface ResearchContractBinding {
  readonly contractID: ProContract.ID
  readonly revision: number
  readonly specHash: string
  readonly candidateLocation: Location.Ref
  readonly workspaceID: string
  readonly canonicalRef: string
  readonly capabilities: ReadonlyArray<"checkpoint" | "task" | "run" | "eval">
  readonly evaluatorPolicyRefs: ReadonlyArray<string>
  readonly audit: {
    readonly implementationID: string
    readonly trustedExecutionClosureSHA256: string
  }
  readonly chargePolicy: {
    readonly checkpointActions: number
  }
  readonly enforcementClass: "cooperative" | "mediated"
  readonly authorityDomainID: string
}
~~~

Tool action reservation is host-only:

~~~ts
export type ActionReservation =
  | { readonly type: "uncontracted" }
  | { readonly type: "denied"; readonly reason: string }
  | {
      readonly type: "reserved"
      readonly reservationID: string
      readonly budgetBefore: ProContractAdmission.BudgetSnapshot
      readonly budgetAfter: ProContractAdmission.BudgetSnapshot
    }
~~~

~~~ts
export interface AdmissionEnvelope {
  readonly receipt: ProContractAdmission.AdmissionReceipt
  readonly canonicalBytes: Uint8Array
}

export interface FinalizeFenceEnvelope {
  readonly fence: ProContractAdmission.FinalizeFence
  readonly canonicalBytes: Uint8Array
}
~~~

The model-visible Research tool input contains only:

~~~ts
const ResearchInput = Schema.Union([
  Schema.Struct({
    action: Schema.Literal("attention"),
    maxChars: Schema.optional(
      Schema.Int.check(
        Schema.isGreaterThanOrEqualTo(512),
        Schema.isLessThanOrEqualTo(16_000),
      ),
    ),
  }),
  Schema.Struct({ action: Schema.Literal("transition_audit"), proposalRef: Schema.NonEmptyString }),
  Schema.Struct({
    action: Schema.Literal("checkpoint"),
    proposalRef: Schema.NonEmptyString,
    message: Schema.NonEmptyString,
  }),
])
~~~

No actor, contract ID, binding, revision, lease, budget, evaluator policy, canonical ref, reservation, credential, or human-direct field may enter this schema.

## Task 1: Define the AdmissionReceipt wire schema

**Files:**

- Create: packages/schema/src/pro-contract-admission.ts
- Create: packages/schema/test/pro-contract-admission.test.ts

- [ ] **Step 1: Write RED schema tests**

Add:

~~~ts
import { describe, expect, test } from "bun:test"
import { Schema } from "effect"
import { ProContractAdmission } from "../src/pro-contract-admission"

function allowReceipt() {
  const receipt = {
    schemaVersion: 1,
    decision: "allow" as const,
    candidateSubjectSHA256: "a".repeat(64),
    auditPayloadSHA256: "b".repeat(64),
    contractID: "pct_test",
    revision: 1,
    specHash: "c".repeat(64),
    workspaceID: "workspace-test",
    canonicalRef: "refs/heads/main",
    sessionID: "ses_test",
    promptID: "msg_test",
    attempt: 1,
    attemptKey: "1:",
    leaseOwner: "owner-test",
    leaseExpiresAt: 2_000,
    capability: "checkpoint" as const,
    budgetBefore: {
      turns: { limit: 10, used: 1, remaining: 9 },
      actions: { limit: 10, used: 1, remaining: 9 },
      deadline: 10_000,
    },
    charge: { actions: 1 },
    budgetRemaining: {
      turns: { limit: 10, used: 1, remaining: 9 },
      actions: { limit: 10, used: 2, remaining: 8 },
      deadline: 10_000,
    },
    evaluatorPolicyRefs: ["visible/quality@1"],
    researchContractBindingSHA256: "d".repeat(64),
    auditImplementationID: "aros-transition-audit-v1",
    trustedExecutionClosureSHA256: "e".repeat(64),
    enforcementClass: "cooperative" as const,
    authorityDomainID: "opencode/local-same-uid",
    issuedAt: 1_000,
    receiptSHA256: "",
  }
  return {
    ...receipt,
    receiptSHA256: "f".repeat(64),
  }
}

describe("ProContractAdmission", () => {
  test("round-trips an exact allow AdmissionReceipt", () => {
    const receipt = allowReceipt()
    expect(Schema.decodeUnknownSync(ProContractAdmission.AdmissionReceipt)(receipt)).toEqual(receipt)
  })

  test("rejects malformed hashes and incomplete authority fences", () => {
    const receipt = { ...allowReceipt(), candidateSubjectSHA256: "bad" }
    expect(() => Schema.decodeUnknownSync(ProContractAdmission.AdmissionReceipt)(receipt)).toThrow()
  })

})
~~~

- [ ] **Step 2: Run and verify RED**

From packages/schema:

~~~bash
bun test test/pro-contract-admission.test.ts
~~~

Expected: module not found.

- [ ] **Step 3: Implement exact schemas**

Export namespace style:

~~~ts
export * as ProContractAdmission from "./pro-contract-admission"
~~~

Define SHA256 as lowercase 64-hex, BudgetSnapshot with turns/actions/deadline, typed deny reasons, AllowReceipt, DenyReceipt, FinalizeFence, and AdmissionReceipt union. Schema contains codecs only; it does not implement hashing or byte serialization. AllowReceipt must include:

~~~text
schemaVersion
decision
candidateSubjectSHA256
auditPayloadSHA256
contractID
revision
specHash
workspaceID
canonicalRef
sessionID
promptID
attempt
attemptKey
leaseOwner
leaseExpiresAt
capability
budgetBefore
charge
budgetRemaining
evaluatorPolicyRefs
researchContractBindingSHA256
auditImplementationID
trustedExecutionClosureSHA256
enforcementClass
authorityDomainID
issuedAt
receiptSHA256
~~~

FinalizeFence binds receipt hash, reservation ID, current revision/binding/session/prompt/attempt/lease, issuedAt, expiresAt, and fence hash. Do not export through packages/schema/src/index.ts; the package wildcard direct entrypoint is sufficient.

- [ ] **Step 4: Verify GREEN and typecheck**

~~~bash
bun test test/pro-contract-admission.test.ts
bun typecheck
~~~

Expected: both exit 0.

- [ ] **Step 5: Commit in the OpenCode worktree**

~~~bash
git add packages/schema/src/pro-contract-admission.ts packages/schema/test/pro-contract-admission.test.ts
git commit -m "feat(schema): define research admission receipts"
~~~

## Task 2: Add adapter tables and shared ledger append

**Files:**

- Create: packages/core/src/pro-contract/ledger.ts
- Create: packages/core/src/pro-contract/research-records.ts
- Modify: packages/core/src/pro-contract/sql.ts
- Modify: packages/core/src/pro-contract.ts
- Modify: packages/core/test/pro-contract.test.ts
- Generated: packages/core/schema.json
- Generated: packages/core/src/database/schema.gen.ts
- Generated: packages/core/src/database/migration.gen.ts
- Generated: one packages/core/src/database/migration/<generated>_pro_contract_research_admission.ts

- [ ] **Step 1: Write RED storage/ledger tests**

In packages/core/test/pro-contract.test.ts add:

~~~ts
it.effect("appends adapter admission events to the existing ProContract hash chain", () =>
  Effect.gen(function* () {
    const { db } = yield* Database.Service
    const appended = yield* db.transaction((tx) =>
      Effect.gen(function* () {
        const before = yield* ProContractLedger.frontier(tx)
        const result = yield* ProContractLedger.append(tx, {
          type: "research-admission",
          contractID,
          receiptHash: "a".repeat(64),
          subjectHash: "b".repeat(64),
          decision: "deny",
        })
        expect(result.previousHash).toBe(before.hash)
        return { before, result }
      }),
    )
    expect(appended.result.frontier).toBe(appended.before.frontier + 1)
  }),
)
~~~

Also test that existing issue/activate/report/discharge receipts keep the same frontier/hash behavior after extraction.

In packages/core/test/pro-contract-research.test.ts add the cross-language golden vector:

~~~ts
expect(hex(canonicalJsonBytes({ z: 1, a: "é", nested: { b: 2, a: [true, null] } }))).toBe(
  "7b2261223a22c3a9222c226e6573746564223a7b2261223a5b747275652c6e756c6c5d2c2262223a327d2c227a223a317d",
)
expect(Hash.sha256(canonicalJsonBytes({ z: 1, a: "é", nested: { b: 2, a: [true, null] } }))).toBe(
  "3d4ef4cab1709da1a1628556cd21d27c5c1c6478d92a03fda97ee98f1236cf44",
)
~~~

Also mutate string, number, nested object, and nested list fields with same-typed values and prove every receipt hash changes. Reject undefined, non-finite numbers, BigInt, and non-JSON values.

- [ ] **Step 2: Run and verify RED**

From packages/core:

~~~bash
bun test test/pro-contract.test.ts
~~~

- [ ] **Step 3: Add Drizzle tables**

First implement research-records.ts canonical JSON recursively: sort every object key by Unicode code point, preserve array order, encode compact UTF-8 JSON with no ASCII escaping, and reject values outside null/boolean/finite-number/string/array/string-keyed-object. receiptBytes validates the codec, omits receiptSHA256 for hashing, then returns the exact final encoded bytes. No JSON.stringify insertion-order hash is accepted.

Add:

~~~ts
export const ProContractResearchBindingTable = sqliteTable(
  "pro_contract_research_binding",
  {
    contract_id: text().notNull(),
    revision: integer().notNull(),
    binding_hash: text().notNull(),
    data: text({ mode: "json" }).$type<StoredResearchContractBinding>().notNull(),
  },
  (table) => [primaryKey({ columns: [table.contract_id, table.revision] })],
)

export const ProContractActionReservationTable = sqliteTable(
  "pro_contract_action_reservation",
  {
    id: text().primaryKey(),
    session_id: text().notNull(),
    assistant_message_id: text().notNull(),
    tool_call_id: text().notNull(),
    data: text({ mode: "json" }).$type<StoredActionReservation>().notNull(),
  },
  (table) => [
    uniqueIndex("pro_contract_action_identity_idx").on(
      table.session_id,
      table.assistant_message_id,
      table.tool_call_id,
    ),
  ],
)

export const ProContractAdmissionReceiptTable = sqliteTable("pro_contract_admission_receipt", {
  receipt_hash: text().primaryKey(),
  reservation_id: text().notNull().unique(),
  contract_id: text().notNull(),
  subject_hash: text().notNull(),
  data: text({ mode: "json" }).$type<ProContractAdmission.AdmissionReceipt>().notNull(),
})
~~~

Use the repository's existing JSON column helper/type pattern; do not introduce a new ORM wrapper.

- [ ] **Step 4: Generate the migration**

From packages/core:

~~~bash
bun script/migration.ts --name pro_contract_research_admission
bun script/migration.ts --check
~~~

Expected: one migration plus refreshed schema.json, schema.gen.ts, migration.gen.ts; check exits 0. Do not hand-edit generated files.

- [ ] **Step 5: Extract one ledger service**

Move only current append/frontier/hash-chain mechanics into pro-contract/ledger.ts. ProContract.Service and research admission call the same append implementation. Do not add research admission to ProContractKernel.Command.

Define LedgerCommand as ProContractKernel.Command | ResearchAdmissionLedgerEvent and update only the SQL/event-history projection type. ResearchAdmissionLedgerEvent contains type="research-admission", contract/revision, receipt hash, subject hash, and decision; the normative kernel never receives it. ledger.append accepts the caller's existing Drizzle transaction so Contract state+event and admission receipt+event each remain one immediate transaction rather than nesting a second transaction.

- [ ] **Step 6: Verify GREEN and commit**

~~~bash
bun test test/pro-contract.test.ts
bun typecheck
git diff --check
git add packages/core/src/pro-contract/ledger.ts packages/core/src/pro-contract/research-records.ts \
  packages/core/src/pro-contract/sql.ts \
  packages/core/src/pro-contract.ts packages/core/test/pro-contract.test.ts \
  packages/core/schema.json packages/core/src/database/schema.gen.ts \
  packages/core/src/database/migration.gen.ts packages/core/src/database/migration
git commit -m "feat(core): persist research admission authority"
~~~

## Task 3: Bind one immutable Research Contract revision

**Files:**

- Create: packages/core/src/pro-contract/research.ts
- Create: packages/core/src/pro-contract/research-boundary.ts
- Create: packages/core/test/pro-contract-research.test.ts
- Modify: packages/core/src/pro-contract/open-code.ts

- [ ] **Step 1: Write RED binding tests**

Add:

- stores one immutable ResearchContractBinding per revision
- returns the existing binding for exact idempotent bytes
- rejects different bytes for the same contract/revision
- does not copy a binding to a new revision
- preserves exact approved budget without proposal widening
- binds candidate Location, canonical ref, evaluator refs, and trusted audit closure
- refuses mediated from the default cooperative boundary

Representative test:

~~~ts
it.effect("rejects a conflicting binding for the same revision", () =>
  Effect.gen(function* () {
    const research = yield* ProContractResearch.Service
    const first = binding({ canonicalRef: "refs/heads/main" })
    yield* research.bind(first)
    const error = yield* research.bind({ ...first, canonicalRef: "refs/heads/other" }).pipe(Effect.flip)
    expect(error).toBeInstanceOf(ProContractResearch.BindingConflictError)
  }),
)
~~~

- [ ] **Step 2: Run and verify RED**

~~~bash
bun test test/pro-contract-research.test.ts
~~~

- [ ] **Step 3: Implement exact binding and boundary interfaces**

bind computes canonical JSON SHA-256, verifies the current contract revision/spec hash and exact Location, calls ResearchBoundary.attest, then inserts with conflict-do-nothing and re-reads. Any mismatch is BindingConflictError.

The service also exposes attentionContext(sessionID, now), a read-only projection of current contract/binding/lease/capabilities, exact remaining budget, and institutional obligation status. It never reserves or charges an action and returns explicit unavailable/stale states rather than granting authority.

Default ResearchBoundary returns:

~~~ts
{
  enforcementClass: "cooperative",
  authorityDomainID: "opencode/local-same-uid",
  candidateLocationSHA256: sha256(canonicalJsonBytes(location)),
}
~~~

It must never infer mediated from a directory label, worktree strategy, prompt, or caller input.

- [ ] **Step 4: Keep general Contract formation unchanged**

Do not route AROS binding through contract_propose, which widens budgets. bind accepts only an already issued exact contract/revision from a host principal path. This plan adds no model tool or public HTTP route for binding.

- [ ] **Step 5: Verify GREEN and commit**

~~~bash
bun test test/pro-contract-research.test.ts test/pro-contract.test.ts
bun typecheck
git diff --check
git add packages/core/src/pro-contract/research.ts \
  packages/core/src/pro-contract/research-boundary.ts \
  packages/core/src/pro-contract/open-code.ts \
  packages/core/test/pro-contract-research.test.ts
git commit -m "feat(core): bind research contract revisions"
~~~

## Task 4: Reserve actions inside trusted leaf executors

**Files:**

- Modify: packages/core/src/pro-contract/open-code.ts
- Modify: packages/core/src/tool/registry.ts
- Modify: packages/core/src/tool/read.ts
- Modify: packages/core/src/tool/glob.ts
- Modify: packages/core/src/tool/grep.ts
- Modify: packages/core/src/tool/edit.ts
- Modify: packages/core/src/tool/write.ts
- Modify: packages/core/src/tool/apply-patch.ts
- Modify: packages/core/src/tool/bash.ts
- Modify: packages/core/src/tool/todowrite.ts
- Modify: packages/core/src/tool/contract-control.ts
- Modify: packages/core/test/pro-contract-research.test.ts
- Modify: packages/core/test/session-runner-tool-registry.test.ts
- Modify: packages/core/test/application-tools.test.ts
- Modify: packages/core/test/tool-write.test.ts
- Modify: packages/core/test/tool-apply-patch.test.ts

- [ ] **Step 1: Write RED reservation tests**

Add:

- reserves one action under current session/prompt/attempt/lease
- includes tool name, assistant message, and tool call in reservation identity
- exact retry returns the same reservation without another charge
- different tool identity consumes a separate action
- stale session/prompt/attempt/lease/location denies
- returns exact before/after budget snapshots
- uncontracted sessions keep existing behavior
- Research attention/transition_audit decode and run without action reservation
- Research checkpoint reserves exactly once after input decode
- exhausted action budget still permits read-only Research actions
- attentionContext reports exact authority/budget/obligations without charging or requiring checkpoint capability
- edit/write/apply_patch each reserve exactly once and stale/exhausted denial occurs before mutation

- [ ] **Step 2: Run and verify RED**

~~~bash
bun test test/pro-contract-research.test.ts \
  test/session-runner-tool-registry.test.ts test/application-tools.test.ts \
  test/tool-write.test.ts test/tool-apply-patch.test.ts
~~~

- [ ] **Step 3: Change reserveAction to structured input/output**

Use:

~~~ts
readonly reserveAction: (input: {
  readonly sessionID: SessionSchema.ID
  readonly assistantMessageID: SessionMessage.ID
  readonly toolCallID: string
  readonly toolName: string
  readonly now: number
}) => Effect.Effect<ActionReservation>
~~~

The immediate transaction validates contract active revision, mutable binding revision/session/prompt/attempt/lease/location, exact immutable Research binding when present, deadline, and action budget. It inserts a deterministic reservation ID derived from full tool identity. Conflict re-read must match exact bytes.

- [ ] **Step 4: Move authorization out of Tool Registry**

Remove ProContractOpenCode from ToolRegistry dependencies and delete the pre-settlement reserveAction call. Registry remains canonical lookup/settlement/output bounding only, as required by packages/core/src/tool/AGENTS.md.

Add one helper on ProContractOpenCode:

~~~ts
readonly reserveToolAction: (
  context: Tool.Context,
  toolName: string,
  now: number,
) => Effect.Effect<{ readonly reservationID?: string }, ToolFailure>
~~~

Each currently Contract-advertised built-in captures ProContractOpenCode.Service in its Location layer and calls reserveToolAction after input decode and immediately before its first effect. Contract control tools already capture the service. AROS application tools call the SDK local facade with their trusted Tool.Context: Research calls it only for checkpoint; Task/Run/Eval call it for mutating actions. Attention and transition_audit do not call it. Do not change Tool.Context, public Tool.make, or add registry authorization metadata.

- [ ] **Step 5: Verify GREEN and commit**

~~~bash
bun test test/pro-contract-research.test.ts \
  test/session-runner-tool-registry.test.ts test/application-tools.test.ts \
  test/tool-write.test.ts test/tool-apply-patch.test.ts
bun typecheck
git diff --check
git add packages/core/src/pro-contract/open-code.ts packages/core/src/tool/registry.ts \
  packages/core/src/tool/read.ts packages/core/src/tool/glob.ts \
  packages/core/src/tool/grep.ts packages/core/src/tool/edit.ts \
  packages/core/src/tool/write.ts packages/core/src/tool/apply-patch.ts \
  packages/core/src/tool/bash.ts packages/core/src/tool/todowrite.ts \
  packages/core/src/tool/contract-control.ts \
  packages/core/test/pro-contract-research.test.ts \
  packages/core/test/session-runner-tool-registry.test.ts packages/core/test/application-tools.test.ts \
  packages/core/test/tool-write.test.ts packages/core/test/tool-apply-patch.test.ts
git commit -m "feat(core): reserve contract actions in leaves"
~~~

## Task 5: Admit exact transition subjects

**Files:**

- Modify: packages/core/src/pro-contract/research.ts
- Modify: packages/core/test/pro-contract-research.test.ts

- [ ] **Step 1: Write RED allow/deny tests**

Add:

- admits one mechanically valid exact subject
- records allow and deny in existing hash chain
- denies mechanically invalid audit
- denies stale revision/session/prompt/attempt/lease/location/ref/binding
- denies exhausted budget and disallowed evaluator policy
- denies audit implementation or trusted closure mismatch
- returns exact before/charge/remaining snapshots
- exact reservation/subject retry returns the same receipt
- a changed subject cannot reuse a reservation
- revalidation rejects lease expiry revision binding session prompt or attempt drift without charging

- [ ] **Step 2: Run and verify RED**

~~~bash
bun test test/pro-contract-research.test.ts
~~~

- [ ] **Step 3: Implement admitTransition**

Use:

~~~ts
readonly admitTransition: (input: {
  readonly reservationID: string
  readonly candidateSubjectSHA256: string
  readonly auditPayloadSHA256: string
  readonly mechanicallyValid: boolean
  readonly evaluatorPolicyRefs: ReadonlyArray<string>
  readonly auditImplementationID: string
  readonly trustedExecutionClosureSHA256: string
  readonly now: number
}) => Effect.Effect<AdmissionEnvelope>
~~~

One immediate transaction loads reservation, current Contract, mutable OpenCode binding, immutable Research binding, and any prior receipt. Revalidate every fence and policy. The already persisted reservation authorizes its charged action even when budgetRemaining.actions is zero; do not charge or reject it a second time. Build and persist allow or typed deny receipt, append its exact hash/projection to the shared ProContract ledger, and return the exact canonical bytes. Git CAS is deliberately outside this transaction.

Add revalidateAdmission(receiptHash, reservationID, now) -> FinalizeFenceEnvelope. It performs no budget charge, rechecks current contract revision, immutable binding hash, session/prompt/attempt ordinal and key, lease owner/expiry, Location/ref, and stored receipt, then emits a short-lived fence. Stale state returns a typed deny rather than a fence.

- [ ] **Step 4: Test unused allow semantics**

Add a test showing an allow receipt remains an attributable charged authorization when no Git callback occurs. It does not discharge the Contract, change scientific state, or automatically retry.

- [ ] **Step 5: Verify GREEN and commit**

~~~bash
bun test test/pro-contract-research.test.ts test/pro-contract.test.ts
bun typecheck
git diff --check
git add packages/core/src/pro-contract/research.ts \
  packages/core/test/pro-contract-research.test.ts
git commit -m "feat(core): admit audited research transitions"
~~~

## Task 6: Expose the local-only SDK Next facade

**Files:**

- Modify: packages/sdk-next/src/opencode.ts
- Modify: packages/sdk-next/test/embedded.test.ts
- Create: packages/core/src/pro-contract/research-attention.ts
- Modify: packages/core/src/session/runner/llm.ts
- Modify: packages/core/src/session/projector.ts
- Modify: packages/core/test/session-runner.test.ts
- Modify: packages/core/test/session-projector.test.ts
- Modify: packages/core/test/location-layer.test.ts

- [ ] **Step 1: Write RED facade and permission tests**

Add:

- SDK Next exposes research.bind, budgetSnapshot, and admitTransition
- SDK Next create accepts an optional host-only ResearchBoundary implementation and defaults to cooperative
- SDK Next exposes a session/Location-specific AROS Attention provider registration seam
- matching active Research binding advertises only the registered application tools named by checkpoint/task/run/eval capabilities
- no matching revision binding hides the tool
- Research tool receives host-only reservation
- system prompt contains no binding hash, authority token, reservation, lease owner, or canonical path
- default local boundary refuses mediated
- initial start model switch and compaction replacement each load the exact current packet

- [ ] **Step 2: Run and verify RED**

From packages/core:

~~~bash
bun test test/session-runner.test.ts test/session-projector.test.ts test/location-layer.test.ts
~~~

From packages/sdk-next:

~~~bash
bun test test/embedded.test.ts
~~~

- [ ] **Step 3: Add the local facade**

Extend local creation without adding an HTTP surface:

~~~ts
export interface CreateOptions {
  readonly researchBoundary?: ProContractResearchBoundary.Interface
}
~~~

Change create to accept options: CreateOptions = {}. Add ProContractResearch.node,
ProContractResearchAttention.node, and ProContractOpenCode.node to the same
AppNodeBuilder graph built with the existing memoMap; provide the supplied
ResearchBoundary service or the default cooperative layer before build. After
Layer.buildWithMemoMap, capture concrete instances with Context.get from that
exact built Context:

~~~ts
const research = Context.get(context, ProContractResearch.Service)
const researchAttention = Context.get(context, ProContractResearchAttention.Service)
const contractBindings = Context.get(context, ProContractOpenCode.Service)
~~~

Return closures over those captured instances; do not call Service.use outside
the built graph:

~~~ts
research: {
  bind: research.bind,
  budgetSnapshot: research.budgetSnapshot,
  admitTransition: research.admitTransition,
  revalidateAdmission: research.revalidateAdmission,
  attentionContext: research.attentionContext,
  reserveToolAction: (context, toolName, now) =>
    contractBindings.reserveToolAction(context, toolName, now),
  registerAttention: researchAttention.register,
},
~~~

Use the file's existing runtime/layer pattern rather than constructing a second service graph.

- [ ] **Step 4: Gate the Research tool**

Session runner advertises the host-registered Research tool for a matching Research binding so its read actions remain available; checkpoint execution still requires checkpoint capability through decoded-operation metering. It maps task/run/eval capabilities only to those host-registered tools for the current Location. Do not add coarse process.execute; the mediated commissioning contract has no Bash.

research-attention.ts stores one process-global host callback whose load input contains sessionID and exact Location; it returns one SystemContext value or unavailable. Session runner requests it for the current session and combines it with the existing Location registry without treating the registry as global. The host callback invokes the same pinned AROS packet builder as the Research attention tool.

On SessionEvent.ModelSwitched, projector resets SessionContextEpoch after updating the model. The next turn loads a complete new bounded packet as baseline; do not emit only a pointer. Existing compaction replacement continues to use the same source. Add real provider-request tests for initial start, model switch, and compaction replacement.

- [ ] **Step 5: Verify GREEN and commit**

~~~bash
cd /workspace/opencode/.worktree/aros-admission/packages/core
bun test test/pro-contract-research.test.ts test/pro-contract.test.ts \
  test/session-runner.test.ts test/session-projector.test.ts test/location-layer.test.ts
bun typecheck

cd /workspace/opencode/.worktree/aros-admission/packages/sdk-next
bun test test/embedded.test.ts
bun typecheck
~~~

Expected: all exit 0.

~~~bash
git add packages/core/src/pro-contract/research-attention.ts \
  packages/core/src/session/runner/llm.ts packages/core/src/session/projector.ts \
  packages/core/test/session-runner.test.ts packages/core/test/session-projector.test.ts \
  packages/core/test/location-layer.test.ts \
  packages/sdk-next/src/opencode.ts packages/sdk-next/test/embedded.test.ts
git commit -m "feat(opencode): expose research admission facade"
~~~

## Task 7: Package regression and boundary evidence

**Files:**

- Create in Arbor after OpenCode commit is fixed: docs/analysis/aros-opencode-admission-smoke.md

- [ ] **Step 1: Run schema package verification**

~~~bash
cd /workspace/opencode/.worktree/aros-admission/packages/schema
bun test test/pro-contract-admission.test.ts
bun typecheck
~~~

- [ ] **Step 2: Run core package verification**

~~~bash
cd /workspace/opencode/.worktree/aros-admission/packages/core
bun script/migration.ts --check
bun test test/pro-contract-research.test.ts test/pro-contract.test.ts \
  test/session-runner-tool-registry.test.ts test/application-tools.test.ts \
  test/tool-write.test.ts test/tool-apply-patch.test.ts \
  test/session-runner.test.ts test/session-projector.test.ts test/location-layer.test.ts
bun typecheck
~~~

- [ ] **Step 3: Run SDK Next verification**

~~~bash
cd /workspace/opencode/.worktree/aros-admission/packages/sdk-next
bun test test/embedded.test.ts
bun typecheck
~~~

- [ ] **Step 4: Verify no forbidden surface appeared**

~~~bash
git diff dev...HEAD -- packages/core/src/pro-contract/kernel.ts \
  packages/protocol packages/client packages/sdk/js
~~~

Expected: empty diff. Confirm no generated Client/SDK changes and no public admission route.

- [ ] **Step 5: Record narrow evidence**

Record exact OpenCode base/commit, package commands, test counts, migration check, receipt fixture hash, and the explicit statement that default local ResearchBoundary is cooperative. This is adapter commissioning, not mediated non-bypass or full AROS commissioning.

- [ ] **Step 6: Commit the Arbor evidence separately**

~~~bash
cd /workspace/Arbor/.worktree/aros-principal-loop
git add docs/analysis/aros-opencode-admission-smoke.md
git commit -m "docs(aros): record opencode admission evidence"
~~~

## OpenCode plan completion gate

This plan is complete only when:

- Research binding is immutable per contract revision;
- exact action retry never double-charges;
- AdmissionReceipt is ProContract-owned and hash-chain recorded;
- every host fence/policy mismatch denies;
- the model cannot provide or observe authority fields;
- only tools named by the exact Research binding capabilities are advertised;
- kernel.ts and public HTTP/SDK surfaces remain unchanged;
- local boundary remains explicitly cooperative;
- all package tests/typechecks/migration checks pass.

Do not mark mediated authority or the overall AROS goal complete. Proceed to the separate cross-repository commissioning plan.
