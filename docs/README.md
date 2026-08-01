# 文档路由

所有 AROS 文档工作从这里开始。`document_registry.json` 是文档生命周期、权威类型和 Agent 默认可见性的唯一索引。

## 默认读取规则

- 默认上下文只包含同时标记为 `status=current` 和 `agent_visibility=default` 的条目。
- `on_demand` 文档只在任务需要时读取；计划、分析、结果和兼容性说明不能自动成为当前产品事实。
- 尚未登记的既有文档不代表已失效，只表示尚未完成本 registry 的生命周期审查。

当前行为与目标规范属于两个不同问题：

1. 当前产品行为以代码、schema、测试和精确 receipt 为准。
2. `aros-implementation-baseline` 是当前默认的迁移实施上下文。
3. `aros-design-book-v1-0-zh` 是 AROS 的最高目标规范。当前实现与它不一致时，应明确报告尚未实现的差距，不能修改解释来掩盖差距。

发生冲突时不要静默选边；核对 registry、可执行行为、测试和 receipt，并记录冲突。

## Registry schema v1

每个 `documents` 条目必须且只能包含：

- `id`：稳定且唯一的文档标识。
- `title`：显示名称。
- `path`：仓库根目录下的相对路径；不得逃逸仓库。
- `status`：`current`、`proposed` 或 `historical`。
- `authority`：`implementation_baseline`、`target_specification`、`compatibility` 或 `informative`。
- `agent_visibility`：`default` 或 `on_demand`。

修改 registry 后运行：

```bash
pytest -q tests/test_document_registry.py
```
