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
- `Task` 提供 durable child brief/worktree/mailbox/return/collection/preserve/prune；
  child 不能直接修改 canonical scientific meaning。Task 不再拥有 process carrier。
- `Run` 是唯一 process carrier，提供 durable process、log、public stop、timeout、lost、
  descendant truth 和 immutable final。每个启动的 Task 绑定一个幂等的 trusted-local
  Run；Task collection 同时绑定 TaskRun manifest/final 与 B-C-R return lineage。
- `Eval` 冻结 candidate/apparatus/input/seed/environment/parser，并返回 factual
  MeasurementReceipt；Principal 解释科学意义。
- `Checkpoint(message, paths)` 使用普通 Git selected-path commit。host 自动追加
  `AROS-Observed:` trailers；它只表示 Principal session 已收到这些 returns，不表示
  supports、proves、resolves 或 belief admission。

Task/Run/Eval 的 versioned records 通过同一个内部 Git helper 提交。重复读取已提交的
终态 record 幂等复用 HEAD。Principal 同时观察 Task-owned Run final 和 Task
collection；checkpoint 仍要求真实语义变更。

Generic Run 在 terminal reconciliation 中排空同 PGID 进程与被 live subreaper
收养的 descendants。leader 退出后，public stop 只有在 exact bound local carrier 仍存活
时才会投递；发现未被 containment 覆盖的 descendants 时不能签发 clean final。

## 明确删除的机制

当前实现不存在 proposal、admission、assimilation、strict evidence relation、semantic
index、rebuild-index、receipt fence 或双路径兼容层。旧 runtime 不迁移、不读取、不双写。

执行边界是单用户、same-UID、cooperative Git。host 决定 Principal 是否获得
`Checkpoint`；prompt 或 tool input 不能把 cooperative 机制升级为 protected authority。
mode-normalizing filesystem integrity 与 controlled Git optional-lock 行为是事实记录，
不是 protected authority。

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

在 product source `4f1eb3df2578f2ba45c8d542ff8fe0e07a37fe10` 上，独立
`-I -S` verifier 将 Agent tool sequence 和 Write bytes 绑定到 Git blobs、Task-owned
Run manifest/final、Task collection、Eval receipt、final commit trailers、restart
packet，以及 source→wheel→installed distribution/RECORD/entrypoint provenance。clean
wheel 包含 `task_adapter.py` 与 `task_run.py`，不包含 `task_runner.py` 或 installed
bytecode。最终 research commit 是
`7ca93fc634d08e4598016f7b4e9ad2fd276e4c57`。

该 retained commissioning evidence 仍严格绑定上述 `4f1eb3d` source。第一项
post-commission fix `26fe611fc88252a4667d24b0db92b742f654712e` 修复 public
stop/final publication race：delivered stop 只有在 matching `cancelled` final 可验证
后才返回，delivered-false receipt 仍立即返回。第二项 post-commission fix
`14d8268ae5e82a11c872e6052027f7cf064a7337` 继续排空 nested adopted descendants，
在 escalation 后重复 KILL，并把 `/proc` stat 的 ESRCH disappearance 视为进程已消失，
其他 observation errors 仍 fail closed。current authoritative code source
`c11eed140ca99ec6ff0d5e8d60243242411fd624` 最终限制 repeated KILL 只发生于已经
delivered 的 stop 或已经触发的 timeout；direct KILL delivery false 不会被 retry 或
伪装，receipt/final 保留 `delivered=false` truth。其 exact clean-wheel full gate 收集
1,970 tests：1,964 passed、6 skipped、0 failed，exit 0；direct-KILL-false、nested stop、
nested timeout 与 ESRCH 四项 regression set 连续 20/20 passed。

## 当前限制与下一阶段

- human-approved Phase 0A interim gate 已在 Task 8 recursive
  `src/aros = 17,700 LOC` 且旧 carrier 缺席时完成；current `c11eed14` count 仍 exactly
  17,700。原 program-wide Phase 0B `src/aros <= 12,000 LOC` gate 仍必须在
  Phase A Mission Supervisor 之前完成。当前功能有意保持受限。
- deterministic simple-loop commissioning 不证明真实 external-model research quality、
  真实 Researcher inner loop 或 async portfolio。
- Source Gateway、Independent Reviewer、budgets/Supervisor、project Skills 和 MCP
  transport 尚未实现。
- protected multi-process authority 与 non-bypass enforcement 未实现；filesystem/Git
  的 factual enforcement 不能替代它。
- AROS-native K/M/G 仍主要是 workspace 约定与 Principal 能力，不是独立产品层。
- legacy Arbor 的其他公共命令尚未全部被等价 AROS 能力替换，因此尚未退休。

AROS 仍是 limited implementation；当前 evidence 不支持“strictly better than Arbor”
的声明。

任何下一步不得重新引入 Coordinator state machine、transition ceremony、automatic
scientific interpretation 或为了未来扩展而增加的配置/抽象层。
