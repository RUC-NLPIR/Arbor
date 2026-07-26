# 配置

!!! tip "时间有限？本页可跳过"
    大多数人只配置 Arbor 一次：运行 `arbor setup`，选定 model 即可。下面的一切，都是当你想
    **换 model**、**设时间/成本预算**、**加入人工监督**，或**对准某个特定领域**时才需要。需要时
    再回来查阅。

本页是写给从没用过 Arbor 的人的。它按顺序回答三个问题：

1. **我能配置什么**，哪些设置真正重要？
2. **每一项怎么从命令行设？**
3. **当两个设置冲突时，谁说了算？**

## 选你的路径

=== "只是试试"

    运行 `arbor setup` 选一个 model，然后 `arbor` 开始。你**不**需要配置文件或任何参数。若有兴趣，可
    阅读[最重要的几项设置](#the-settings-that-matter-most)，其余忽略即可。

=== "做一项真实研究"

    先 `arbor setup`，然后浏览[你能配置什么](#what-you-can-configure)和[设一个预算](#budgets-and-timeouts)。
    一个 `--max-cycles` 参数加上合适的 model，通常就够了。

=== "反复跑同一个基准"

    把持久设置放进一个**项目配置文件**，让每次运行一模一样；或把整个领域固化进一个[插件](plugins.md)。
    见[按项目设置](#3-per-project-a-config-file)。

## 你能配置什么 { #what-you-can-configure }

设置分四个层级，从“几乎人人都会调整”到“高级”。

| 层级 | 设置 | 它控制什么 | 你为何会改它 |
| --- | --- | --- | --- |
| **必要** | `provider`、`model`、`api_key`、`base_url` | Arbor 用哪个 LLM、怎么连上 | 你必须选一个 model 并一次性提供 key。 |
| **重要** | `max_cycles` | Arbor 停下并写报告前跑多少个实验 | 控制时间与成本的主要参数。值越大，搜索越久、越深入。 |
| **重要** | `reasoning_effort` | 模型每步的思考投入程度（`low`/`medium`/`high`） | 用速度/成本换深度。 |
| **重要** | `max_turns`、`timeout:` | 单个实验的硬性安全上限 | 在长任务上控制失控的成本。 |
| **可选** | `interaction_mode` | 你对运行的引导程度（自动 vs. 批准想法） | 你想要一个人在回路。见[交互模式](interaction-modes.md)。 |
| **可选** | `webui_port` / `--no-webui` | 只读浏览器监控 | 实时看进度，或把它关掉。 |
| **高级** | `plugin`、`plugin_profile` | 把 Arbor 重定向到一个领域（评测规则、保护文件、预算包） | 你经常跑同一类基准。见[插件](plugins.md)。 |
| **高级** | skills | 调整智能体在某一步*如何*推理 | 你想要更好的构思/分析。见[技能](skills.md)。 |

### 最重要的几项设置 { #the-settings-that-matter-most }

如果你只调整三项设置，那就是这三项：

- **`model`** —— 质量与成本主要由它决定。
- **`max_cycles`** —— 研究跑多久、多深。
- **`interaction_mode`** —— 你是旁观（`auto`）还是批准每个想法（`review`）。

其余一切都有合理的默认值。

## 怎么设——从命令行

一个设置可以来自五个地方。按你实际会去用它们的顺序列出：

1. **`arbor setup`** —— 一次性向导，全局保存你的 model。*大多数人只会用到这个。*
2. **`arbor config`** —— 之后查看或编辑那个全局文件。
3. **项目配置文件** —— 随某个项目一起携带的持久设置。
4. **CLI 参数** —— 单次运行的一次性覆盖。
5. **对话里的斜杠命令** —— 为本次运行挑一个插件或技能，无需文件。

### 1. 你的 model：`arbor setup`

配置好它的最快方式。它问四个问题并写出 `~/.arbor/config.yaml`：

```console
$ arbor setup
arbor setup — let's configure your model (one time).

API type (anthropic/openai/litellm): anthropic
Base URL (local proxy / vLLM, blank for the official API):
Model: claude-sonnet-4-5
API key (blank to read from the environment): ********
✓ credentials look resolvable
Done. Saved to ~/.arbor/config.yaml
```

- **API type** 是 provider —— 见下面的 [Providers](#providers)。
- **Base URL** 对官方 Anthropic/OpenAI API 留空；只在本地代理或网关时设它。
- **API key** 可以留空，从环境变量读取（推荐）—— 例如 `ANTHROPIC_API_KEY` 或 `OPENAI_API_KEY`。

此后，直接运行 `arbor`。

### 2. 查看或编辑全局配置：`arbor config`

```bash
arbor config show           # 打印生效配置（密钥已遮蔽）
arbor config path           # 文件位置
arbor config init --provider openai --model gpt-5 --api-key dummy   # 非交互式写入
```

`arbor config init` 是该向导的可脚本化版本——一行命令即可配置好一个本地网关：

```bash
arbor config init --provider litellm --model qwen-72b \
  --base-url http://localhost:4141 --api-key dummy
```

### Providers { #providers }

选**一种** API type。你给 `arbor setup` / `--provider` 的值，恰好是以下三者之一：

| `provider` | 用于 | 备注 |
| --- | --- | --- |
| `anthropic` | Claude 系列模型 | 原生 Anthropic API。 |
| `openai` | OpenAI 系列模型 | 对推理模型使用 Responses API。 |
| `litellm` | DeepSeek、Gemini、Qwen、vLLM、Ollama、本地网关 | 任何 OpenAI 兼容端点。需设 `base_url`。 |

=== "Anthropic"

    ```yaml
    llm:
      provider: anthropic
      model: claude-sonnet-4-5
      api_key: ${ANTHROPIC_API_KEY}
    ```

=== "OpenAI"

    ```yaml
    llm:
      provider: openai
      model: gpt-5
      api_key: ${OPENAI_API_KEY}
      reasoning_effort: medium
    ```

=== "litellm（OpenAI 兼容 / 本地）"

    ```yaml
    llm:
      provider: litellm
      model: deepseek-chat
      api_key: ${OPENAI_API_KEY}   # 本地网关通常接受任意 dummy 值
      base_url: https://your-gateway/v1
    ```

!!! tip "别把 key 放进文件"
    优先用环境变量（`${ANTHROPIC_API_KEY}`），而非把密钥粘进文件。`arbor setup` 会把你的全局 key
    连同其余配置一起存在 `~/.arbor/` 下。

!!! warning "实验性：用 ChatGPT 订阅登录（`openai-oauth`）"
    ChatGPT Plus/Pro/Team 订阅用户可以用月费订阅来跑 Arbor，而不必用按量计费的 API key。
    运行 `arbor login openai` 通过浏览器登录；token 存在 `~/.arbor/oauth/openai.json` 并自动刷新。
    它会写入：

    ```yaml
    llm:
      provider: openai-oauth
      model: gpt-5
    ```

    用 `arbor login status` / `arbor login logout` 管理会话。请求发往 ChatGPT 后端
    （`chatgpt.com/backend-api/codex`），**不是** `api.openai.com`。

    用订阅 token 接第三方工具可能违反 OpenAI 条款，并有被限流或封号的风险。此路径为可选、
    不受支持——正式用途请优先使用标准的 `OPENAI_API_KEY`。

### 3. 按项目：一个配置文件 { #3-per-project-a-config-file }

当一个项目需要它自己的持久设置时，往里放一个 YAML 文件。Arbor 会自动检测目标目录里的
`research_config.yaml`、`arbor.yaml` 或 `autoresearch.yaml`（或用 `--config PATH` 传入）。这里的
设置会覆盖你的全局 setup，但优先级低于 CLI 参数。

```yaml title="research_config.yaml"
# ── 模型 ───────────────────────────────────────────────
llm:
  provider: anthropic            # anthropic | openai | litellm
  model: claude-sonnet-4-5
  api_key: ${ANTHROPIC_API_KEY}  # 环境变量会被展开
  base_url: null                 # 为 litellm / OpenAI 兼容网关设置
  reasoning_effort: medium       # low | medium | high（在支持处）
  meta_model: null               # 可选的、用于元/报告步骤的更便宜模型

# ── 编排 ──────────────────────────────────────────────
max_cycles: 12                   # Arbor 定稿并报告前的实验数
executor_max_turns: 60           # 单个实验推理轮次的硬上限

# ── 超时（秒）─────────────────────────────────────────
timeout:
  executor: 172800               # 每个实验 48 小时
  run_training_max: 604800       # 单条训练命令 7 天上限

# ── 人在回路与监控 ────────────────────────────────────
ui:
  interaction_mode: auto         # auto | direction | review | collaborative
  webui_port: 8765               # 只读浏览器监控

# ── 文献检索 / 外部知识 ────────────────────────────────
search:
  enabled: true
  backends: [alphaxiv, jina]     # 有序；多后端结果合并去重（见下）
  grounded_ideation: false       # 让 coordinator 在 ideation 阶段联网检索
  auto_search_on_add: false      # 每个新想法在运行前先做新颖性审查
```

#### 外部知识与检索 {#search}

!!! tip "检索后端与接地 ideation"
    Arbor 能在两条互相独立、默认关闭的 lane 中使用文献与开放网络——**接地 ideation**
    （ideation *期间*检索 → `node.grounding`）与**新颖性审查**（实验*之后*核查先行工作 →
    `node.related_work`）。`search.backends` 是一个有序、合并的来源列表；免 key 默认是
    `[alphaxiv, jina]`（论文 + 通用网页，零配置），`serper` / `exa` 需各自 API key、`exa-mcp`
    免 key。读取页面同样免 key（`visit_backend: auto`，经 Jina reader，支持 PDF）。完整后端表格、
    intent、key 与示例见 **[检索与外部知识](search.zh.md)** 指南。

    关键字段：`backends`、`grounded_ideation`、`auto_search_on_add`、`visit_backend`、
    `visit_max_content_tokens` / `research_visit_tokens`、
    `serper_api_key` / `exa_api_key` / `jina_api_key`、`exa_mcp_url`。旧的
    `builtin_backend` / `web_search_endpoint` 仍可用。



!!! note "扁平键也可以"
    嵌套分组（`llm:`、`timeout:`、`ui:`）是推荐风格，但等价的扁平键也被接受。带注解的参考见仓库里的
    `examples/research_config.example.yaml`。

### 4. 一次性：CLI 参数

参数覆盖其余一切，仅对单次运行有效：

```bash
arbor run --max-cycles 20 --mode review --no-webui
```

常用的有：`--max-cycles N`、`--max-turns N`、`--mode MODE`、`--webui-port N`、`--no-webui`。
完整列表见 [CLI 参考](cli.md#arbor-run)。

### 5. 在对话里：为本次运行挑一个插件或技能

你不必编辑文件来改变领域行为。当接入对话开着时，输入 `/`：

```text
/plugin load mle_kaggle mle_bench_lite   # 本次运行使用一个领域插件（+ profile）
/plugin unload                           # 本次运行忽略任何已配置的插件
/skill load idea_drafting                # 加载一个额外的推理手册
/skill unload first_principles_probe     # 本次运行丢弃一个默认技能
```

这些选择只对你即将启动的那一次运行生效，不动你的配置。见[插件](plugins.md)与[技能](skills.md)。

## 每个设置是什么意思

### 编排

| 键 | 含义 |
| --- | --- |
| `max_cycles` | Arbor 定稿并写报告前，完成/跳过/失败的想法实验的最大数量。按次用 `--max-cycles` 覆盖。 |
| `executor_max_turns` | 单个实验推理轮次的硬上限——一个失控/成本安全阀。用 `--max-turns` 覆盖。 |
| `reasoning_effort` | 模型每步的思考投入程度（`low`/`medium`/`high`，在 provider 支持处）。 |
| `meta_model` | 可选的、更便宜/更快的模型，用于元层级步骤（提炼洞见、起草报告），而 `model` 驱动主循环。 |
| `logprobs` | 可选的采样 token logprobs，用于 token 忠实的轨迹记录。默认关闭——部分 OpenAI 兼容端点（Gemini、Ollama）会以 HTTP 400 拒绝该字段而非忽略它。仅在支持的端点（OpenAI、vLLM、DeepSeek）上启用。可用 `--logprobs`/`--no-logprobs` 覆盖。 |

### 预算与超时 { #budgets-and-timeouts }

`timeout:` 分组限定各项操作可运行多久（单位秒）：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `executor` | `172800`（48 小时） | 单个实验的墙钟限制。 |
| `run_training_max` | `604800`（7 天） | 单条长时间训练命令的上限。 |

对基准而言，设定一个连贯预算最整洁的方式是一个**插件 profile**，它把 `max_cycles`、树深、
executor 超时与总时间预算归入一个名字下（例如 `mle_bench_lite`）。见[插件](plugins.md)。

### 人在回路与监控

`ui:` 分组控制监督与实时监控：

| 键 | 含义 |
| --- | --- |
| `interaction_mode` | `auto`、`direction`、`review` 或 `collaborative`。见[交互模式](interaction-modes.md)。用 `--mode` 覆盖。 |
| `webui_port` | 浏览器监控的端口（默认 `8765`）。见 [Web UI 与监控](web-ui.md)。用 `--webui-port` 覆盖；用 `--no-webui` 关闭。 |

### 领域对准

两个顶层键无需改代码即可把 Arbor 重定向到一个领域：

```yaml
plugin: mle_kaggle              # 加载一个随附的领域插件
plugin_profile: mle_bench_lite  # 在其中挑一个具名的预算/行为 profile
```

完整的插件格式与内置的 `mle_kaggle` 插件见[插件](plugins.md)。

## 当两者冲突时：优先级 { #when-settings-disagree-precedence }

配置来自好几个地方。当两者设定同一个值时，更高的那个说了算：

```text
内置默认  <  插件覆盖  <  插件 profile  <  全局 setup（~/.arbor）  <  项目配置  <  CLI 参数
```

!!! info "经验法则"
    CLI 参数胜过一切。你的项目配置胜过你的全局 setup。把持久选择设在文件里；用参数做一次性改动。

## 验证它能用

```bash
arbor config show   # 确认 provider/model/key 是你预期的（密钥已遮蔽）
arbor doctor        # 检查 PATH、Python、git，以及你的 API key 能否解析
```

`arbor doctor` 是在运行开始前抓出缺失 key 或不可达网关的最快方式。
