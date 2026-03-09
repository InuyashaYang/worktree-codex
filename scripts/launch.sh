#!/usr/bin/env bash
# worktree-codex/scripts/launch.sh — 一键启动展板
#
# 用法:
#   launch.sh [选项]
#   launch.sh --find          查找当前运行的展板实例并打印 URL
#   launch.sh --stop          停止当前运行的展板实例
#
# 选项:
#   --port <PORT>           期望端口，默认 7789（固定常驻端口）
#                           端口占用策略：
#                             - 被自家展板（dashboard.py）占用 → kill 旧实例，原地重启
#                             - 被其他进程占用 → 自动顺延找下一个空闲端口
#   --logs <glob/path>...   初始 log 文件（可 glob，可多个），后续 agent 自动注册
#   --llm-base-url <URL>    AI 分析 base URL
#   --llm-api-key <KEY>     AI 分析 API Key
#   --llm-model <MODEL>     AI 分析模型，默认 gpt-4.1-mini
#   --no-ai                 禁用 AI 分析
#   --bg                    后台运行（不阻塞终端）
#   --open                  启动后自动打开浏览器
#
# 启动后 URL 写入 /tmp/wt-dashboard.url（固定路径，无论实际端口）
# 任何时候 cat /tmp/wt-dashboard.url 即可找到展板地址
#
# 示例（默认配置）:
#   launch.sh --bg --open
#
# 示例（禁用 AI 分析，纯展板）:
#   launch.sh --no-ai --bg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_PY="$SCRIPT_DIR/../dashboard.py"
URL_FILE="/tmp/wt-dashboard.url"
PID_FILE="/tmp/wt-dashboard.pid"

PREFERRED_PORT=7789
LOGS=()
LLM_BASE_URL=""
LLM_API_KEY=""
LLM_MODEL=""
NO_AI=0
BG=0
OPEN_BROWSER=0

# ── --find / --stop 快捷命令 ────────────────────────
if [[ "${1:-}" == "--find" ]]; then
  if [[ -f "$URL_FILE" ]]; then
    URL=$(cat "$URL_FILE")
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "✅ 展板运行中: $URL  (PID=$(cat $PID_FILE))"
    else
      echo "⚠  URL 文件存在但进程已死: $URL"
      echo "   重新启动: launch.sh --bg --open"
    fi
  else
    echo "❌ 没有找到运行中的展板（$URL_FILE 不存在）"
    echo "   启动: launch.sh --bg --open"
  fi
  exit 0
fi

if [[ "${1:-}" == "--stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null && echo "✅ 已停止 PID=$PID" || echo "⚠ 进程已不存在"
    rm -f "$PID_FILE" "$URL_FILE"
  else
    pkill -f "dashboard.py" 2>/dev/null && echo "✅ 已停止" || echo "❌ 没有找到运行中的展板"
    rm -f "$URL_FILE"
  fi
  exit 0
fi

# ── 解析参数 ────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)        PREFERRED_PORT="$2"; shift 2 ;;
    --logs)
      shift
      while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do
        LOGS+=("$1"); shift
      done
      ;;
    --llm-base-url) LLM_BASE_URL="$2"; shift 2 ;;
    --llm-api-key)  LLM_API_KEY="$2";  shift 2 ;;
    --llm-model)    LLM_MODEL="$2";    shift 2 ;;
    --no-ai)        NO_AI=1;           shift   ;;
    --bg)           BG=1;              shift   ;;
    --open)         OPEN_BROWSER=1;    shift   ;;
    --help|-h)
      grep '^#' "$0" | head -40 | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

# ── 端口占用策略 ─────────────────────────────────────────────────────
# 1. 若 PID_FILE 存在且进程活着 → 直接 kill（是自家展板，占用合法，原地重启）
# 2. 若 PREFERRED_PORT 被非自家进程占用 → 顺延找下一个空闲端口（最多尝试 20 个）
# 3. 端口空闲 → 直接用

resolve_port() {
  local want="$PREFERRED_PORT"

  # 先干掉已有的自家展板实例（无论端口是否一致）
  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid=$(cat "$PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then
      echo "[launch] 停止旧展板实例 PID=$old_pid" >&2
      kill "$old_pid" 2>/dev/null || true
      sleep 0.4
    fi
    rm -f "$PID_FILE"
  fi

  # 检查期望端口是否空闲
  local port="$want"
  local tried=0
  while [[ $tried -lt 20 ]]; do
    if ! ss -tlnp 2>/dev/null | grep -q ":${port} " && \
       ! lsof -i ":${port}" -sTCP:LISTEN -t >/dev/null 2>&1; then
      echo "$port"
      return 0
    fi

    # 端口被占，判断是否是 dashboard.py
    local occupier
    occupier=$(lsof -i ":${port}" -sTCP:LISTEN -c python3 -t 2>/dev/null || true)
    if [[ -n "$occupier" ]]; then
      local cmd_check
      cmd_check=$(ps -p "$occupier" -o args= 2>/dev/null || true)
      if echo "$cmd_check" | grep -q "dashboard.py"; then
        echo "[launch] 端口 $port 被旧展板占用 (PID=$occupier)，正在停止..." >&2
        kill "$occupier" 2>/dev/null || true
        sleep 0.5
        echo "$port"
        return 0
      fi
    fi

    echo "[launch] 端口 $port 被其他进程占用，尝试 $((port+1))..." >&2
    port=$((port + 1))
    tried=$((tried + 1))
  done

  echo "$port"
}

PORT=$(resolve_port)

# ── 组装 python 命令 ────────────────────────────────
CMD=(python3 "$DASHBOARD_PY" --port "$PORT")

[[ ${#LOGS[@]} -gt 0 ]] && CMD+=(--logs "${LOGS[@]}")
[[ -n "$LLM_BASE_URL" ]] && CMD+=(--llm-base-url "$LLM_BASE_URL")
[[ -n "$LLM_API_KEY"  ]] && CMD+=(--llm-api-key  "$LLM_API_KEY")
[[ -n "$LLM_MODEL"    ]] && CMD+=(--llm-model     "$LLM_MODEL")
[[ $NO_AI -eq 1       ]] && CMD+=(--no-ai)

DASHBOARD_URL="http://localhost:$PORT"
echo "$DASHBOARD_URL" > "$URL_FILE"

# ── 自动打开浏览器 ───────────────────────────────────
if [[ $OPEN_BROWSER -eq 1 ]]; then
  (sleep 1 && cmd.exe /c start "$DASHBOARD_URL" 2>/dev/null || \
              xdg-open "$DASHBOARD_URL" 2>/dev/null || true) &
fi

# ── 启动 ─────────────────────────────────────────────
if [[ $BG -eq 1 ]]; then
  nohup "${CMD[@]}" > "/tmp/wt-dashboard-${PORT}.log" 2>&1 &
  NEW_PID=$!
  echo $NEW_PID > "$PID_FILE"
  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║  展板已在后台启动"
  echo "║  URL  : $DASHBOARD_URL"
  echo "║  PORT : $PORT"
  if [[ "$PORT" != "$PREFERRED_PORT" ]]; then
  echo "║  ⚠ 期望端口 $PREFERRED_PORT 被占用，已改用 $PORT"
  fi
  echo "║  PID  : $NEW_PID"
  echo "║  LOG  : /tmp/wt-dashboard-${PORT}.log"
  echo "║"
  echo "║  查找展板: launch.sh --find"
  echo "║  停止展板: launch.sh --stop"
  echo "╚══════════════════════════════════════════════════════════╝"
else
  trap "rm -f '$URL_FILE' '$PID_FILE'" EXIT
  echo $$ > "$PID_FILE"
  echo "[launch] 展板地址: $DASHBOARD_URL (前台运行，Ctrl+C 退出)"
  "${CMD[@]}"
fi
