# Arbor Research Agent Skill Suite

This directory contains a Codex/Claude Code skill suite that reconstructs the
open-source Arbor/AutoResearch behavior from `arbor` as a set of
Agent Skills.

Most users should invoke only the public entrypoint:

```text
$arbor-research-agent <your research or optimization request>
```

In Claude Code, the equivalent direct invocation is usually:

```text
/arbor-research-agent <your research or optimization request>
```

The internal phase skills are still required. Install all `arbor-*` skill
directories together; do not install only `arbor-research-agent`.

## Quick Download And Installation

Set `REPO_URL` to the Arbor GitHub repository and `REPO_REF` to the branch or
tag that contains this `skills/` directory.

```bash
REPO_URL="https://github.com/RUC-NLPIR/Arbor.git"
REPO_REF="main"
TMP_DIR="$(mktemp -d)"
git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$TMP_DIR/arbor-skill-suite"
SKILLS_SRC="$TMP_DIR/arbor-skill-suite/skills"
```

If you are installing from the current local checkout instead of GitHub, use:

```bash
SKILLS_SRC="<path-to-Arbor>/skills"
```

### WSL-native local adapter

On Windows, the recommended local-clone setup is to clone Arbor inside WSL,
for example under `~/agent-workspace/Arbor`, not under `/mnt/c`. From that WSL
checkout, `arbor local` can run this suite through the WSL `claude` or `codex`
CLI without configuring Arbor's native provider runtime:

```bash
cd ~/agent-workspace/Arbor
arbor local doctor
arbor local install --agent both
arbor local run --agent claude --cwd ~/agent-workspace/<target_repo> \
  "try a one-cycle smoke run; do not train, install packages, or use B_test"
```

`arbor local` rejects mounted Windows paths under `/mnt/c` so the Arbor clone,
target repository, agent CLI, and generated `.arbor/` artifacts all stay in the
WSL filesystem.

### Install into Codex

```bash
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$CODEX_SKILLS_DIR"
cp -R "$SKILLS_SRC"/arbor-* "$CODEX_SKILLS_DIR"/
find "$CODEX_SKILLS_DIR" -maxdepth 1 -type d -name 'arbor-*' | sort
```

Restart Codex after installation. Then invoke:

```text
$arbor-research-agent <your task>
```

### Install into Claude Code

User-level installation:

```bash
mkdir -p ~/.claude/skills
cp -R "$SKILLS_SRC"/arbor-* ~/.claude/skills/
find ~/.claude/skills -maxdepth 1 -type d -name 'arbor-*' | sort
```

Project-level installation:

```bash
mkdir -p <target_repo>/.claude/skills
cp -R "$SKILLS_SRC"/arbor-* <target_repo>/.claude/skills/
find <target_repo>/.claude/skills -maxdepth 1 -type d -name 'arbor-*' | sort
```

Restart Claude Code after installation. Then invoke:

```text
/arbor-research-agent <your task>
```

### Let Codex Install It

Paste this prompt into Codex:

```text
Install the Arbor Research Agent skill suite from
https://github.com/RUC-NLPIR/Arbor.git, branch main. Clone the repo
into a temporary directory, locate skills/arbor-research-agent/SKILL.md, and
copy every skills/arbor-* directory into ${CODEX_HOME:-$HOME/.codex}/skills. Do
not copy only the wrapper skill. Do not modify the target project source. After
copying, verify that 11 arbor-* skill directories exist and that each contains
SKILL.md. Then tell me to restart Codex and show the exact path you installed
to.
```

For local installation from this checkout, use:

```text
Install the Arbor Research Agent skill suite from <path-to-Arbor>/skills.
Copy every arbor-* directory into ${CODEX_HOME:-$HOME/.codex}/skills. Do not
copy only the wrapper skill. Verify that 11 arbor-* skill directories exist and
that each contains SKILL.md. Then tell me to restart Codex and show the exact
path you installed to.
```

### Let Claude Code Install It

Paste this prompt into Claude Code:

```text
Install the Arbor Research Agent skill suite from
https://github.com/RUC-NLPIR/Arbor.git, branch main. Clone the repo
into a temporary directory, locate skills/arbor-research-agent/SKILL.md, and
copy every skills/arbor-* directory into ~/.claude/skills. Do not copy only the
wrapper skill. Do not modify the target project source. After copying, verify
that 11 arbor-* skill directories exist and that each contains SKILL.md. Then
tell me to restart Claude Code and show the exact path you installed to.
```

For project-level installation, use this prompt instead:

```text
Install the Arbor Research Agent skill suite from
https://github.com/RUC-NLPIR/Arbor.git, branch main, into this
project's .claude/skills directory. Clone the repo into a temporary directory,
locate skills/arbor-research-agent/SKILL.md, and copy every skills/arbor-*
directory into .claude/skills. Do not copy only the wrapper skill. Do not modify
source files outside .claude/skills. Verify that 11 arbor-* skill directories
exist and that each contains SKILL.md. Then tell me to restart Claude Code.
```

## Status

The suite is usable and aligns with Arbor's core behavior at the level that
Agent Skills can express and execute.

It covers:

- A public intake entrypoint similar to `arbor run`.
- An Arbor-style clarification checkpoint for missing target, metric, data,
  evaluation, permissions, budget, and run mode.
- Fast-path execution when the user already provides enough constraints or
  explicitly says to use safe defaults.
- A research contract passed from the public wrapper into the orchestrator.
- A phase-loading orchestrator rather than one monolithic skill.
- Durable `.arbor/sessions/<run_name>/` session state.
- An Idea Tree as persistent memory across context changes.
- B_dev/B_test discipline: B_dev for iteration, B_test only for merge/final
  verification.
- Coordinator discipline: the coordinator does not directly edit benchmark or
  project source code; implementation work goes through executor/worktree
  behavior.
- IDEATE, executor, merge/eval, related-work search, plugin/HITL/budget,
  resume, and report behavior.
- A deterministic fallback helper, `arbor_state.py`, for Codex/Claude
  environments without native Arbor tools.

Important boundary: this is not a binary replacement for the native `arbor`
CLI runtime. Native dashboard rendering, EventBus streaming, provider runtime,
full native executor concurrency, and the background SearchAgent lifecycle
still belong to the original Arbor runtime. If the native `arbor` CLI is
installed and the goal is a production Arbor run, prefer the native runtime.
This skill suite is intended to make Codex or Claude Code behave according to
the Arbor design when native Arbor tools are unavailable or when a
skill-based reconstruction is desired.

## Skill Layout

Install these 11 skill directories as a single suite:

| Skill | Responsibility |
| --- | --- |
| `arbor-research-agent` | Public entrypoint. Performs Arbor-style intake/clarification, forms the research contract, then loads the orchestrator. |
| `arbor-agent-orchestrator` | Top-level phase loader and policy owner. Decides when to load each phase skill. |
| `arbor-agent-setup-intake` | Project intake, metric/eval discovery, baseline handling, B_dev/B_test policy, and session setup. |
| `arbor-agent-coordinator` | INIT/OBSERVE/IDEATE/SELECT/DISPATCH/DECIDE loop and durable Idea Tree operation. |
| `arbor-agent-ideate` | Reconstructs idea drafting and first-principles probing. Enforces constraints view and four-line hypotheses. |
| `arbor-agent-executor` | Executor/worktree/prompt/report/metrics/insight-propagation behavior. |
| `arbor-agent-merge-eval` | B_dev/B_test separation, merge guards, protected paths, metric direction, and final scoring. |
| `arbor-agent-search` | Related-work and novelty search for validated winners. |
| `arbor-agent-plugins-hitl-budget` | Plugin/profile precedence, MLE/Kaggle behavior, HITL gates, and budget/cycle policy. |
| `arbor-agent-resume-report` | Checkpoint/resume behavior, running-node requeue, and `REPORT.md` finalization. |
| `arbor-agent-tools` | Deterministic fallback tools for environments without native Arbor tools. |

Each skill directory contains:

- `SKILL.md`: the cross-platform instruction body used by Codex and Claude
  Code.
- `agents/openai.yaml`: OpenAI/Codex UI metadata. This file controls display
  name, short description, and default prompt text. It does not contain the
  execution logic.

Additional resources:

- `arbor-agent-orchestrator/references/source-map.md`: source-level mapping
  from the `arbor` open-source branch to this suite.
- `arbor-agent-orchestrator/references/compatibility.md`: Codex and Claude
  Code compatibility notes.
- `arbor-agent-tools/references/tool-mapping.md`: mapping between native Arbor
  tools and fallback helper commands.
- `arbor-agent-tools/scripts/arbor_state.py`: a stdlib-only helper that
  supports `init`, `view`, `meta`, `add`, `update`, `prune`, `propagate`,
  `eval`, `parse-log`, `prompt-executor`, `record`, `worktree`, `merge`,
  `check`, and `report`.

## Arbor Behavior Mapping

| Arbor behavior | Skill suite equivalent |
| --- | --- |
| `arbor run` starts with intake and a Research Contract | `arbor-research-agent` |
| `.arbor/sessions/<run_name>/` session layout | `arbor-agent-setup-intake` + `arbor-agent-tools` |
| Persistent coordinator ReAct loop | `arbor-agent-orchestrator` + `arbor-agent-coordinator` |
| `TreeView`, `TreeAddNode`, `TreeSetMeta`, `TreeUpdateNode`, `TreePropagate` | `arbor-agent-coordinator` + `arbor_state.py` |
| `TreeView(format="constraints")` before ideation | `arbor-agent-ideate` |
| Four-line hypothesis: `Mechanism`, `Hypothesis`, `Observable`, `Conflicts` | `arbor-agent-ideate` |
| `RunExecutor` / `RunExecutorParallel` | `arbor-agent-executor` |
| Executor evaluates on B_dev and avoids B_test | `arbor-agent-executor` + `arbor-agent-merge-eval` |
| `GitMergeBranch` auto-runs B_test verification and protected-path checks | `arbor-agent-merge-eval` + `arbor_state.py merge` |
| SearchAgent annotates validated winners only | `arbor-agent-search` |
| Plugin/profile/HITL/budget policy | `arbor-agent-plugins-hitl-budget` |
| Checkpoint/resume/final report | `arbor-agent-resume-report` + `arbor_state.py report` |
| Long training and noisy progress logs | `arbor-agent-executor` + `arbor_state.py parse-log` |

## Loading In Codex

### Recommended installation

Codex installs skills into `${CODEX_HOME:-$HOME/.codex}/skills` by default.
From this repository, install the whole suite with:

```bash
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$CODEX_SKILLS_DIR"
cp -R <path-to-Arbor>/skills/arbor-* "$CODEX_SKILLS_DIR"/
```

Restart Codex after copying the skills.

Then open Codex in the target project and invoke the public entrypoint:

```text
$arbor-research-agent optimize this repo for the leaderboard metric. Ask before training, installing packages, or using B_test.
```

For a smoke test:

```text
$arbor-research-agent try a one-cycle smoke run in this repo. Do not edit source, do not train, use cached metrics where safe, and write an Arbor-style report.
```

### One-off forward test

For a temporary test without installing the suite globally, expose this
repository to Codex and explicitly tell the agent to start from the public
entrypoint:

```bash
codex exec --add-dir <path-to-Arbor> -C <target_repo> \
  'Use the skill suite under <path-to-Arbor>/skills. Start from arbor-research-agent. <your task>'
```

This is useful for validation. For normal use, install the skills into the
Codex skills directory.

## Loading In Claude Code

Claude Code skills are directories that contain a `SKILL.md` file. Official
Claude Code documentation describes both project skills under
`.claude/skills/*/SKILL.md` and user skills under `~/.claude/skills/`. Direct
skill invocation uses `/skill-name`.

Reference: <https://code.claude.com/docs/en/skills>

### User-level installation

Use this when you want the suite available across multiple projects:

```bash
mkdir -p ~/.claude/skills
cp -R <path-to-Arbor>/skills/arbor-* ~/.claude/skills/
```

Restart Claude Code, open the target project, and invoke:

```text
/arbor-research-agent optimize this repo for the validation score. Ask before running training or editing protected files.
```

### Project-level installation

Use this when you want the suite attached to one repository:

```bash
mkdir -p <target_repo>/.claude/skills
cp -R <path-to-Arbor>/skills/arbor-* <target_repo>/.claude/skills/
```

Then start Claude Code inside `<target_repo>` and invoke:

```text
/arbor-research-agent try a smoke-only Arbor run. Use current cwd, no training, no source edits, one cycle, write REPORT.md.
```

If Claude Code does not auto-trigger the skill, explicitly ask it to read the
public entrypoint:

```text
Read .claude/skills/arbor-research-agent/SKILL.md and follow it as the public entrypoint. Then handle: <your task>
```

## Usage After Loading

### Real run

Codex:

```text
$arbor-research-agent optimize this repo for <metric>. You may edit source through executor branches, run <eval command> on B_dev, and stop after 5 cycles or 4 hours. Ask before package installs, data downloads, GPU jobs longer than 30 minutes, or B_test.
```

Claude Code:

```text
/arbor-research-agent optimize this repo for <metric>. You may edit source through executor branches, run <eval command> on B_dev, and stop after 5 cycles or 4 hours. Ask before package installs, data downloads, GPU jobs longer than 30 minutes, or B_test.
```

Expected behavior:

- The wrapper inspects local context and git state.
- If target, metric, eval, permissions, or budget are ambiguous, it asks a
  compact clarification checkpoint.
- Once the contract is clear, it loads the orchestrator.
- The orchestrator initializes `.arbor/sessions/<run_name>/`.
- The coordinator manages candidates through the Idea Tree.
- Executors implement and evaluate ideas within the allowed edit surface.
- Merge/eval protects B_test and trunk.
- The run ends with a `REPORT.md`.

### Smoke or forward test

Codex:

```text
$arbor-research-agent try a one-cycle smoke run. Use cached metrics/defaults where safe, do not run training, do not edit source, do not create worktrees, and write an Arbor-style report.
```

Claude Code:

```text
/arbor-research-agent try a one-cycle smoke run. Use cached metrics/defaults where safe, do not run training, do not edit source, do not create worktrees, and write an Arbor-style report.
```

Expected artifacts:

```text
.arbor/sessions/<run_name>/.coordinator/idea_tree.json
.arbor/sessions/<run_name>/.coordinator/idea_tree.md
.arbor/sessions/<run_name>/experiments/<node_id>/executor_prompt.md
.arbor/sessions/<run_name>/experiments/<node_id>/report.md
.arbor/sessions/<run_name>/experiments/<node_id>/metrics.json
.arbor/sessions/<run_name>/REPORT.md
```

### Ambiguous request

For a vague request such as:

```text
$arbor-research-agent make this model better overnight
```

the wrapper should ask a compact checkpoint similar to:

```text
I can start, but I need these defaults confirmed:
- target: <cwd>
- objective/metric: <inferred or unknown>
- eval: <inferred command or unknown>
- run mode: smoke / real
- permissions: may edit code? may run training/GPU? may install packages?
- budget: <cycles/time>

Reply "yes" to accept, or edit any line.
```

This mirrors the native Arbor intake and Research Contract experience.

## Runtime Guardrails

- Install all `arbor-*` skill directories, not only `arbor-research-agent`.
- Users should invoke only the public entrypoint. Internal phase skills are
  loaded by the wrapper/orchestrator.
- `try`, `test`, `demo`, and `smoke` requests default to smoke-only.
- Smoke mode does not run training, downloads, long GPU jobs, real worktrees,
  or real merges.
- Real training, package installation, data download, B_test use, and merge
  operations require explicit user permission.
- B_test must not be used for routine iteration.
- The coordinator should not directly edit project source. Source changes go
  through executor/worktree behavior.
- Do not inspect long logs with raw `cat`/`grep`. Prefer:

```bash
python skills/arbor-agent-tools/scripts/arbor_state.py parse-log --log <log> --metric <metric>
```

## Validation Commands

Run these from the Arbor repository root.

Compile the deterministic helper:

```bash
python -m py_compile skills/arbor-agent-tools/scripts/arbor_state.py
```

Validate every skill frontmatter:

```bash
find skills -mindepth 1 -maxdepth 1 -type d | sort | while read -r d; do
  printf '%s: ' "$d"
  uv run --with pyyaml python <path-to-skill-creator>/scripts/quick_validate.py "$d"
done
```

Validate OpenAI metadata:

```bash
uv run --with pyyaml python - <<'PY'
from pathlib import Path
import yaml
for path in sorted(Path("skills").glob("*/agents/openai.yaml")):
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), path
    assert isinstance(data.get("interface"), dict), path
    for key in ("display_name", "short_description", "default_prompt"):
        assert data["interface"].get(key), (path, key)
print("openai.yaml valid:", len(list(Path("skills").glob("*/agents/openai.yaml"))))
PY
```

Validate a smoke session:

```bash
python skills/arbor-agent-tools/scripts/arbor_state.py check --cwd <target_repo> --run-name <run_name> \
  --require-report --require-experiment --require-executor-prompt
```

## Verified Behavior

The suite has been validated with both static checks and a dynamic Codex smoke
run in a disposable target repository outside the Arbor checkout. A reproducible
validation should confirm:

- All 11 skills pass `quick_validate.py`.
- All 11 `agents/openai.yaml` files parse correctly.
- `arbor_state.py` compiles.
- `arbor_state.py check` returns `OK` with the expected artifact flags.
- A fresh Codex session starting only from `$arbor-research-agent` performs
  intake, loads the orchestrator and phase skills, maintains an Idea Tree,
  dispatches through executor-style behavior, and writes Arbor-style artifacts.
- Smoke runs do not execute package syncs, training scripts, GPU training,
  downloads, full evals, worktrees, merges, or source edits unless the user
  explicitly requested a real run.

## Troubleshooting

### Only one skill appears, or internal skills do not load

You probably copied only `arbor-research-agent`. Copy every `arbor-*` skill
directory and restart Codex or Claude Code.

### Claude Code does not trigger `/arbor-research-agent`

Check the installation path:

- User-level: `~/.claude/skills/arbor-research-agent/SKILL.md`
- Project-level: `<target_repo>/.claude/skills/arbor-research-agent/SKILL.md`

You can also explicitly prompt:

```text
Read .claude/skills/arbor-research-agent/SKILL.md and follow it as the public entrypoint.
```

### Codex does not trigger `$arbor-research-agent`

Check that the skill exists at:

```text
${CODEX_HOME:-$HOME/.codex}/skills/arbor-research-agent/SKILL.md
```

Restart Codex after installation. For a one-off test, use `--add-dir` and
explicitly tell the agent to start from `arbor-research-agent`.

### When should I use native Arbor instead?

Use the native `arbor` CLI when you want a production Arbor run and the native
runtime is installed. Use this skill suite when you want to:

- Reproduce Arbor-style behavior in Codex or Claude Code.
- Work in an environment without native Arbor tools.
- Run smoke or forward tests.
- Teach an agent to follow Arbor's research discipline.
- Share a cross-platform `SKILL.md`-based workflow.

---

# Arbor Research Agent Skill Suite（中文）

本目录包含一套 Codex/Claude Code skill suite，用 Agent Skills 的形式复刻
`arbor` open-source 分支中的 Arbor/AutoResearch 行为。

大多数用户只需要调用公开入口：

```text
$arbor-research-agent <你的研究或优化需求>
```

在 Claude Code 中，通常使用：

```text
/arbor-research-agent <你的研究或优化需求>
```

内部阶段 skill 仍然必须安装。请把所有 `arbor-*` skill 目录作为一整套安装，不要只
安装 `arbor-research-agent`。

## 快速下载与安装

把 `REPO_URL` 设置成 Arbor GitHub 仓库地址，把 `REPO_REF` 设置成包含这个
`skills/` 目录的分支或 tag。

```bash
REPO_URL="https://github.com/RUC-NLPIR/Arbor.git"
REPO_REF="main"
TMP_DIR="$(mktemp -d)"
git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$TMP_DIR/arbor-skill-suite"
SKILLS_SRC="$TMP_DIR/arbor-skill-suite/skills"
```

如果从当前本地 checkout 安装，而不是从 GitHub 下载，使用：

```bash
SKILLS_SRC="<path-to-Arbor>/skills"
```

### 安装到 Codex

```bash
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$CODEX_SKILLS_DIR"
cp -R "$SKILLS_SRC"/arbor-* "$CODEX_SKILLS_DIR"/
find "$CODEX_SKILLS_DIR" -maxdepth 1 -type d -name 'arbor-*' | sort
```

安装后重启 Codex，然后调用：

```text
$arbor-research-agent <你的任务>
```

### 安装到 Claude Code

用户级安装：

```bash
mkdir -p ~/.claude/skills
cp -R "$SKILLS_SRC"/arbor-* ~/.claude/skills/
find ~/.claude/skills -maxdepth 1 -type d -name 'arbor-*' | sort
```

项目级安装：

```bash
mkdir -p <target_repo>/.claude/skills
cp -R "$SKILLS_SRC"/arbor-* <target_repo>/.claude/skills/
find <target_repo>/.claude/skills -maxdepth 1 -type d -name 'arbor-*' | sort
```

安装后重启 Claude Code，然后调用：

```text
/arbor-research-agent <你的任务>
```

### 让 Codex 自己安装

把下面这段 prompt 交给 Codex：

```text
Install the Arbor Research Agent skill suite from
https://github.com/RUC-NLPIR/Arbor.git, branch main. Clone the repo
into a temporary directory, locate skills/arbor-research-agent/SKILL.md, and
copy every skills/arbor-* directory into ${CODEX_HOME:-$HOME/.codex}/skills. Do
not copy only the wrapper skill. Do not modify the target project source. After
copying, verify that 11 arbor-* skill directories exist and that each contains
SKILL.md. Then tell me to restart Codex and show the exact path you installed
to.
```

如果从当前本地 checkout 安装，使用：

```text
Install the Arbor Research Agent skill suite from <path-to-Arbor>/skills.
Copy every arbor-* directory into ${CODEX_HOME:-$HOME/.codex}/skills. Do not
copy only the wrapper skill. Verify that 11 arbor-* skill directories exist and
that each contains SKILL.md. Then tell me to restart Codex and show the exact
path you installed to.
```

### 让 Claude Code 自己安装

把下面这段 prompt 交给 Claude Code：

```text
Install the Arbor Research Agent skill suite from
https://github.com/RUC-NLPIR/Arbor.git, branch main. Clone the repo
into a temporary directory, locate skills/arbor-research-agent/SKILL.md, and
copy every skills/arbor-* directory into ~/.claude/skills. Do not copy only the
wrapper skill. Do not modify the target project source. After copying, verify
that 11 arbor-* skill directories exist and that each contains SKILL.md. Then
tell me to restart Claude Code and show the exact path you installed to.
```

项目级安装使用下面这段：

```text
Install the Arbor Research Agent skill suite from
https://github.com/RUC-NLPIR/Arbor.git, branch main, into this
project's .claude/skills directory. Clone the repo into a temporary directory,
locate skills/arbor-research-agent/SKILL.md, and copy every skills/arbor-*
directory into .claude/skills. Do not copy only the wrapper skill. Do not modify
source files outside .claude/skills. Verify that 11 arbor-* skill directories
exist and that each contains SKILL.md. Then tell me to restart Claude Code.
```

## 当前状态

这套 skill suite 是可用的，并且在 Agent Skills 能表达和执行的层面上对齐 Arbor 的
核心行为。

它覆盖：

- 类似 `arbor run` 的公开 intake 入口。
- 对目标、metric、数据、eval、权限、预算、run mode 缺失时的 Arbor 式反问。
- 用户已给出足够约束或明确使用安全 defaults 时的 fast path。
- 从 wrapper 传给 orchestrator 的 research contract。
- 阶段加载式 orchestrator，而不是一个单体大 skill。
- 持久化 `.arbor/sessions/<run_name>/` session state。
- 使用 Idea Tree 作为跨上下文的 durable memory。
- B_dev/B_test 纪律：B_dev 用于迭代，B_test 只用于 merge/final verification。
- Coordinator 纪律：coordinator 不直接修改 benchmark 或项目源码，真实实现通过
  executor/worktree 行为完成。
- IDEATE、executor、merge/eval、related-work search、plugin/HITL/budget、
  resume、report 等 Arbor 行为面。
- 在没有原生 Arbor tools 的 Codex/Claude 环境中，用 deterministic helper
  `arbor_state.py` 模拟核心状态工具。

边界也要明确：这不是原生 `arbor` CLI runtime 的二进制替代品。原生 dashboard、
EventBus streaming、provider runtime、完整原生 executor 并发、后台 SearchAgent
生命周期仍属于原项目 runtime。如果机器上已安装原生 `arbor` CLI，并且目标是生产级
完整 Arbor run，应优先使用原生 runtime。这套 skill suite 的目标是在没有原生 Arbor
tools，或需要 skill-based reconstruction 的环境中，让 Codex/Claude Code 按 Arbor 的
设计行动。

## Skill 结构

请把下面 11 个 skill 目录作为一整套安装：

| Skill | 责任 |
| --- | --- |
| `arbor-research-agent` | 公开入口。做 Arbor 式 intake/clarification，形成 research contract，然后加载 orchestrator。 |
| `arbor-agent-orchestrator` | 顶层 phase loader 和 policy owner，决定何时加载各阶段 skill。 |
| `arbor-agent-setup-intake` | 项目 intake、metric/eval 发现、baseline、B_dev/B_test policy、session setup。 |
| `arbor-agent-coordinator` | INIT/OBSERVE/IDEATE/SELECT/DISPATCH/DECIDE 大循环和 durable Idea Tree。 |
| `arbor-agent-ideate` | 复刻 idea drafting 和 first-principles probing，强制 constraints view 和四行 hypothesis。 |
| `arbor-agent-executor` | executor/worktree/prompt/report/metrics/insight propagation 行为。 |
| `arbor-agent-merge-eval` | B_dev/B_test 隔离、merge guard、protected paths、metric direction、final scoring。 |
| `arbor-agent-search` | 对 validated winner 做 related-work 和 novelty search。 |
| `arbor-agent-plugins-hitl-budget` | plugin/profile 优先级、MLE/Kaggle 行为、HITL gates、budget/cycle policy。 |
| `arbor-agent-resume-report` | checkpoint/resume、running-node requeue、`REPORT.md` finalization。 |
| `arbor-agent-tools` | 没有原生 Arbor tools 时的 deterministic fallback tools。 |

每个 skill 目录包含：

- `SKILL.md`：Codex 和 Claude Code 共用的主要指令。
- `agents/openai.yaml`：OpenAI/Codex UI metadata。它控制展示名、短描述和默认
  prompt，不承载执行逻辑。

额外资源：

- `arbor-agent-orchestrator/references/source-map.md`：从 `arbor`
  open-source 分支到本 suite 的源码级映射。
- `arbor-agent-orchestrator/references/compatibility.md`：Codex 和 Claude Code
  兼容性说明。
- `arbor-agent-tools/references/tool-mapping.md`：原生 Arbor tools 和 fallback helper
  commands 的映射。
- `arbor-agent-tools/scripts/arbor_state.py`：stdlib-only helper，支持 `init`、
  `view`、`meta`、`add`、`update`、`prune`、`propagate`、`eval`、`parse-log`、
  `prompt-executor`、`record`、`worktree`、`merge`、`check`、`report`。

## Arbor 行为映射

| Arbor 行为 | skill suite 对应 |
| --- | --- |
| `arbor run` 先进入 intake 并整理 Research Contract | `arbor-research-agent` |
| `.arbor/sessions/<run_name>/` session layout | `arbor-agent-setup-intake` + `arbor-agent-tools` |
| 持久 coordinator ReAct loop | `arbor-agent-orchestrator` + `arbor-agent-coordinator` |
| `TreeView`, `TreeAddNode`, `TreeSetMeta`, `TreeUpdateNode`, `TreePropagate` | `arbor-agent-coordinator` + `arbor_state.py` |
| IDEATE 前必须 `TreeView(format="constraints")` | `arbor-agent-ideate` |
| 四行 hypothesis：`Mechanism`, `Hypothesis`, `Observable`, `Conflicts` | `arbor-agent-ideate` |
| `RunExecutor` / `RunExecutorParallel` | `arbor-agent-executor` |
| Executor 使用 B_dev，不碰 B_test | `arbor-agent-executor` + `arbor-agent-merge-eval` |
| `GitMergeBranch` 自动做 B_test verification 和 protected-path checks | `arbor-agent-merge-eval` + `arbor_state.py merge` |
| SearchAgent 只标注 validated winners | `arbor-agent-search` |
| plugin/profile/HITL/budget policy | `arbor-agent-plugins-hitl-budget` |
| checkpoint/resume/final report | `arbor-agent-resume-report` + `arbor_state.py report` |
| 长训练和噪声 progress logs | `arbor-agent-executor` + `arbor_state.py parse-log` |

## 在 Codex 中加载

### 推荐安装方式

Codex 默认把 skill 安装到 `${CODEX_HOME:-$HOME/.codex}/skills`。从本仓库安装整套
suite：

```bash
CODEX_SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$CODEX_SKILLS_DIR"
cp -R <path-to-Arbor>/skills/arbor-* "$CODEX_SKILLS_DIR"/
```

复制后重启 Codex。

然后在目标项目中启动 Codex，调用公开入口：

```text
$arbor-research-agent optimize this repo for the leaderboard metric. Ask before training, installing packages, or using B_test.
```

Smoke test 示例：

```text
$arbor-research-agent try a one-cycle smoke run in this repo. Do not edit source, do not train, use cached metrics where safe, and write an Arbor-style report.
```

### 一次性 forward test

如果只是临时测试，不想安装到全局 skills 目录，可以把本仓库暴露给 Codex，并在 prompt
中明确要求从公开入口开始：

```bash
codex exec --add-dir <path-to-Arbor> -C <target_repo> \
  'Use the skill suite under <path-to-Arbor>/skills. Start from arbor-research-agent. <your task>'
```

这种方式适合验证。正式使用仍建议安装到 Codex skills 目录。

## 在 Claude Code 中加载

Claude Code skills 是包含 `SKILL.md` 的目录。官方文档说明，项目级 skill 可以放在
`.claude/skills/*/SKILL.md`，用户级 skill 可以放在 `~/.claude/skills/`。直接调用
skill 的语法是 `/skill-name`。

参考：<https://code.claude.com/docs/en/skills>

### 用户级安装

适合在多个项目中复用：

```bash
mkdir -p ~/.claude/skills
cp -R <path-to-Arbor>/skills/arbor-* ~/.claude/skills/
```

重启 Claude Code，在目标项目中调用：

```text
/arbor-research-agent optimize this repo for the validation score. Ask before running training or editing protected files.
```

### 项目级安装

适合把 suite 绑定到某一个 repo：

```bash
mkdir -p <target_repo>/.claude/skills
cp -R <path-to-Arbor>/skills/arbor-* <target_repo>/.claude/skills/
```

然后在 `<target_repo>` 中启动 Claude Code，调用：

```text
/arbor-research-agent try a smoke-only Arbor run. Use current cwd, no training, no source edits, one cycle, write REPORT.md.
```

如果 Claude Code 没有自动触发 skill，可以显式要求它读取公开入口：

```text
Read .claude/skills/arbor-research-agent/SKILL.md and follow it as the public entrypoint. Then handle: <your task>
```

## 加载后如何使用

### 真实运行

Codex：

```text
$arbor-research-agent optimize this repo for <metric>. You may edit source through executor branches, run <eval command> on B_dev, and stop after 5 cycles or 4 hours. Ask before package installs, data downloads, GPU jobs longer than 30 minutes, or B_test.
```

Claude Code：

```text
/arbor-research-agent optimize this repo for <metric>. You may edit source through executor branches, run <eval command> on B_dev, and stop after 5 cycles or 4 hours. Ask before package installs, data downloads, GPU jobs longer than 30 minutes, or B_test.
```

预期行为：

- wrapper 检查本地上下文和 git 状态。
- 如果 target、metric、eval、permissions、budget 不明确，就先问一个 compact
  clarification checkpoint。
- contract 清楚后加载 orchestrator。
- orchestrator 初始化 `.arbor/sessions/<run_name>/`。
- coordinator 用 Idea Tree 管理候选方向。
- executor 在允许的 edit surface 内实现和评估。
- merge/eval 保护 B_test 和 trunk。
- 最后生成 `REPORT.md`。

### Smoke 或 forward test

Codex：

```text
$arbor-research-agent try a one-cycle smoke run. Use cached metrics/defaults where safe, do not run training, do not edit source, do not create worktrees, and write an Arbor-style report.
```

Claude Code：

```text
/arbor-research-agent try a one-cycle smoke run. Use cached metrics/defaults where safe, do not run training, do not edit source, do not create worktrees, and write an Arbor-style report.
```

预期 artifacts：

```text
.arbor/sessions/<run_name>/.coordinator/idea_tree.json
.arbor/sessions/<run_name>/.coordinator/idea_tree.md
.arbor/sessions/<run_name>/experiments/<node_id>/executor_prompt.md
.arbor/sessions/<run_name>/experiments/<node_id>/report.md
.arbor/sessions/<run_name>/experiments/<node_id>/metrics.json
.arbor/sessions/<run_name>/REPORT.md
```

### 模糊需求

例如：

```text
$arbor-research-agent make this model better overnight
```

wrapper 应该先问类似下面的 compact checkpoint：

```text
I can start, but I need these defaults confirmed:
- target: <cwd>
- objective/metric: <inferred or unknown>
- eval: <inferred command or unknown>
- run mode: smoke / real
- permissions: may edit code? may run training/GPU? may install packages?
- budget: <cycles/time>

Reply "yes" to accept, or edit any line.
```

这对应原生 Arbor intake 和 Research Contract 的体验。

## 运行期保护规则

- 必须安装所有 `arbor-*` skill 目录，不要只安装 `arbor-research-agent`。
- 用户只调用公开入口。内部阶段 skill 由 wrapper/orchestrator 加载。
- `try`、`test`、`demo`、`smoke` 默认 smoke-only。
- Smoke mode 不跑训练、下载、GPU 长任务、真实 worktree 或真实 merge。
- 真实 training、package install、data download、B_test、merge 都需要用户明确授权。
- B_test 不能用于常规迭代。
- Coordinator 不应直接修改项目源码；源码改动通过 executor/worktree 路径完成。
- 不要用 raw `cat`/`grep` 扫长日志。优先使用：

```bash
python skills/arbor-agent-tools/scripts/arbor_state.py parse-log --log <log> --metric <metric>
```

## 验证命令

在 Arbor 仓库根目录下运行。

编译 deterministic helper：

```bash
python -m py_compile skills/arbor-agent-tools/scripts/arbor_state.py
```

校验所有 skill frontmatter：

```bash
find skills -mindepth 1 -maxdepth 1 -type d | sort | while read -r d; do
  printf '%s: ' "$d"
  uv run --with pyyaml python <path-to-skill-creator>/scripts/quick_validate.py "$d"
done
```

校验 OpenAI metadata：

```bash
uv run --with pyyaml python - <<'PY'
from pathlib import Path
import yaml
for path in sorted(Path("skills").glob("*/agents/openai.yaml")):
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), path
    assert isinstance(data.get("interface"), dict), path
    for key in ("display_name", "short_description", "default_prompt"):
        assert data["interface"].get(key), (path, key)
print("openai.yaml valid:", len(list(Path("skills").glob("*/agents/openai.yaml"))))
PY
```

校验 smoke session：

```bash
python skills/arbor-agent-tools/scripts/arbor_state.py check --cwd <target_repo> --run-name <run_name> \
  --require-report --require-experiment --require-executor-prompt
```

## 已验证行为

这套 suite 已经过静态检查和动态 Codex smoke run 验证；动态验证应在 Arbor checkout
之外的临时目标仓库中进行。一次可复现的验证应确认：

- 11 个 skill 全部通过 `quick_validate.py`。
- 11 个 `agents/openai.yaml` 都可解析。
- `arbor_state.py` 可编译。
- `arbor_state.py check` 在带上预期 artifact flags 时返回 `OK`。
- 新 Codex 只从 `$arbor-research-agent` 入口启动时，能完成 intake，加载
  orchestrator 和各阶段 skill，维护 Idea Tree，通过 executor-style 行为派发，并生成
  Arbor-style artifacts。
- 除非用户明确要求真实运行，smoke run 不会执行 package sync、训练脚本、GPU
  training、downloads、full evals、worktrees、merges 或 source edits。

## 常见问题

### 只看到一个 skill，或者内部 skill 没加载

通常是只复制了 `arbor-research-agent`。需要复制所有 `arbor-*` skill 目录，并重启
Codex 或 Claude Code。

### Claude Code 没有触发 `/arbor-research-agent`

检查安装路径：

- 用户级：`~/.claude/skills/arbor-research-agent/SKILL.md`
- 项目级：`<target_repo>/.claude/skills/arbor-research-agent/SKILL.md`

也可以显式提示：

```text
Read .claude/skills/arbor-research-agent/SKILL.md and follow it as the public entrypoint.
```

### Codex 没有触发 `$arbor-research-agent`

检查 skill 是否存在于：

```text
${CODEX_HOME:-$HOME/.codex}/skills/arbor-research-agent/SKILL.md
```

安装后需要重启 Codex。一次性测试时，用 `--add-dir` 暴露本仓库，并明确要求 agent 从
`arbor-research-agent` 开始。

### 什么时候应该使用原生 Arbor？

如果需要生产级完整 Arbor run，且原生 `arbor` CLI 已安装，优先使用原生 runtime。
这套 skill suite 更适合：

- 在 Codex 或 Claude Code 中复刻 Arbor-style 行为。
- 没有原生 Arbor tools 的环境。
- smoke 或 forward test。
- 教 agent 遵守 Arbor 的研究纪律。
- 共享跨平台的 `SKILL.md` 工作流。
