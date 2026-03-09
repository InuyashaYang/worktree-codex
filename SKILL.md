---
name: worktree-codex
description: >
  使用 git worktree 隔离多个 Codex 实例，由 OpenClaw 主控器并行调度完成同一项目的不同编码模块。
  适用场景：将一个编码项目拆分为独立子任务，让多个 Codex 实例并行实现，最后合并。
  触发条件：用户要求"多个 Codex 协作"、"并行编码"、"worktree 编码"、"多 Codex 编排"、"并行完成项目"时激活。
---

# Worktree Codex — 多 Codex 并行编码技能

## 架构分层

```
Layer 4 — 决策层       conductor.sh
                        全局生命周期、Contract 验证、追问注入、终止判断
Layer 3 — Agent 层     orchestrate.sh
                        单 Agent 执行循环、注入监听、Codex session 管理
Layer 2 — 基础设施层   setup_worktrees.sh / push_and_pr.sh
                        lib/syntax.sh（可插拔语法检查）
                        lib/contract.sh（CONTRACT 解析与注入 prompt 生成）
Layer 1 — 可观测层     dashboard.py + launch.sh
                        只读展示，SSE 推流，不介入决策
```

**层间通信协议：**
- conductor → orchestrate：写 `/tmp/wt-inject-<name>.txt`（异步注入）
- orchestrate → conductor：写 `##AGENT_DONE##` 到 log（被动轮询）
- orchestrate → dashboard：写 `##标记##` 到 log（只追加，单向）
- conductor → dashboard：`POST /reload`（通知新任务开始）

## 前提条件

- Codex CLI：`~/.npm-global/bin/codex`（`npm install -g @openai/codex`）
- `OPENAI_API_KEY` + `OPENAI_BASE_URL` 已配置（见 `~/.profile`）
- 目标 Git 仓库已存在（本地）

## 标准工作流（6 步）

### 步骤 1：写 CONTRACT.md（接口契约）

在任务开始前，写好各 Agent 的接口约定，格式如下：

```markdown
## Agent: agent-board
Files: board.js, board.css
Exports:
- class ChessBoard
- method initBoard
- method highlightCell
- method clearHighlights
- method onCellClick

## Agent: agent-logic
Files: logic.js
Exports:
- class ChessLogic
- method getInitialBoard
- method isValidMove
- method applyMove
- method isInCheck
```

CONTRACT 作用：
1. 写入各 Agent 的 task prompt，Agent 知道自己必须实现什么
2. conductor 用它做完成后的自动验证基准（grep 符号名）

### 步骤 2：初始化仓库 + 创建 Worktree

```bash
mkdir -p ~/projects/my-project && cd ~/projects/my-project
git init && echo "# My Project" > README.md
git add . && git commit -m "init"

bash skills/worktree-codex/scripts/setup_worktrees.sh \
  ~/projects/my-project \
  ~/projects/my-project-worktrees \
  agent-a agent-b agent-c
```

输出：`agent_name:worktree_path:branch_name`（每行一个 Agent）

### 步骤 3：启动展板

```bash
bash skills/worktree-codex/scripts/launch.sh \
  --port 7789 \
  --llm-base-url http://152.53.52.170:3003/v1 \
  --llm-api-key sk-xxx \
  --llm-model gpt-4.1-mini \
  --bg --open

# 随时查地址：
cat /tmp/wt-dashboard.url
bash skills/worktree-codex/scripts/launch.sh --find
```

### 步骤 4：并行启动各 Agent（后台）

每个 Agent 调用一次 `orchestrate.sh`，后台运行：

```bash
OPENAI_API_KEY="sk-xxx" OPENAI_BASE_URL="http://BASE/v1" \
DASHBOARD_PORT=7789 \
bash skills/worktree-codex/scripts/orchestrate.sh \
  <repo_dir> <agent_name> <worktree_path> <branch> \
  "<task_prompt>" /tmp/<agent>.log &
```

orchestrate.sh 在每轮 Codex 执行后，会检查 `/tmp/wt-inject-<agent>.txt`。若 conductor 注入了追问，自动启动新的 Codex turn 处理。

**Task prompt 模板（推荐）：**
```
你是 <AgentName>，只能创建和修改 <文件列表>，不得碰其他文件。

任务：<具体实现要求>

接口契约（必须严格导出）：
<CONTRACT 对应 Agent 块的内容>

完成后 git add -A && git commit -m "[<AgentName>] task complete"
```

### 步骤 5：启动 conductor（主控轮询，接管全局生命周期）

```bash
OPENAI_API_KEY="sk-xxx" OPENAI_BASE_URL="http://BASE/v1" \
bash skills/worktree-codex/scripts/conductor.sh \
  --repo <repo_dir> \
  --contract <contract_file> \
  --agents "name:worktree:branch:logfile name2:worktree2:branch2:logfile2" \
  --max-global-rounds 3 \
  --poll-interval 15 \
  --syntax-check "node --check" \
  --dashboard-port 7789
```

**conductor 工作阶段：**
```
Phase 0：等待所有 Agent 首轮 AGENT_DONE（超时 30min）
Phase 1：集成验证
  ├─ 语法检查（syntax-check 命令逐文件）
  └─ CONTRACT 验证（grep 符号名，验证导出存在）
Phase 2：对失败 Agent 写入追问注入
  └─ /tmp/wt-inject-<name>.txt（内容：失败原因+契约要求+文件摘要）
Phase 3：等待追问完成（超时 15min）→ 回 Phase 1
Phase 4：输出报告 /tmp/wt-conductor-report.txt
```

### 步骤 6：合并 + 推 PR（可选）

```bash
cd <repo_dir>
git merge <branch-a> --no-edit
git merge <branch-b> --no-edit
git merge <branch-c> --no-edit

# 推 GitHub PR（需要 GH_TOKEN）
bash skills/worktree-codex/scripts/push_and_pr.sh \
  <repo_dir> <gh_token> <owner/repo> <agent_name> <worktree_path> <branch> main
```

## 环境变量速查

| 变量 | 默认 | 说明 |
|------|------|------|
| `OPENAI_API_KEY` | 无（必填） | API Key |
| `OPENAI_BASE_URL` | `http://152.53.52.170:3003/v1` | 代理地址 |
| `CODEX_MODEL` | `gpt-5.3-codex` | Codex 模型 |
| `CODEX_BIN` | `~/.npm-global/bin/codex` | Codex 二进制 |
| `AGENT_MAX_TURNS` | `3` | 单 Agent 最大追问轮数 |
| `DASHBOARD_PORT` | `7789` | 展板端口 |

## conductor.sh 参数速查

```
--repo <dir>              目标 Git 仓库目录
--contract <file>         CONTRACT.md 路径
--agents "<spec>..."      空格分隔，每项: name:worktree:branch:logfile
--max-global-rounds <N>   全局最大追问轮数（默认 5）
--poll-interval <S>       轮询间隔秒数（默认 10）
--syntax-check "<cmd>"    语法检查命令前缀，如 "node --check"
--dashboard-port <PORT>   展板端口（默认 7789）
```

## launch.sh 参数速查

```
--port <PORT>             展板端口（默认 7789）
--llm-base-url <URL>      AI 分析 LLM base URL
--llm-api-key <KEY>       AI 分析 LLM API Key
--llm-model <MODEL>       AI 分析模型（默认 gpt-4.1-mini）
--no-ai                   禁用 AI 分析
--bg                      后台运行
--open                    启动后自动打开浏览器
--find                    查找运行中的展板
--stop                    停止展板
```

## 已知限制

| 问题 | 原因 | 解决 |
|------|------|------|
| Codex 只支持 `/v1/responses` 端点 | OpenAI 专有格式 | 只用 `gpt-5.x-codex` 系列 |
| worktree 不能放 `/mnt/c/` | NTFS 文件锁 | 放 WSL 本地 `~/projects/` |
| Agent 间通信 | 各自独立 session | 通过 CONTRACT 对齐接口，conductor 做集成验证 |
| 合并冲突 | 文件所有权分配不清 | CONTRACT 里明确每个文件只属于一个 Agent |

## 文件结构

```
skills/worktree-codex/
├── SKILL.md                    # 本文件
├── dashboard.py                # 可观测层（展板服务）
├── requirements.txt            # dashboard 依赖
├── scripts/
│   ├── conductor.sh            # Layer 4：决策层（主控轮询）
│   ├── orchestrate.sh          # Layer 3：Agent 执行层
│   ├── launch.sh               # Layer 1：展板启动入口
│   ├── setup_worktrees.sh      # Layer 2：worktree 隔离
│   ├── push_and_pr.sh          # Layer 2：PR 推送
│   └── lib/
│       ├── syntax.sh           # Layer 2：可插拔语法检查
│       └── contract.sh         # Layer 2：CONTRACT 解析与注入 prompt 生成
└── references/
    ├── DESIGN.md               # 完整设计文档（含取舍说明）
    ├── CONTRACT.md             # CONTRACT 模板（含示例）
    └── task-decomposition.md   # 任务拆分指南
```
