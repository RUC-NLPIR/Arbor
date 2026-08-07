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
- `Task`：durable Researcher worktree、process、return 与 collect。
- `Run`：durable background experiment process。
- `Eval`：exact candidate/apparatus measurement receipt；不产生科学 verdict。
- `Checkpoint`：`message + paths` 的 selected-path Git commit。

Task/Run/Eval 返回的终态 refs 由 host session 记录。下一次成功 Checkpoint 自动写入：

```text
AROS-Observed: tasks/TASK-.../collected.json
AROS-Observed: eval/evaluations/EVAL-.../receipt.json
```

trailers 表示“本 session 收到过”，不表示“已证明或已吸收”。科学意义保留在普通
Markdown；OS 不解析 relation enum、belief level 或 strict Evidence JSON。

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
  subreaper. A new-session process that outlives runner death is not claimed
  contained and cannot justify a clean final receipt or prune. Delegated per-task
  cgroups belong to the shared Operations process core, not the Wave 2 security
  claim.
- mode-normalizing filesystem 上 integrity 仍启用，但 receipt 标记
  `filesystem_permissions_enforced=false`。
- Eval 绑定 exact candidate/apparatus commits；lost evaluation 不以同一 key 自动重试。
- pre-existing staged Git changes 会阻止 Checkpoint；unselected unstaged dirt 不被修改。

## Not yet implemented

- 真实 LLM Researcher inner-loop commissioning 与 async Idea concurrency；
- Task-on-Run 简化；
- protected evaluation registration and admission；
- Source Gateway、project-local Skills 和 MCP parity；
- protected authority、shared budgets/leases；
- AROS-native semantic K/M/G；
- 完整 K/M/G productization 与 Arbor retirement。

## Replacement boundary

CI keeps the remaining legacy implementation frozen at `src/coordinator`,
`src/executor`, `src/run.py`, `src/review.py`, and `src/cli/commands/run.py`.
Other `arbor` commands remain legacy implementations until equivalent AROS paths
are commissioned and the old paths are deleted.
