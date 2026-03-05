#!/usr/bin/env bash
# worktree-codex/scripts/setup_worktrees.sh
# 为每个 Agent 创建 git worktree 并返回路径
#
# 用法: setup_worktrees.sh <repo_dir> <worktrees_base_dir> <agent_names...>
# 输出: 每行一个 "agent_name:worktree_path:branch_name"
# SETUP_LOG（可选环境变量）: 写入结构化标记的日志文件

set -euo pipefail

REPO_DIR="$1"
WORKTREES_BASE="$2"
shift 2
AGENTS=("$@")

SETUP_LOG="${SETUP_LOG:-/tmp/worktree-setup.log}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# ── 结构化日志辅助 ──────────────────────────────────────────────────
slog() {
  local tag="$1"; shift
  echo "##${tag}## [$(date '+%H:%M:%S')] $*" | tee -a "$SETUP_LOG" >&2
}

slog "SETUP_START" "repo=$REPO_DIR agents=${AGENTS[*]} ts=$TIMESTAMP"

cd "$REPO_DIR"
mkdir -p "$WORKTREES_BASE"

ORIGINAL_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || git rev-parse --short HEAD)
slog "SETUP_BASE_BRANCH" "branch=$ORIGINAL_BRANCH"

# HEAD commit 快照（让后来者知道任务从哪个代码版本出发）
HEAD_SHA=$(git rev-parse --short HEAD)
HEAD_MSG=$(git log -1 --pretty=format:"%s" HEAD)
slog "SETUP_HEAD_SNAPSHOT" "sha=$HEAD_SHA msg=$HEAD_MSG"

for AGENT in "${AGENTS[@]}"; do
  RAND=$(tr -dc 'a-z0-9' < /dev/urandom | head -c4 || true)
  BRANCH="feature/${AGENT}-${TIMESTAMP}-${RAND}"
  WORKTREE_PATH="$WORKTREES_BASE/$AGENT"

  slog "SETUP_WORKTREE_BEGIN" "agent=$AGENT branch=$BRANCH path=$WORKTREE_PATH"

  # 清理已存在的 worktree
  if git worktree list | grep -q "$WORKTREE_PATH"; then
    slog "SETUP_WORKTREE_CLEANUP" "removing existing worktree at $WORKTREE_PATH"
    git worktree remove --force "$WORKTREE_PATH" 2>/dev/null || true
  fi
  rm -rf "$WORKTREE_PATH"

  git branch "$BRANCH" "$ORIGINAL_BRANCH"
  git worktree add "$WORKTREE_PATH" "$BRANCH"

  slog "SETUP_WORKTREE_DONE" "agent=$AGENT branch=$BRANCH path=$WORKTREE_PATH"

  echo "$AGENT:$WORKTREE_PATH:$BRANCH"
done

slog "SETUP_ALL_DONE" "total=${#AGENTS[@]} agents"
