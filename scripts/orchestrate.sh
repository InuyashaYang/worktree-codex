#!/usr/bin/env bash
# worktree-codex/scripts/orchestrate.sh
# OpenClaw 多 Codex Worktree 编排脚本
#
# 用法:
#   orchestrate.sh <repo_dir> <agent_name> <worktree_path> <branch> <task_prompt> <log_file>
#
# 环境变量（调用方传入或 export）:
#   OPENAI_API_KEY    — API Key（必填，从 ~/.profile 继承或显式传入）
#   OPENAI_BASE_URL   — 默认 http://YOUR_PROXY_HOST:PORT/v1（自建代理，支持 /v1/responses）
#   CODEX_MODEL       — 默认 gpt-5.3-codex
#   CODEX_BIN         — 默认 ~/.npm-global/bin/codex
#   CODEX_MAX_RETRIES — 最多重试次数，默认 2（共 3 次机会）
#   CODEX_RETRY_BASE  — 退避基数（秒），默认 10
#   DASHBOARD_PORT    — 展板端口，默认 7789

set -euo pipefail

REPO_DIR="$1"
AGENT_NAME="$2"
WORKTREE_PATH="$3"
BRANCH="$4"
TASK_PROMPT="$5"
LOG_FILE="$6"

export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://YOUR_PROXY_HOST:PORT/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:?需要设置 OPENAI_API_KEY（或 source ~/.profile）}"

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
CODEX_MODEL="${CODEX_MODEL:-gpt-5.3-codex}"
CODEX_BIN="${CODEX_BIN:-$HOME/.npm-global/bin/codex}"
DASHBOARD_PORT="${DASHBOARD_PORT:-7789}"

# ── 辅助：写结构化中间态标记到 log ──────────────────────────────────
# 格式：##TAG## [HH:MM:SS] 内容
# 展板和离线分析工具可按 ##TAG## 提取
emit() {
  local tag="$1"; shift
  echo "##${tag}## [$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ── 任务快照（开始时写入，记录意图和约束） ───────────────────────────
emit "TASK_START"  "agent=$AGENT_NAME branch=$BRANCH"
emit "TASK_MODEL"  "model=$CODEX_MODEL base=$OPENAI_BASE_URL"
emit "TASK_GOAL"   "$(echo "$TASK_PROMPT" | head -c 300)"  # prompt 前300字
emit "TASK_CTX"    "worktree=$WORKTREE_PATH repo=$REPO_DIR"

# worktree 当前文件快照（让后来者知道 Agent 启动时的代码状态）
emit "TASK_FILES"  "$(cd "$WORKTREE_PATH" && git ls-files | head -20 | tr '\n' ' ')"

# ── 向展板注册（静默失败，展板是可选组件） ───────────────────────────
curl -s --max-time 1 -X POST "http://localhost:${DASHBOARD_PORT}/register" \
  -H "Content-Type: application/json" \
  -d "{\"log\":\"$LOG_FILE\"}" > /dev/null 2>&1 || true

cd "$WORKTREE_PATH"

# ── 构造注入了可解释性协议的完整 prompt ──────────────────────────────
#
# 协议说明（写给 Codex 的）：
#   ##INTENT##   — 开始每个主要步骤前，一句话说明意图
#   ##OBSTACLE## — 遇到意外时，一句话描述问题
#   ##DECISION## — 在多个方案中选择时，一句话说明选了什么、为什么
#   ##RESULT##   — 最终完成时，总结改了什么、核心逻辑是什么
#
# 格式要求：标记放在独立行，##TAG## 后面跟内容，不要加时间戳（脚本会加）
#
INSTRUMENTED_PROMPT="${TASK_PROMPT}

---
**可解释性协议（必须遵守）：**
在执行过程中，在关键节点输出以下格式的标记行（独立成行，不要省略）：

  ##INTENT## 我接下来要做什么，以及为什么
  ##OBSTACLE## 遇到了什么预期外的问题
  ##DECISION## 我选择了方案X而不是方案Y，原因是Z
  ##RESULT## 最终完成了什么，核心改动是什么（一句话）

示例：
  ##INTENT## 先读取现有 API 接口定义，确认参数格式
  ##DECISION## 选择在原函数上扩展而不是新建，因为调用方已有依赖
  ##RESULT## 在 src/api.py 新增 batch_process()，复用了现有 validate() 逻辑

要求：每个主要阶段至少一个标记，遇到问题必须输出 ##OBSTACLE##，完成时必须输出 ##RESULT##。
完成后执行：git add -A && git commit -m \"[${AGENT_NAME}] task complete\"
"

# ── Codex 执行（指数退避重试） ───────────────────────────────────────
CODEX_MAX_RETRIES="${CODEX_MAX_RETRIES:-2}"
CODEX_RETRY_BASE="${CODEX_RETRY_BASE:-10}"
EXIT_CODE=0

for attempt in $(seq 1 $((CODEX_MAX_RETRIES + 1))); do
  if [ "$attempt" -gt 1 ]; then
    WAIT=$(( CODEX_RETRY_BASE * (2 ** (attempt - 2)) ))
    emit "RETRY" "attempt=$attempt reason=exit_code_${EXIT_CODE} wait=${WAIT}s"
    sleep "$WAIT"
  fi

  "$CODEX_BIN" exec \
    --model "$CODEX_MODEL" \
    --full-auto \
    --skip-git-repo-check \
    "$INSTRUMENTED_PROMPT" \
    >> "$LOG_FILE" 2>&1
  EXIT_CODE=$?

  emit "CODEX_EXIT" "code=$EXIT_CODE attempt=$attempt"

  if [ "$EXIT_CODE" -eq 0 ]; then
    break
  elif [ "$EXIT_CODE" -eq 1 ] && grep -q "AGENT_DONE\|##RESULT##\|no changes" "$LOG_FILE" 2>/dev/null; then
    EXIT_CODE=0; break
  elif [ "$attempt" -le "$CODEX_MAX_RETRIES" ]; then
    emit "RETRY_PLAN" "will_retry=true attempts_left=$(( CODEX_MAX_RETRIES - attempt + 1 ))"
  else
    emit "TASK_FAILED" "exit_code=$EXIT_CODE all_attempts_exhausted=true"
  fi
done

# ── 结果快照（完成时写入，记录实际产出） ─────────────────────────────
DIFF_STAT=$(git diff HEAD~1 --stat 2>/dev/null | tail -1 || echo "no diff")
CHANGED_FILES=$(git diff HEAD~1 --name-only 2>/dev/null | tr '\n' ' ' || echo "")

emit "OUTCOME_DIFF"  "$DIFF_STAT"
emit "OUTCOME_FILES" "changed: $CHANGED_FILES"
emit "OUTCOME_EXIT"  "final_exit_code=$EXIT_CODE"

# ── 自动 commit（Codex 未提交时兜底） ────────────────────────────────
PENDING=$(git status --porcelain)
if [ -n "$PENDING" ]; then
  git add -A
  git commit -m "[${AGENT_NAME}] task complete (auto-commit)" --no-verify 2>&1 | tee -a "$LOG_FILE"
  emit "AUTO_COMMIT" "reason=codex_did_not_commit files=$(echo "$PENDING" | wc -l)"
fi

emit "AGENT_DONE" "agent=$AGENT_NAME exit_code=$EXIT_CODE"
echo "AGENT_DONE:$AGENT_NAME" | tee -a "$LOG_FILE"
