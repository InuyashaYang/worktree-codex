#!/usr/bin/env bash
# worktree-codex/scripts/launch.sh — 一键启动展板
#
# 用法:
#   launch.sh [选项]
#   launch.sh --find          查找当前运行的展板实例并打印 URL
#   launch.sh --stop          停止当前运行的展板实例
#
# 选项:
#   --port <PORT>           展板端口，默认 7789
#   --logs <glob/path>...   初始 log 文件（可 glob，可多个），后续 agent 自动注册
#   --llm-base-url <URL>    AI 分析 base URL，默认 http://YOUR_PROXY_HOST:PORT/v1（自建代理）
#   --llm-api-key <KEY>     AI 分析 API Key，默认从环境变量 OPENAI_API_KEY 读取
#   --llm-model <MODEL>     AI 分析模型，默认 gemini-2.5-flash；多模态用 qwen3-30b-vl
#   --no-ai                 禁用 AI 分析
#   --bg                    后台运行（不阻塞终端）
#   --open                  启动后自动打开浏览器
#
# 启动后 URL 写入 /tmp/wt-dashboard.url（固定路径，无论用哪个端口）
# 任何时候 cat /tmp/wt-dashboard.url 即可找到展板地址
#
# 示例（默认配置，直接用自建代理 gemini-2.5-flash）:
#   launch.sh --bg --open
#
# 示例（多模态分析任务）:
#   launch.sh --llm-model qwen3-30b-vl --bg --open
#
# 示例（用 OpenRouter）:
#   launch.sh \
#     --llm-base-url https://openrouter.ai/api/v1 \
#     --llm-api-key sk-or-xxx \
#     --llm-model stepfun/step-3.5-flash:free \
#     --bg --open
#
# 示例（禁用 AI 分析，纯展板）:
#   launch.sh --no-ai --bg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_PY="$SCRIPT_DIR/../dashboard.py"
URL_FILE="/tmp/wt-dashboard.url"   # 固定路径，任何时候 cat 都能找到

PORT=7789
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
    # 验证进程还活着
    PID_FILE="/tmp/wt-dashboard.pid"
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
  PID_FILE="/tmp/wt-dashboard.pid"
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
    --port)        PORT="$2"; shift 2 ;;
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

# ── 组装 python 命令 ────────────────────────────────
CMD=(python3 "$DASHBOARD_PY" --port "$PORT")

[[ ${#LOGS[@]} -gt 0 ]] && CMD+=(--logs "${LOGS[@]}")
[[ -n "$LLM_BASE_URL" ]] && CMD+=(--llm-base-url "$LLM_BASE_URL")
[[ -n "$LLM_API_KEY"  ]] && CMD+=(--llm-api-key  "$LLM_API_KEY")
[[ -n "$LLM_MODEL"    ]] && CMD+=(--llm-model     "$LLM_MODEL")
[[ $NO_AI -eq 1       ]] && CMD+=(--no-ai)

DASHBOARD_URL="http://localhost:$PORT"
echo "[launch] 展板地址: $DASHBOARD_URL"
echo "$DASHBOARD_URL" > "$URL_FILE"   # 写入固定路径

# ── 启动 ────────────────────────────────────────────
if [[ $OPEN_BROWSER -eq 1 ]]; then
  (sleep 1 && cmd.exe /c start "$DASHBOARD_URL" 2>/dev/null || \
              xdg-open "$DASHBOARD_URL" 2>/dev/null || true) &
fi

PID_FILE="/tmp/wt-dashboard.pid"

if [[ $BG -eq 1 ]]; then
  # 若已有实例在跑，先杀掉
  if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    kill "$OLD_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    sleep 0.3
  fi
  nohup "${CMD[@]}" > "/tmp/wt-dashboard-$PORT.log" 2>&1 &
  NEW_PID=$!
  echo $NEW_PID > "$PID_FILE"
  echo ""
  echo "╔══════════════════════════════════════════════════════════╗"
  echo "║  展板已在后台启动"
  echo "║  URL  : $DASHBOARD_URL"
  echo "║  PORT : $PORT"
  echo "║  PID  : $NEW_PID"
  echo "║  LOG  : /tmp/wt-dashboard-$PORT.log"
  echo "║"
  echo "║  查找展板: launch.sh --find"
  echo "║  停止展板: launch.sh --stop"
  echo "╚══════════════════════════════════════════════════════════╝"
else
  # 前台运行：退出时清理 URL 文件
  trap "rm -f '$URL_FILE' '$PID_FILE'" EXIT
  echo $$ > "$PID_FILE"
  "${CMD[@]}"
fi
