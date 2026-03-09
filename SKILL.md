---
name: worktree-codex
description: 多 Agent 并行编程 + DAG 调度。当用户要求"用多个 Agent 并行开发"、"worktree 编排"、"多 Codex 协作"、"帮我搭一个 xxx 项目（前端/Web App）"时使用。
---

# worktree-codex

多 Agent 并行编程系统。一句话描述 → LLM 自动规划 DAG → 多 Codex Agent 并行实现 → 验证合并。

## 触发场景

- 用户说"用 worktree-codex 做 xxx"
- 用户要求"多 Agent 并行开发 xxx"
- 用户要求"帮我搭一个前端项目 xxx"（中等复杂度，适合多模块并行）

## 调用方式

收到请求后：

1. 从用户描述中提取参数：
   - `desc`：项目一句话描述（尽量详细，包含功能点）
   - `repo`：项目路径，默认 `~/projects/<项目名>`
   - `agents`：agent 数量，默认 3-4，复杂项目 5
   - `tech`：技术栈，默认 "ES module，无第三方库，纯原生 JS+CSS"

2. 创建 repo 目录并初始化 git：
```bash
mkdir -p <repo> && cd <repo> && git init -q && echo "# <name>" > README.md && git add -A && git commit -q -m "init"
```

3. 后台启动 planner（用 nohup + & 后台运行，不要等待）：
```bash
OKEY="sk-5Ds6eFbTEE1zu5fQ14F4FfB5892b419dB1BfC7292147B9Ef"
OBASE="http://152.53.52.170:3003/v1"

nohup python3 ~/.openclaw/workspace/skills/worktree-codex/scripts/planner.py \
  --desc "<desc>" \
  --repo <repo> \
  --agents <n> \
  --tech "<tech>" \
  --openai-api-key "$OKEY" \
  --openai-base-url "$OBASE" \
  --model gpt-4.1-mini \
  --codex-model gpt-5.3-codex \
  --codex-bin ~/.npm-global/bin/codex \
  --yes \
  > /tmp/planner-<name>.log 2>&1 &
```

4. 告知用户：任务已后台启动，展板 http://localhost:7789，完成后会自动通知。

5. **不要等待、不要轮询**。conductor 完成后会通过 notify.sh 自动把结果塞回本会话。

## 结果回报

conductor 在以下时机自动推消息到主会话：
- ✅ 全部完成：`✅ [worktree-codex] 项目 xxx 完成！N/N 通过，耗时 Xs`
- ⚠️ 需要决策：`⚠️ [worktree-codex 需要指示] agent-xxx 已追问 3 轮...`
  → 用户回复自然语言，在展板输入框输入即可
- ❌ 有失败：`❌ [worktree-codex] ...`

## 环境变量（从 TOOLS.md 获取）

- `OPENAI_API_KEY`：`sk-5Ds6e...`
- `OPENAI_BASE_URL`：`http://152.53.52.170:3003/v1`
- `CODEX_BIN`：`~/.npm-global/bin/codex`

## 参数速查

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--agents` | 3 | Agent 数量（2-6） |
| `--tech` | ES module 原生 JS | 技术栈约束 |
| `--model` | gpt-4.1-mini | Planner LLM |
| `--codex-model` | gpt-5.3-codex | Agent 执行模型 |
| `--max-slots` | 同 agents 数 | 并发 slot 数 |
| `--dry-run` | - | 只生成文件不执行 |
