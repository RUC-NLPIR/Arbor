# AROS Implementation Baseline

本文件是 Agent 默认读取的当前实现基线。最高目标规范仍是
`AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`；代码、测试、Git objects 与
Task/Run/Eval receipts 决定产品实际能力。

新原生入口直接使用 `aros`；legacy `arbor` root 不再挂载 AROS。等价能力
commissioning 后直接删除旧路径，不增加转发兼容层。

## 当前架构

AROS 使用 Arbor 已验证的 Agent topology，但不在 Agent 之上实现科研状态机：

```text
Principal Research Agent
  → 维护 Question、Model、Idea、Claim 与行动选择
Researcher Task
  → 在一个方向内维护自由的研究内循环
AROS kernel
  → workspace、Git/worktree、process、Eval receipts、restart Attention
```

Question、Model、Idea、Claim 和 memory 是普通 Markdown。科研关系、scope、
uncertainty、counterevidence 与停止判断由 Principal 书写和解释，不是 OS schema。

## 当前可执行闭环

模型可见的 AROS 系统调用只有：

```text
Attention  Task  Run  Eval  Checkpoint
```

- `Attention` 从 canonical Markdown、Git history 和 Task/Run/Eval facts 派生小型
  restart packet；`unread_returns` 来自终态 receipts 与 Git trailers 的差集。
- `Task` 提供 durable child worktree/process/return；child 不能直接修改 canonical
  scientific meaning。
- `Run` 提供 durable experiment process 和 immutable final。
- `Eval` 冻结 candidate/apparatus/input/seed/environment/parser，并返回 factual
  MeasurementReceipt；Principal 解释科学意义。
- `Checkpoint(message, paths)` 使用普通 Git selected-path commit。host 自动追加
  `AROS-Observed:` trailers；它只表示 Principal session 已收到这些 returns，不表示
  supports、proves、resolves 或 belief admission。

Task/Run/Eval 的 versioned records 通过同一个内部 Git helper 提交。重复读取已提交的
终态 record 幂等复用 HEAD。Principal checkpoint 仍要求真实语义变更。

## 明确删除的机制

当前实现不存在 proposal、admission、assimilation、strict evidence relation、semantic
index、rebuild-index、receipt fence 或双路径兼容层。旧 runtime 不迁移、不读取、不双写。

执行边界是单用户、same-UID、cooperative Git。host 决定 Principal 是否获得
`Checkpoint`；prompt 或 tool input 不能把 cooperative 机制升级为 protected authority。

## 已验证

deterministic clean-wheel E2E 已验证：

```text
fresh Question-centered KB
→ Principal Attention
→ prose preregistration + Checkpoint
→ one Task + one Eval
→ scoped Question/Model/Idea/Claim/NOW prose
→ automatic observed trailers + Checkpoint
→ destroy primary Agent
→ fresh Agent Attention with unread_returns=[]
```

独立 verifier 将 Agent tool sequence 和 Write bytes 绑定到 Git blobs、Task collection、
Eval receipt、final commit trailers 和 restart packet，并确认安装包没有已删除模块。

## 当前限制与下一阶段

- Task 仍有独立 process carrier；下一阶段把它简化为 worktree + Run + Researcher
  adapter，目标 `src/aros <= 12,000 LOC`。
- 尚未 commissioning 真实 LLM Researcher 内循环与 async 多 Idea concurrency。
- Source Gateway、project Skills 和 MCP transport 尚未加入。只有重复出现且 native
  service 已证明稳定的问题才获得 Skill/MCP surface。
- protected multi-process authority、budgets/leases 和 non-bypass enforcement 未实现。
- AROS-native K/M/G 仍主要是 workspace 约定与 Principal 能力，不是独立产品层。
- legacy Arbor 的其他公共命令尚未全部被等价 AROS 能力替换，因此尚未退休。

任何下一步不得重新引入 Coordinator state machine、transition ceremony、automatic
scientific interpretation 或为了未来扩展而增加的配置/抽象层。
