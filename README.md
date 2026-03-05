# worktree-codex

**并行多 Codex / Claude Code 编码编排器 + 实时展板**

将一个编码项目拆成独立子任务，用 `git worktree` 隔离，让多个 AI Coding Agent 实例并行实现，最后自动 commit + 推 PR 合并。

---

## 功能

- **并行编排**：每个 Agent 独占一个 git worktree，互不干扰，完全并行
- **实时展板**（`dashboard.py`）：SSE 推送，每秒刷新，显示 token 用量 / 耗时 / diff / 甘特图
- **一键启动**（`scripts/launch.sh`）：统一入口，LLM 配置通过参数传入，不硬编码
- **AI 效率分析**：所有 Agent 完成后，自动调用 LLM 进行 token 效率 / 弯路 / 最差 prompt 分析
- **任务生命周期绑定**：idle 模式保持页面不失效，`/reload` 恢复 active
- **自动收尾**：Agent 未 commit 时脚本自动 `git add -A && git commit`

---

## 前提

- Git 仓库（本地 + 可选 GitHub 远端）
- [Codex CLI](https://github.com/openai/codex) 或 [Claude Code](https://github.com/anthropics/claude-code)
- Python 3.8+（展板依赖标准库 + `httpx`）
- OpenAI-compatible API（任何实现了 `/v1/responses` 的后端，用于 Codex；展板 AI 分析用 `/v1/chat/completions`）

---

## 快速开始

### 1. 拆分任务 + 创建 Worktree

```bash
bash scripts/setup_worktrees.sh <repo_dir> <worktrees_base_dir> agent-a agent-b
# 输出: agent_name:worktree_path:branch_name（每行一个）
```

### 2. 启动展板

```bash
# 默认：gemini-2.5-flash，key 从环境变量 OPENAI_API_KEY 读
bash scripts/launch.sh --bg --open

# 指定模型和端点
bash scripts/launch.sh \
  --llm-base-url https://openrouter.ai/api/v1 \
  --llm-api-key sk-or-xxx \
  --llm-model google/gemini-flash-1.5:free \
  --bg --open

# 多模态任务
bash scripts/launch.sh --llm-model qwen3-30b-vl --bg --open

# 禁用 AI 分析
bash scripts/launch.sh --no-ai --bg --open
```

浏览器打开 `http://localhost:7789`。

### 3. 并行启动 Agents

```bash
# Agent A（后台）
OPENAI_API_KEY="sk-xxx" OPENAI_BASE_URL="https://api.openai.com/v1" \
bash scripts/orchestrate.sh \
  /path/to/repo agent-a /path/to/worktree-a feat/agent-a \
  "你是 agent-a，只能修改 src/module_a.py。实现 foo() 函数，完成后 git commit。" \
  /tmp/agent-a.log &

# Agent B（后台）
OPENAI_API_KEY="sk-xxx" OPENAI_BASE_URL="https://api.openai.com/v1" \
bash scripts/orchestrate.sh \
  /path/to/repo agent-b /path/to/worktree-b feat/agent-b \
  "你是 agent-b，只能修改 src/module_b.py。实现 bar() 函数，完成后 git commit。" \
  /tmp/agent-b.log &

wait
```

### 4. 推 PR + 合并

```bash
bash scripts/push_and_pr.sh \
  <repo_dir> <gh_token> <owner/repo> agent-a /path/to/worktree-a feat/agent-a main
```

---

## launch.sh 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--port` | 7789 | 展板端口 |
| `--logs` | 无 | 初始 log 路径（可 glob，后续 agent 自动注册） |
| `--llm-base-url` | `http://YOUR_PROXY_HOST:PORT/v1` | AI 分析 LLM 端点 |
| `--llm-api-key` | 环境变量 `OPENAI_API_KEY` | AI 分析 API Key |
| `--llm-model` | `gemini-2.5-flash` | AI 分析模型；多模态用 `qwen3-30b-vl` |
| `--no-ai` | — | 禁用 AI 分析 |
| `--bg` | — | 后台运行 |
| `--open` | — | 自动打开浏览器 |

---

## orchestrate.sh 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `OPENAI_API_KEY` | 必填 | Codex 用 API Key |
| `OPENAI_BASE_URL` | `http://YOUR_PROXY_HOST:PORT/v1` | Codex 用 base URL（需实现 `/v1/responses`） |
| `CODEX_MODEL` | `gpt-5.3-codex` | Codex 模型 |
| `CODEX_BIN` | `~/.npm-global/bin/codex` | Codex 二进制路径 |
| `DASHBOARD_PORT` | 7789 | 展板端口（自动注册 log） |

---

## 展板展示

```
⚡ worktree-codex    agents: 3  ✅ 2 done  ❌ 0 fail  🔥 45,231 tok
                    ⚡ 并行节省 183s（串行需 312s，实际 129s）

[agent-a ✅]  gpt-5.3-codex · 10:00 → 10:02
  🔥 18,432 tok  ⏱ 127s  📝 3f +95 -12  ⚡ 193.5 tok/ln  🔄 8 calls

[agent-b ✅]  gpt-5.3-codex · 10:00 → 10:02
  🔥 14,211 tok  ⏱ 129s  📝 2f +44 -6   ⚡ 237.5 tok/ln  🔄 5 calls
```

---

## 已知限制

- Codex CLI 要求后端实现 `/v1/responses`（OpenAI 原生 API）；OpenRouter 不支持
- WSL2：worktree 放在 WSL 本地文件系统（`~/`），不放 `/mnt/c/`，避免 NTFS 权限错误
- Claude Code 需要 `--dangerously-skip-permissions` 才能在 worktree 写文件

---

## OpenClaw Skill

本项目同时是一个 [OpenClaw](https://github.com/openclaw/openclaw) Skill，可通过 `SKILL.md` 直接在 OpenClaw 中使用。

```
安装：将整个目录放入 ~/.openclaw/workspace/skills/worktree-codex/
触发：告诉 OpenClaw "多个 Codex 协作" / "并行编码" / "worktree 编码"
```

---

## License

MIT
