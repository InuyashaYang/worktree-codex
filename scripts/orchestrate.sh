#!/usr/bin/env bash
# worktree-codex/scripts/orchestrate.sh
# OpenClaw 多 Agent Worktree 编排脚本（Codex 多轮对话版）
#
# 用法:
#   orchestrate.sh <repo_dir> <agent_name> <worktree_path> <branch> <task_prompt> <log_file>
#
# 环境变量（调用方传入或 export）:
#   OPENAI_API_KEY    — API Key（必填，从 ~/.profile 继承或显式传入）
#   OPENAI_BASE_URL   — 默认 http://152.53.52.170:3003/v1
#   CODEX_MODEL       — 默认 gpt-5.3-codex
#   CODEX_BIN         — 默认 ~/.npm-global/bin/codex
#   AGENT_MAX_TURNS   — 最大追问轮数，默认 3
#   DASHBOARD_PORT    — 展板端口，默认 7789

set -euo pipefail

REPO_DIR="$1"
AGENT_NAME="$2"
WORKTREE_PATH="$3"
BRANCH="$4"
TASK_PROMPT="$5"
LOG_FILE="$6"

export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://152.53.52.170:3003/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:?需要设置 OPENAI_API_KEY（或 source ~/.profile）}"

export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

CODEX_MODEL="${CODEX_MODEL:-gpt-5.3-codex}"
CODEX_BIN="${CODEX_BIN:-$HOME/.npm-global/bin/codex}"
AGENT_MAX_TURNS="${AGENT_MAX_TURNS:-3}"
DASHBOARD_PORT="${DASHBOARD_PORT:-7789}"

# 临时文件：存 codex 上一轮最后输出（用于判断是否需要追问）
LAST_MSG_FILE="/tmp/codex-last-msg-${AGENT_NAME}.txt"
# Session ID — 第一轮结束后从 session 文件名提取，后续轮 resume 用
SESSION_ID="__last__"

# ── 辅助：写结构化标记到 log ─────────────────────────────────────────
emit() {
  local tag="$1"; shift
  echo "##${tag}## [$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ── 控制端口 / 展板信息 ──────────────────────────────────────────────
DASHBOARD_URL_FILE="/tmp/wt-dashboard.url"
DASHBOARD_URL=""
if [ -f "$DASHBOARD_URL_FILE" ]; then
  DASHBOARD_URL=$(cat "$DASHBOARD_URL_FILE")
fi

{
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Worktree-Codex Agent: $AGENT_NAME"
echo "║  Log 文件  : $LOG_FILE"
if [ -n "$DASHBOARD_URL" ]; then
  echo "║  展板地址  : $DASHBOARD_URL"
else
  echo "║  展板地址  : 未启动（运行 launch.sh --bg --open 可启动）"
fi
echo "║  Dashboard Port : $DASHBOARD_PORT"
echo "╚══════════════════════════════════════════════════════════╝"
} | tee -a "$LOG_FILE"

# ── 任务快照 ─────────────────────────────────────────────────────────
emit "TASK_START"  "agent=$AGENT_NAME branch=$BRANCH"
emit "TASK_MODEL"  "model=$CODEX_MODEL session=$SESSION_ID"
emit "TASK_GOAL"   "$(echo "$TASK_PROMPT" | head -c 300)"
emit "TASK_CTX"    "worktree=$WORKTREE_PATH repo=$REPO_DIR"
emit "TASK_FILES"  "$(cd "$WORKTREE_PATH" && git ls-files | head -20 | tr '\n' ' ')"

# ── 向展板注册 ────────────────────────────────────────────────────────
curl -s --max-time 1 -X POST "http://localhost:${DASHBOARD_PORT}/register" \
  -H "Content-Type: application/json" \
  -d "{\"log\":\"$LOG_FILE\"}" > /dev/null 2>&1 || true

cd "$WORKTREE_PATH"

# ── 构造初始 Prompt ───────────────────────────────────────────────────
INSTRUMENTED_PROMPT="${TASK_PROMPT}

---
**可解释性协议（必须遵守）：**
在执行过程中，在关键节点输出以下格式的标记行（独立成行）：

  ##INTENT## 我接下来要做什么，以及为什么
  ##OBSTACLE## 遇到了什么预期外的问题
  ##DECISION## 我选择了方案X而不是方案Y，原因是Z
  ##RESULT## 最终完成了什么，核心改动是什么（一句话）

要求：每个主要阶段至少一个 ##INTENT##，遇到问题必须输出 ##OBSTACLE##，完成时必须输出 ##RESULT##。
完成后执行：git add -A && git commit -m \"[${AGENT_NAME}] task complete\"
"

# ── 多轮 Codex 执行 ───────────────────────────────────────────────────
# turn 1: codex exec --session-id <id> <prompt>   → 建立 session
# turn N: codex exec resume <session-id> <prompt>  → 复用上下文追问
#
EXIT_CODE=0
TURN=0

run_turn() {
  local turn="$1"
  local prompt="$2"
  local exit_code=0

  emit "TURN_START" "turn=$turn"

  if [ "$turn" -eq 1 ]; then
    # 第一轮：新建 session，--json 模式可以拿到 session_id 事件
    "$CODEX_BIN" exec \
      --model "$CODEX_MODEL" \
      --dangerously-bypass-approvals-and-sandbox \
      --skip-git-repo-check \
      --output-last-message "$LAST_MSG_FILE" \
      "$prompt" \
      >> "$LOG_FILE" 2>&1 || exit_code=$?

    # 从最新 session 文件名提取 UUID（第一轮跑完后的最新文件）
    SESSION_FILE=$(ls -t ~/.codex/sessions/$(date +%Y/%m/%d)/rollout-*.jsonl 2>/dev/null | head -1)
    if [ -n "$SESSION_FILE" ]; then
      SESSION_ID=$(basename "$SESSION_FILE" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
      emit "SESSION_ID" "id=$SESSION_ID file=$SESSION_FILE"
    else
      SESSION_ID="__last__"
      emit "SESSION_ID" "fallback=--last"
    fi
  else
    # 后续轮：resume 同一 session
    if [ "$SESSION_ID" = "__last__" ]; then
      RESUME_ARG="--last"
    else
      RESUME_ARG="$SESSION_ID"
    fi
    "$CODEX_BIN" exec resume \
      $RESUME_ARG \
      --model "$CODEX_MODEL" \
      --dangerously-bypass-approvals-and-sandbox \
      --skip-git-repo-check \
      --output-last-message "$LAST_MSG_FILE" \
      "$prompt" \
      >> "$LOG_FILE" 2>&1 || exit_code=$?
  fi

  emit "TURN_DONE" "turn=$turn exit_code=$exit_code"
  return $exit_code
}

# 第 1 轮：主任务
TURN=1
run_turn 1 "$INSTRUMENTED_PROMPT" || EXIT_CODE=$?
emit "CODEX_EXIT" "code=$EXIT_CODE turn=1"

# ── Conductor 注入文件路径 ────────────────────────────────────────────
# conductor.sh 会把追问内容写到这个文件；orchestrate.sh 每轮检查并消费
INJECT_FILE="/tmp/wt-inject-${AGENT_NAME}.txt"

# ── 后续轮：根据上一轮输出判断是否追问，同时监听 conductor 注入 ────────
while [ "$TURN" -lt "$AGENT_MAX_TURNS" ]; do
  # ── 检查 conductor 注入（优先级最高）────────────────────────────────
  # 文件存在且非空 → 读取内容，清除文件，作为本轮追问
  if [ -f "$INJECT_FILE" ] && [ -s "$INJECT_FILE" ]; then
    INJECT_CONTENT=$(cat "$INJECT_FILE")
    # 清除注入文件，防止重复消费
    : > "$INJECT_FILE"

    TURN=$((TURN + 1))
    emit "INJECT_TURN" "turn=$TURN source=conductor inject_len=$(echo "$INJECT_CONTENT" | wc -c)"
    run_turn "$TURN" "$INJECT_CONTENT" || EXIT_CODE=$?
    emit "CODEX_EXIT" "code=$EXIT_CODE turn=$TURN source=inject"
    # 注入轮完成后回到循环顶部，继续检查是否还有注入或是否满足完成条件
    continue
  fi

  HAS_RESULT=$(grep -c "##RESULT##" "$LOG_FILE" 2>/dev/null || echo 0)
  HAS_OBSTACLE=$(grep -c "##OBSTACLE##" "$LOG_FILE" 2>/dev/null || echo 0)
  HAS_COMMIT=$(grep -c "task complete\|\[${AGENT_NAME}\]" "$LOG_FILE" 2>/dev/null || echo 0)

  # 已有结果且已 commit → 停止
  if [ "$HAS_RESULT" -gt 0 ] && [ "$HAS_COMMIT" -gt 0 ]; then
    emit "MULTI_TURN" "stopping reason=result_and_commit_found turn=$TURN"
    break
  fi

  TURN=$((TURN + 1))

  # 读取上一轮最后输出，拼进追问
  LAST_OUTPUT=""
  if [ -f "$LAST_MSG_FILE" ]; then
    LAST_OUTPUT=$(cat "$LAST_MSG_FILE" | head -c 800)
  fi

  if [ "$HAS_OBSTACLE" -gt 0 ] && [ "$HAS_RESULT" -eq 0 ]; then
    FOLLOWUP="你在上一轮遇到了障碍（##OBSTACLE##）。上一轮的结尾输出：

${LAST_OUTPUT}

请继续解决这个问题。如果原方案走不通就换方案，最终完成全部任务，输出 ##RESULT##，并执行 git add -A && git commit -m \"[${AGENT_NAME}] task complete\"。"
  else
    FOLLOWUP="请继续并完成任务收尾。上一轮的结尾输出：

${LAST_OUTPUT}

确认所有改动已实现。如还有未完成的部分，现在完成它。然后输出 ##RESULT##，执行 git add -A && git commit -m \"[${AGENT_NAME}] task complete\"。"
  fi

  emit "MULTI_TURN" "turn=$TURN obstacle=$HAS_OBSTACLE result_so_far=$HAS_RESULT"
  run_turn "$TURN" "$FOLLOWUP" || EXIT_CODE=$?
  emit "CODEX_EXIT" "code=$EXIT_CODE turn=$TURN"
done

# ── 清理注入文件 ──────────────────────────────────────────────────────
rm -f "$INJECT_FILE"

# ── 结果快照 ─────────────────────────────────────────────────────────
DIFF_STAT=$(git diff HEAD~1 --stat 2>/dev/null | tail -1 || echo "no diff")
CHANGED_FILES=$(git diff HEAD~1 --name-only 2>/dev/null | tr '\n' ' ' || echo "")

emit "OUTCOME_DIFF"  "$DIFF_STAT"
emit "OUTCOME_FILES" "changed: $CHANGED_FILES"
emit "OUTCOME_EXIT"  "final_exit_code=$EXIT_CODE"

# ── 自动 commit 兜底 ─────────────────────────────────────────────────
PENDING=$(git status --porcelain 2>/dev/null || echo "")
if [ -n "$PENDING" ]; then
  git add -A
  git commit -m "[${AGENT_NAME}] task complete (auto-commit)" --no-verify 2>&1 | tee -a "$LOG_FILE"
  emit "AUTO_COMMIT" "reason=codex_did_not_commit files=$(echo "$PENDING" | wc -l)"
fi

rm -f "$LAST_MSG_FILE"
emit "AGENT_DONE" "agent=$AGENT_NAME exit_code=$EXIT_CODE total_turns=$TURN"
echo "AGENT_DONE:$AGENT_NAME" | tee -a "$LOG_FILE"
