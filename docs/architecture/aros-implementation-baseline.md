# AROS Implementation Baseline

本文件是 AROS 渐进实施的当前默认上下文，不是“AROS 已实现”的声明。最高目标规范是仓库根目录的 `AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md`；可执行代码、schema、测试与 receipt 仍决定当前产品实际上做了什么。

## 架构冻结

AROS 只保留四个不可约部分：

1. Principal Research Agent 理解问题并选择科研行动。
2. Versioned workspace 保存可跨 session 恢复的项目心智。
3. AR OS kernel 只负责 capability、Git、process、receipt、budget、event 与 recovery。
4. Independent reality interfaces 提供不能由 Agent self-report 定义的 observation。

新 AROS 代码不得假设 semantic Coordinator 位于 Principal 之上。Question、ScientificModel 与 IdeaGraph 是 Principal 可直接编辑的 workspace view，不是 kernel-owned state。Transcript 和 provider memory 不是 canonical project memory。

## 第一版范围

- 单 Git repository、Linux/POSIX、单个 active Principal。
- 直接复用 Arbor 的 Agent loop、provider、基础文件工具及可验证的 Git/process utility。
- 新原生入口直接使用 `aros`；`arbor aros` 仅作为临时转发兼容入口（temporary forwarding compatibility route）。其他 `arbor` 命令在逐项迁移前仍是 legacy implementation。
- write-heavy child worktree 统一位于仓库的 `.worktree/`。
- 每完成一个模块都先执行该模块的真实行为测试，再进入下一模块。

## 迁移边界

| 处置 | Legacy surface |
| --- | --- |
| 保留并复用 | Agent loop、provider、配置、基础文件工具、确定性的 parser/Git/process helper |
| 新路径绕过 | CoordinatorOrchestrator、IdeaTree scientific frontier、固定 Arbor Cycle、依赖 transcript 的 resume |
| Commissioning 后删除 | semantic Coordinator、mandatory role pipeline、重复的 scheduler/state writer 与重复适配实现 |
| 保留为历史输入 | 旧 session、tree、report、raw trace；在 audit 和 Principal assimilation 前不得提升为当前 project state |

删除动作必须晚于等价能力的实测和数据迁移审计；新旧状态不得双写。

## Threat model

系统必须防御或明确限制：

- Agent/child 的误操作、越权路径访问和环境 secret 泄漏；
- worker prose 或被优化 artifact 伪造 primary metric；
- Principal、CLI、MCP 或 tmux 退出后的 stale/missing process state；
- 并发 child 污染共享 checkout，以及 cleanup 丢失 dirty work；
- 缺失 parser、hash、receipt 或 ownership 时被错误解释为成功。

`trusted-local` 只承诺审计与防误操作，不声称安全隔离。需要安全边界的 child 必须使用 fail-closed 的隔离 profile。宿主 root/kernel compromise 和分布式多租户隔离不属于第一版范围。

## 实施顺序与 exit gate

1. 文档治理：registry 可验证，Design Book 与当前实现差距可见。
2. Bootable workspace：无 transcript 的新 Agent 可恢复 mission、current thesis、active work 与主要 uncertainty。
3. Durable run：终止 Principal 不终止 experiment，recovery 不发明 final state。
4. Capability isolation：隔离不可用时 fail closed，不静默降级。
5. Deterministic evaluation：exact-commit clean rerun，worker prose 不能设置 primary metric。
6. Child task substrate：write-heavy child 使用独立 `.worktree/`，dirty work 永不被强制清理。
7. Migration/adapters：同一 kernel 服务 native CLI 与 MCP；旧数据只单向导入。
8. Commissioning：完成 fresh boot、long-run restart、independent admission、isolated child、assimilation 与 provider switch 后，才切换默认路径并退役重复模块。

每个 gate 都需要命令输出、状态文件、receipt 或测试作为证据；仅有代码路径或 mock 不能证明端到端能力。

## Commissioning 前非目标

- graph database 或 opaque memory service；
- universal BeliefEngine 或 semantic workflow/frontier scheduler；
- persistent per-role Agent service farm 或 recursive swarm；
- MCTS/PUCT kernel policy；
- automatic skill promotion；
- dashboard platform、cluster scheduler 或通用 backend framework；
- 自动科研解释、自动 assimilation 或由 hook 改写 semantic state。

这些约束用于防止旧控制论以新名称重新进入 kernel。任何偏离都必须先更新 Design Book 或显式记录冲突，不能只修改本 baseline 来降低目标。
