# 检索与外部知识

真实研究从已有的知识出发。Arbor 能引入外部知识——文献与开放网络——分**两条互相独立的 lane**：

| Lane | 时机 | 作用 | 写入 | 默认 |
| --- | --- | --- | --- | --- |
| **接地 ideation** | IDEATE *期间* | 协调器检索以**启发新想法** | `node.grounding` | **关** |
| **新颖性审查** | 实验*之后* | SearchAgent 为已验证的想法核查**先行工作** | `node.related_work` | 关 |

两条 lane **各自独立检索、永不共享抓取的文本**——因此同一个页面不能既启发一个想法、又给它认证新颖。
两者都**默认关闭**，以保证基准运行的公平：系统无法从网上抄成品想法或结果。做真实研究时再打开——
那时"先读相关工作"正是关键。

这些都配置在 `search:` 块下——完整字段见 [配置](configuration.zh.md#search)。

---

## 接地 ideation

开启方式：

```yaml
search:
  enabled: true
  grounded_ideation: true
  backends: [alphaxiv, jina]   # 免 key：论文 + 通用网页
```

随后协调器会获得一个 **`ResearchSearch`** 工具，可在构思时调用。它是*可选*输入——想法仍可来自
实验结果或模型自身推理——只在需要外部知识时才用。用 `intent` 决定检索方式：

| intent | 用途 |
| --- | --- |
| `related_work` | 已有草稿想法——找先行工作，评估重叠 / 空白 |
| `survey` | 整理某问题/领域当前的解法（路线 + 取舍） |
| `lookup` | 查具体事实：某方法细节、数据集、benchmark 数字 |
| `explore` | 开放式扫描某方向，找未被探索的角度 |

该工具在**隔离上下文**中运行（冗长的搜索结果与页面正文不会进入协调器窗口），返回精简摘要：
summary、findings 与编号来源。从未被实际打开过的来源会被丢弃，让引用保持诚实。

当某来源确实塑造了一个想法，协调器会把它记录在该节点的 **`grounding`** 字段——在想法树与报告中可见，
与新颖性审查的 `related_work` 分开。

---

## 新颖性审查

审查为**已经验证有效的想法**调研**先行工作**，让你在合并进主干前知道：一次提升是否也是一项贡献。

```yaml
search:
  enabled: true
  auto_search_on_add: true     # 每个新想法在运行前先核查
```

开启 `auto_search_on_add: true` 后，加入树的每个想法都会先做一次实验前新颖性核查，判定结果写入节点的
`related_work` 字段（仅作建议，绝不阻断运行）。一个专门的 SearchAgent 会做自己的全新检索，因此不会被
接地 ideation 之前读过的内容误导。

在运行之外单独核查：

```bash
arbor idea-check "一句话描述你的假设"
```

选项见 [`arbor idea-check`](cli.zh.md#arbor-idea-check)。

---

## 检索后端

`search.backends` 是一个**有序列表**；所有后端的结果会合并去重，因此可以组合来源（如论文*与*通用网页，
同一篇论文若来自两个后端会合并为一条）。

| 后端 | 需要 key? | 覆盖 |
| --- | --- | --- |
| `alphaxiv` | 否 | arXiv / alphaXiv 论文（Python ≥ 3.12） |
| `jina` | 否（可选 `JINA_API_KEY` 提配额） | 通用网页（s.jina.ai） |
| `serper` | `SERPER_API_KEY` | Google 结果（serper.dev） |
| `serply` | `SERPLY_API_KEY` | Google Scholar 论文（[serply.io](https://serply.io)，GET + X-Api-Key） |
| `exa` | `EXA_API_KEY` | 神经网络检索（exa.ai REST） |
| `exa-mcp` | 否（可选 `EXA_API_KEY` 提配额） | 经 Exa 托管 MCP 服务器接入 |
| `endpoint` | 可选 | 自托管 `web_search_endpoint`（BrowseComp 风格） |

缺少 key 的后端会被**静默跳过**——因此像 `[alphaxiv, jina, serper]` 这样的列表在没有 `SERPER_API_KEY`
时会优雅退化为免 key 的两个。完全免 key 的默认是 **`backends: [alphaxiv, jina]`**——论文 + 通用网页，零配置。

key 写在配置文件里（`serper_api_key` / `serply_api_key` / `exa_api_key` / `jina_api_key`），或用对应的同名环境变量
（`SERPER_API_KEY`、`SERPLY_API_KEY`、`EXA_API_KEY`、`JINA_API_KEY`）。

!!! note "两个论文后端"
    `alphaxiv` 与 `serply` 都覆盖论文，但语料并不相同：alphaXiv 是 arXiv 预印本，
    而 Serply 走 Google Scholar，后者还收录了大量从不出现在 arXiv 上的会议与期刊论文
    （ACL Anthology、AAAI、NeurIPS 等）。同时列出两者——`backends: [alphaxiv, serply]`
    ——会把结果合并去重，从而扩大新颖性审查的先行工作覆盖面。注意 Scholar 的
    `description` 是署名行（“作者 - 会议/期刊, 年份”），并非摘要。

!!! note "Exa via MCP"
    `exa-mcp` 后端连接 Exa 托管的 MCP 服务器（`https://mcp.exa.ai/mcp`，可用 `exa_mcp_url` 覆盖 URL）。
    托管服务器**免 key** 即可基本使用——设 `exa_api_key` 只是提配额（作为 `x-api-key` header 发送）。
    它需要 MCP 客户端：`pip install 'arbor-agent[mcp]'`。而普通的 `exa` 后端走同一家的 REST API
    （那个**需要** key）——按你的环境二选一。

---

## 读取页面

读页面同样免 key。`search.visit_backend: auto`（默认）：

- alphaXiv 论文 URL → alphaXiv SDK（全文），
- 其它任意 URL → **Jina reader**（`r.jina.ai`，干净 markdown，免 key），再 fallback 到原始 `requests` 抓取。
  **PDF 也能读**——Jina reader 会抽取 PDF 文本，raw fallback 用 `pypdf` 解析 PDF。

因此打开页面无需 browse 端点或 API key。也可强制单一取页器：
`visit_backend: jina | requests | alphaxiv | endpoint`。

**阅读深度（全文 vs 摘要）。** 页面文本会按 token 预算截断。新颖性审查 lane 用
`visit_max_content_tokens`（默认 2048——判断先行工作有摘要就够）。接地 ideation lane 要读深度，
用更大的 `research_visit_tokens`（默认 6000），让论文的**方法 / 结果章节不被截断**，而不只是摘要。
需要更深可调大任一项（token 成本更高）。

---

## 向后兼容

旧配置仍原样可用。当 `backends` 为空时，Arbor 自动映射旧字段：`builtin_backend: alphaxiv` → `[alphaxiv]`，
设置了 `web_search_endpoint` → `[endpoint]`（visit 使用 `web_browse_endpoint`）。

## 当前生效了什么？

运行启动时，协调器会打印一次解析后的配置，例如：

```
search enabled — backends: alphaxiv, jina | visit: auto | grounded_ideation: on
```

让你一眼确认哪些后端生效、接地 ideation 是否打开。

## 另见

- [配置](configuration.zh.md#search) —— 完整的 `search:` 字段列表。
- [`arbor idea-check`](cli.zh.md#arbor-idea-check) —— 一次性新颖性核查。
