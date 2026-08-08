# AROS

AROS 是本仓库中直接面向 Principal Research Agent 的 research OS。
The legacy `arbor` root does not mount AROS.

## Available now

The exposed command surface is:

```text
aros start [--question TEXT] [--material PATH] [--allow-checkpoint]
aros boot [--json]
aros status
aros checkpoint --message TEXT --path PATH [--path PATH]
aros task create|start|status|list|message|stop|collect|preserve|prune
aros run start|status|list|tail|stop
aros eval register|run|status|observe|audit
```

`aros start` 是唯一 AROS 入口。未初始化 workspace 时，它创建 Question-centered
Knowledge Bank；已初始化时，它从 durable Attention 启动新 Principal，而不 replay
transcript。默认模型 triple 是 `openai-responses / gpt-5.6-luna / max`。

`--allow-checkpoint` 只向本次 Principal 暴露 cooperative same-UID Git Checkpoint，
不声明 protected authority。独立 human CLI checkpoint 也明确是 cooperative。

## Agent surface

Principal 使用普通 `Read/Grep/Glob/Edit/Write`，以及至多五个 AROS tools：

- `Attention`：Question、uncertainty、hypotheses、pending measurements、unread
  returns、obligations、budget 与 blockers 的有界 restart packet。
- `Task`：durable Researcher brief/worktree/mailbox/return/collection/preserve/prune；
  不拥有 process carrier。
- `Run`：唯一 durable process carrier，拥有 log、stop、timeout、lost 与 descendant
  truth。启动的 Task 绑定一个幂等 trusted-local Run。
- `Eval`：exact candidate/apparatus measurement receipt；不产生科学 verdict。
- `Checkpoint`：`message + paths` 的 selected-path Git commit。

Task/Run/Eval 返回的终态 refs 由 host session 记录。下一次成功 Checkpoint 自动写入：

```text
AROS-Observed: tasks/TASK-.../collected.json
AROS-Observed: runs/RUN-.../final.json
AROS-Observed: eval/evaluations/EVAL-.../receipt.json
```

Task collection 绑定 TaskRun manifest/final 和 B-C-R return lineage；Principal 同时
观察 Task-owned Run final 与 collection。trailers 表示“本 session 收到过”，不表示
“已证明或已吸收”。科学意义保留在普通 Markdown；OS 不解析 relation enum、belief
level 或 strict Evidence JSON。

Visible Eval is available through `aros eval register|run|status|observe|audit`.
The apparatus produces factual measurements, the Principal interprets them, and a
lost evaluation is never retried with the same idempotency key.

## Runtime requirements

- durable Run/Task 需要 clean committed Git HEAD 与 `tmux`。
- `isolated-linux` requires a supported Linux architecture (x86_64 or aarch64),
  exactly Landlock ABI 4, `libseccomp`, and `O_PATH`; it fails closed；
  `trusted-local` 不是 security sandbox。
- Task adapters are trusted-local and application-scoped, not a security sandbox.
  network/shell flags 是 audit declarations，不是强制 containment，receipt 标记
  `capabilities_enforced=false`。
- V1 terminal truth covers the exact PGID plus descendants reparented to the live
  subreaper. Generic Run drains both sets before clean finalization. After leader
  exit, public stop is delivered only when the exact bound local carrier remains
  live. A new-session process that outlives runner death is not claimed contained
  and cannot justify a clean final receipt or prune. Delegated per-task cgroups
  belong to the shared Operations process core, not the Wave 2 security claim.
- First post-commission fix `26fe611fc88252a4667d24b0db92b742f654712e`
  closes the stop/final publication race: a delivered stop returns only after its
  matching `cancelled` final is readable and validated; delivered-false remains
  immediate.
- Second post-commission fix `14d8268ae5e82a11c872e6052027f7cf064a7337`
  repeatedly signals nested adopted descendants after escalation and treats ESRCH
  during `/proc` stat as disappearance while other observation errors fail closed.
- Third post-commission fix
  `c11eed140ca99ec6ff0d5e8d60243242411fd624` restricts repeated KILL to an
  already-delivered stop or triggered timeout. A failed direct KILL is not retried
  or hidden; receipt and final retain `delivered=false` truth.
- Current authoritative code source
  `a07f50fce557ea1b89c0e6d87836b407dce44922` records `KILL` only after a
  successful refreshed delivery and at most once in each stop/timeout signal
  sequence.
- mode-normalizing filesystem 上 integrity 仍启用，但 receipt 标记
  `filesystem_permissions_enforced=false`。该行为与 controlled Git optional-lock
  都是 factual enforcement，不是 protected authority。
- Eval 绑定 exact candidate/apparatus commits；lost evaluation 不以同一 key 自动重试。
- pre-existing staged Git changes 会阻止 Checkpoint；unselected unstaged dirt 不被修改。

## Not yet implemented

- Phase 0B program-wide `src/aros <= 12,000 LOC` gate；Phase 0A 已由 human
  approved interim gate 在 Task 8 recursive `src/aros = 17,700 LOC` 且旧 carrier
  缺席时完成；current `a07f50fc` count 是 exactly 17,699；
- 真实 external-model Researcher inner-loop commissioning 与 async portfolio；
- protected evaluation registration and admission；
- Source Gateway、Independent Reviewer、project-local Skills 和 MCP parity；
- Mission Supervisor、protected authority、shared budgets/leases；
- AROS-native semantic K/M/G；
- 完整 K/M/G productization 与 Arbor retirement。

当前 deterministic clean-wheel evidence 证明 exact provenance、tool writes 与 restart，
不证明真实 research quality 或上述未实现能力。AROS 仍有意保持 limited；没有
“strictly better than Arbor”的声明。

## Replacement boundary

CI keeps the remaining legacy implementation frozen at `src/coordinator`,
`src/executor`, `src/run.py`, `src/review.py`, and `src/cli/commands/run.py`.
Other `arbor` commands remain legacy implementations until equivalent AROS paths
are commissioned and the old paths are deleted.

CI hard-gates transitive project-import reachability from every AROS module and
direct adapter. Its conservative module-scope graph indexes every configured local
Python package. Only `src/aros/`, `src/cli/aros_app.py`, `src/cli/aros_start.py`,
and `src/cli/commands/aros_cmd.py` may grow. All non-allowlisted legacy source paths
under `src/` reject growth; `src/core/` remains legacy-frozen, so legacy source LOC
may only stay level or decrease.

These mechanical gates do not prove absence of semantic duplication. A padded copy
inside an allowlisted path still requires module commissioning review. A padded copy
outside it fails the path gate. A Git `R100` move outside `src/` is accepted only
when the destination is not configured as a Python package and no remaining entry
or import refers to the moved module.
