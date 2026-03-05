#!/usr/bin/env bash
# worktree-codex/scripts/launch.sh — 一键启动展板
#
# 用法:
#   launch.sh [选项]
#
# 选项:
#   --port <PORT>           展板端口，默认 7789
#   --logs <glob/path>...   初始 log 文件（可 glob，可多个），后续 agent 自动注册
#   --llm-base-url <URL>    AI 分析 base URL，默认 http://YOUR_PROXY_HOST:PORT/v1（自建代理）
#   --llm-api-key <KEY>     AI 分析 API Key，默认从环境变量 OPENAI_API_KEY 读取
#   --llm-model <MODEL>     AI 分析模型，默认 step-3.5-flash；多模态用 qwen3-30b-vl
#   --no-ai                 禁用 AI 分析
#   --bg                    后台运行（不阻塞终端）
#   --open                  启动后自动打开浏览器
#
# 示例（默认配置，直接用自建代理 step-3.5-flash）:
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

PORT=7789
LOGS=()
LLM_BASE_URL=""
LLM_API_KEY=""
LLM_MODEL=""
NO_AI=0
BG=0
OPEN_BROWSER=0

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

echo "[launch] 展板地址: http://localhost:$PORT"
echo "[launch] 命令: ${CMD[*]}"

# ── 启动 ────────────────────────────────────────────
if [[ $OPEN_BROWSER -eq 1 ]]; then
  # 延迟1秒再开浏览器，等端口就绪
  (sleep 1 && cmd.exe /c start "http://localhost:$PORT" 2>/dev/null || \
              xdg-open "http://localhost:$PORT" 2>/dev/null || true) &
fi

if [[ $BG -eq 1 ]]; then
  PID_FILE="/tmp/wt-dashboard-$PORT.pid"
  # 若已有实例在跑，先杀掉
  if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    kill "$OLD_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    sleep 0.3
  fi
  nohup "${CMD[@]}" > "/tmp/wt-dashboard-$PORT.log" 2>&1 &
  echo $! > "$PID_FILE"
  echo "[launch] 后台运行 PID=$(cat $PID_FILE)，日志: /tmp/wt-dashboard-$PORT.log"
else
  "${CMD[@]}"
fi
