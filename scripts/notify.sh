#!/usr/bin/env bash
# notify.sh — worktree-codex → OpenClaw 主会话消息推送
#
# 用法: notify.sh <event_type> <message>
# event_type: done | await_human | agent_fail | started
#
# 实现：通过 `openclaw cron add` 创建一个 5 秒后触发的 systemEvent cron job，
# 并设置 --wake now，让 heartbeat 立即消费。Job 在执行后自动删除。
#
# 环境变量（均可选，当前版本不需要）:
#   WT_GATEWAY_PORT  (保留，向后兼容)
#   WT_SESSION_KEY   (保留，向后兼容)

set -euo pipefail

EVENT="${1:-}"
MESSAGE="${2:-}"

if [[ -z "$EVENT" || -z "$MESSAGE" ]]; then
  echo "[notify] 用法: notify.sh <event> <message>" >&2
  exit 1
fi

# 格式化消息前缀
case "$EVENT" in
  done)        PREFIX="✅ [worktree-codex]" ;;
  await_human) PREFIX="⚠️ [worktree-codex 需要指示]" ;;
  agent_fail)  PREFIX="❌ [worktree-codex]" ;;
  started)     PREFIX="🚀 [worktree-codex]" ;;
  *)           PREFIX="[worktree-codex]" ;;
esac

FULL_MSG="${PREFIX} ${MESSAGE}"

# 找 openclaw 二进制
OPENCLAW_BIN=""
for _b in \
  "$(command -v openclaw 2>/dev/null || true)" \
  "$HOME/.npm-global/bin/openclaw" \
  "/usr/local/bin/openclaw" \
  "/usr/bin/openclaw"; do
  if [[ -n "$_b" && -x "$_b" ]]; then
    OPENCLAW_BIN="$_b"
    break
  fi
done

if [[ -z "$OPENCLAW_BIN" ]]; then
  echo "[notify] warn: openclaw binary not found, notification skipped" >&2
  exit 0
fi

# 生成 5 秒后的 ISO 时间戳（兼容 GNU date / macOS date）
TRIGGER_TS=$(date -u -d "+5 seconds" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
  || date -u -v+5S +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
  || python3 -c "
import datetime
t = datetime.datetime.utcnow() + datetime.timedelta(seconds=5)
print(t.strftime('%Y-%m-%dT%H:%M:%SZ'))
")

# 创建 systemEvent cron job，wake now，执行后自动删除
"$OPENCLAW_BIN" cron add \
  --name "wt-notify-${EVENT}" \
  --at "$TRIGGER_TS" \
  --session main \
  --system-event "$FULL_MSG" \
  --wake now \
  --delete-after-run \
  >/dev/null 2>&1 || true

echo "[notify] sent: $EVENT → openclaw cron systemEvent"
