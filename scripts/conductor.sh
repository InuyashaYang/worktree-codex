#!/usr/bin/env bash
# worktree-codex/scripts/conductor.sh
# 向后兼容薄包装 — 把老式 --agents 参数转成 tasks.yaml 后调用 conductor.py
#
# 新式用法（直接透传给 conductor.py）:
#   conductor.sh --tasks /path/to/tasks.yaml [--max-slots 5] [--poll-interval 8]
#
# 旧式用法（兼容模式，自动生成临时 tasks.yaml）:
#   conductor.sh \
#     --repo <repo_dir> \
#     --contract <contract_file> \
#     --agents "name:worktree:branch:logfile ..." \
#     [--max-global-rounds 5] \
#     [--poll-interval 10] \
#     [--syntax-check "node --check"] \
#     [--dashboard-port 7789]
#
# 依赖: Python 3.8+，无第三方包；bash ≥ 4

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDUCTOR_PY="${SCRIPT_DIR}/conductor.py"

# ── 检测是否是新式调用（有 --tasks 参数）────────────────────────────
for arg in "$@"; do
  if [[ "$arg" == "--tasks" ]]; then
    # 新式：直接透传给 conductor.py
    exec python3 "$CONDUCTOR_PY" "$@"
  fi
done

# ── 旧式参数解析 ─────────────────────────────────────────────────────
REPO_DIR=""
CONTRACT_FILE=""
AGENTS_RAW=""
MAX_GLOBAL_ROUNDS=5
POLL_INTERVAL=10
SYNTAX_CHECK=""
DASHBOARD_PORT=7789
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-}"
CODEX_MODEL="${CODEX_MODEL:-gpt-5.3-codex}"
CODEX_BIN="${CODEX_BIN:-}"

usage() {
  echo "新式用法: $0 --tasks tasks.yaml [--max-slots 5] [--poll-interval 8] ..."
  echo ""
  echo "旧式兼容用法:"
  echo "  $0 --repo <dir> --contract <file> --agents \"name:wt:branch:log ...\" [选项]"
  echo ""
  echo "选项:"
  echo "  --repo <dir>             git 仓库根目录"
  echo "  --contract <file>        CONTRACT.md 文件路径"
  echo "  --agents \"...\"           Agent 描述（空格分隔，name:worktree:branch:logfile）"
  echo "  --max-global-rounds N    最大追问轮数（默认 5）"
  echo "  --poll-interval N        轮询间隔秒数（默认 10）"
  echo "  --syntax-check \"cmd\"     语法检查命令"
  echo "  --dashboard-port N       展板端口（默认 7789）"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)               REPO_DIR="$2";          shift 2 ;;
    --contract)           CONTRACT_FILE="$2";     shift 2 ;;
    --agents)             AGENTS_RAW="$2";        shift 2 ;;
    --max-global-rounds)  MAX_GLOBAL_ROUNDS="$2"; shift 2 ;;
    --poll-interval)      POLL_INTERVAL="$2";     shift 2 ;;
    --syntax-check)       SYNTAX_CHECK="$2";      shift 2 ;;
    --dashboard-port)     DASHBOARD_PORT="$2";    shift 2 ;;
    --openai-api-key)     OPENAI_API_KEY="$2";    shift 2 ;;
    --openai-base-url)    OPENAI_BASE_URL="$2";   shift 2 ;;
    --codex-model)        CODEX_MODEL="$2";       shift 2 ;;
    --codex-bin)          CODEX_BIN="$2";         shift 2 ;;
    --help|-h)            usage ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

[[ -z "$REPO_DIR" ]]      && echo "错误: --repo 必填（或使用 --tasks 新式调用）" && usage
[[ -z "$CONTRACT_FILE" ]] && echo "错误: --contract 必填" && usage
[[ -z "$AGENTS_RAW" ]]    && echo "错误: --agents 必填" && usage

# ── 把旧式 --agents 字符串转成临时 tasks.yaml ─────────────────────────
TASKS_TMP=$(mktemp /tmp/wt-conductor-tasks-XXXXXX.yaml)

{
  echo "# 自动生成（由 conductor.sh 旧式兼容层创建）"
  echo "max_slots: ${MAX_GLOBAL_ROUNDS}"
  echo "repo: ${REPO_DIR}"
  echo "contract: ${CONTRACT_FILE}"
  echo "syntax_check: \"${SYNTAX_CHECK}\""
  echo "dashboard_port: ${DASHBOARD_PORT}"
  echo "agents:"

  for entry in $AGENTS_RAW; do
    IFS=':' read -r aname awt abranch alog <<< "$entry"
    echo "  - name: ${aname}"
    echo "    worktree: ${awt}"
    echo "    branch: ${abranch}"
    echo "    log: ${alog}"
    echo "    files: []"
    echo "    depends: []"
    echo "    prompt: |"
    echo "      （旧式兼容模式：prompt 由 orchestrate.sh 外部控制）"
  done
} > "$TASKS_TMP"

echo "[conductor.sh] 生成临时 tasks.yaml: $TASKS_TMP"
echo "[conductor.sh] 调用 conductor.py..."

# 构造新式参数
EXTRA_ARGS=(
  "--max-slots" "${MAX_GLOBAL_ROUNDS}"
  "--poll-interval" "${POLL_INTERVAL}"
  "--dashboard-port" "${DASHBOARD_PORT}"
)
[[ -n "$OPENAI_API_KEY" ]]  && EXTRA_ARGS+=(  "--openai-api-key"  "$OPENAI_API_KEY"  )
[[ -n "$OPENAI_BASE_URL" ]] && EXTRA_ARGS+=( "--openai-base-url" "$OPENAI_BASE_URL" )
[[ -n "$CODEX_MODEL" ]]     && EXTRA_ARGS+=(   "--codex-model"    "$CODEX_MODEL"    )
[[ -n "$CODEX_BIN" ]]       && EXTRA_ARGS+=(   "--codex-bin"      "$CODEX_BIN"      )

# 删除临时文件前先执行（exec 替换进程，不再回来）
trap "rm -f '$TASKS_TMP'" EXIT

exec python3 "$CONDUCTOR_PY" --tasks "$TASKS_TMP" "${EXTRA_ARGS[@]}"
