#!/usr/bin/env bash
# worktree-codex/scripts/conductor.sh
# 主控轮询器 — 多 Agent 集成验证与追问注入
#
# 用法:
#   conductor.sh \
#     --repo <repo_dir> \
#     --contract <contract_file> \
#     --agents "name:worktree:branch:logfile [...]" \
#     [--max-global-rounds 5] \
#     [--poll-interval 10] \
#     [--syntax-check "node --check"] \
#     [--dashboard-port 7789]
#
# agents 参数格式（空格分隔，每项冒号分隔）：
#   "agent-board:/tmp/wt/board:feature/board:/tmp/wt-board.log agent-logic:/tmp/wt/logic:feature/logic:/tmp/wt-logic.log"
#
# 工作流程:
#   Phase 0 → 等待所有 Agent 首轮输出 AGENT_DONE（超时 30min）
#   Phase 1 → 对每个 agent 做语法检查 + CONTRACT 导出符号验证
#   Phase 2 → 对验证失败的 agent 写追问到 /tmp/wt-inject-<name>.txt
#   Phase 3 → 等待追问完成（再次等待各自 AGENT_DONE）→ 回到 Phase 1
#   Phase 4 → 输出最终报告 /tmp/wt-conductor-report.txt
#
# 依赖: bash ≥ 4，grep，awk，sed，标准 POSIX 工具
# 不依赖: jq, python, node

set -euo pipefail

# ── 全局默认值 ────────────────────────────────────────────────────────
REPO_DIR=""
CONTRACT_FILE=""
AGENTS_RAW=""          # "name:wt:branch:log name2:wt2:branch2:log2 ..."
MAX_GLOBAL_ROUNDS=5
POLL_INTERVAL=10
SYNTAX_CHECK=""        # 例如 "node --check" 或 "bash -n"，为空则跳过语法检查
DASHBOARD_PORT=7789

CONDUCTOR_LOG="/tmp/wt-conductor.log"
CONDUCTOR_REPORT="/tmp/wt-conductor-report.txt"
PHASE0_TIMEOUT=1800    # 30 分钟

# ── 参数解析 ─────────────────────────────────────────────────────────
usage() {
  echo "用法: $0 --repo <dir> --contract <file> --agents \"name:wt:branch:log ...\" [选项]"
  echo ""
  echo "必填:"
  echo "  --repo <dir>             git 仓库根目录"
  echo "  --contract <file>        CONTRACT.md 文件路径"
  echo "  --agents \"...\"           Agent 描述字符串（空格分隔，每项 name:worktree:branch:logfile）"
  echo ""
  echo "可选:"
  echo "  --max-global-rounds N    最大全局追问轮数（默认 5）"
  echo "  --poll-interval N        轮询间隔秒数（默认 10）"
  echo "  --syntax-check \"cmd\"     语法检查命令（例 'node --check'，为空跳过）"
  echo "  --dashboard-port N       展板端口（默认 7789，目前仅记录）"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)           REPO_DIR="$2";          shift 2 ;;
    --contract)       CONTRACT_FILE="$2";     shift 2 ;;
    --agents)         AGENTS_RAW="$2";        shift 2 ;;
    --max-global-rounds) MAX_GLOBAL_ROUNDS="$2"; shift 2 ;;
    --poll-interval)  POLL_INTERVAL="$2";     shift 2 ;;
    --syntax-check)   SYNTAX_CHECK="$2";      shift 2 ;;
    --dashboard-port) DASHBOARD_PORT="$2";    shift 2 ;;
    --help|-h)        usage ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

# ── 参数校验 ─────────────────────────────────────────────────────────
[[ -z "$REPO_DIR" ]]       && echo "错误: --repo 必填" && usage
[[ -z "$CONTRACT_FILE" ]]  && echo "错误: --contract 必填" && usage
[[ -z "$AGENTS_RAW" ]]     && echo "错误: --agents 必填" && usage
[[ ! -d "$REPO_DIR" ]]     && echo "错误: repo 目录不存在: $REPO_DIR" && exit 1
[[ ! -f "$CONTRACT_FILE" ]] && echo "错误: contract 文件不存在: $CONTRACT_FILE" && exit 1

# ── 初始化 log ────────────────────────────────────────────────────────
: > "$CONDUCTOR_LOG"    # 清空/创建 log 文件

emit() {
  # 写结构化标记行到 conductor log + stdout
  local tag="$1"; shift
  local line="##${tag}## [$(date '+%H:%M:%S')] $*"
  echo "$line" | tee -a "$CONDUCTOR_LOG"
}

log_plain() {
  # 普通信息行（不带 ## 标记）
  local line="[$(date '+%H:%M:%S')] $*"
  echo "$line" | tee -a "$CONDUCTOR_LOG"
}

# ── 解析 --agents 字符串为并行数组 ────────────────────────────────────
# 每项格式: name:worktree:branch:logfile
# 解析到独立数组方便后续下标访问
declare -a AGENT_NAMES=()
declare -a AGENT_WORKTREES=()
declare -a AGENT_BRANCHES=()
declare -a AGENT_LOGS=()
declare -a AGENT_STATUS=()   # waiting | done | failed | pass | retry

for entry in $AGENTS_RAW; do
  IFS=':' read -r aname awt abranch alog <<< "$entry"
  AGENT_NAMES+=("$aname")
  AGENT_WORKTREES+=("$awt")
  AGENT_BRANCHES+=("$abranch")
  AGENT_LOGS+=("$alog")
  AGENT_STATUS+=("waiting")
done

AGENT_COUNT=${#AGENT_NAMES[@]}
if [[ $AGENT_COUNT -eq 0 ]]; then
  echo "错误: --agents 解析后 Agent 数量为 0" && exit 1
fi

log_plain "解析到 $AGENT_COUNT 个 Agent: ${AGENT_NAMES[*]}"

# ── CONTRACT 解析函数 ──────────────────────────────────────────────────
# 从 CONTRACT.md 里提取某个 agent 的 Files 和 Exports 信息
# 使用 awk/sed，不依赖 jq 或 python

# 提取某 agent 的 Files 行（返回逗号分隔的文件名列表）
contract_files_for_agent() {
  local agent_name="$1"
  # 找 "## Agent: agent-name" 块，取 "Files:" 行
  awk "
    /^## Agent: ${agent_name}[[:space:]]*\$/ { in_section=1; next }
    /^## Agent:/ { in_section=0 }
    in_section && /^Files:/ {
      sub(/^Files:[[:space:]]*/, \"\"); print; exit
    }
  " "$CONTRACT_FILE"
}

# 提取某 agent 的导出符号列表（每行一个符号名）
contract_exports_for_agent() {
  local agent_name="$1"
  # 找 "## Agent: agent-name" 块，读 Exports: 后的 "- ..." 行
  # 格式: "- class ChessBoard" 或 "- method initBoard"
  # 提取最后一个单词（符号名）
  awk "
    /^## Agent: ${agent_name}[[:space:]]*\$/ { in_section=1; next }
    /^## Agent:/ { in_section=0 }
    in_section && /^Exports:/ { in_exports=1; next }
    in_section && in_exports && /^- / {
      # 取最后一个空格后的词作为符号名
      n=split(\$0, a, \" \"); print a[n]
    }
    in_section && in_exports && /^[^-]/ { in_exports=0 }
  " "$CONTRACT_FILE"
}

# ── 语法检查函数 ──────────────────────────────────────────────────────
# 对 worktree 里的指定文件列表逐一运行 syntax-check 命令
# 返回 0=全部通过，非 0=有失败；失败详情写到 check_result_file
syntax_check_files() {
  local worktree="$1"
  local files_csv="$2"     # 逗号分隔的文件名
  local result_file="$3"   # 结果写入文件

  if [[ -z "$SYNTAX_CHECK" ]]; then
    echo "syntax_check: skipped (no --syntax-check cmd)" >> "$result_file"
    return 0
  fi

  local failed=0
  # 将逗号分隔转换为遍历
  IFS=',' read -ra file_list <<< "$files_csv"
  for fname in "${file_list[@]}"; do
    fname="${fname// /}"   # 去掉空格
    local fpath="${worktree}/${fname}"
    if [[ ! -f "$fpath" ]]; then
      echo "MISSING_FILE: $fname" >> "$result_file"
      failed=1
      continue
    fi

    # 按扩展名决定检查器，css/html/md 直接跳过
    local ext="${fname##*.}"
    case "$ext" in
      css|html|md|txt|yaml|yml|toml)
        echo "SYNTAX_SKIP: $fname (unsupported ext)" >> "$result_file"
        continue
        ;;
    esac

    if [[ -z "$SYNTAX_CHECK" ]]; then
      echo "SYNTAX_SKIP: $fname (no --syntax-check cmd)" >> "$result_file"
      continue
    fi

    # 执行语法检查
    local check_out
    if ! check_out=$(eval "$SYNTAX_CHECK" "$fpath" 2>&1); then
      echo "SYNTAX_FAIL: $fname" >> "$result_file"
      echo "  output: $check_out" >> "$result_file"
      failed=1
    else
      echo "SYNTAX_OK: $fname" >> "$result_file"
    fi
  done
  return $failed
}

# ── CONTRACT 验证函数 ─────────────────────────────────────────────────
# 对 worktree 里的文件用 grep 验证导出符号是否存在
# 返回 0=全部通过，非 0=有缺失符号；详情写到 result_file
contract_check_agent() {
  local agent_name="$1"
  local worktree="$2"
  local result_file="$3"

  local exports
  exports=$(contract_exports_for_agent "$agent_name")

  if [[ -z "$exports" ]]; then
    echo "CONTRACT_CHECK: no exports defined for $agent_name (skipped)" >> "$result_file"
    return 0
  fi

  local files_csv
  files_csv=$(contract_files_for_agent "$agent_name")

  local failed=0
  while IFS= read -r symbol; do
    [[ -z "$symbol" ]] && continue
    local found=0
    # 在 agent 的所有文件里搜索该符号
    IFS=',' read -ra file_list <<< "$files_csv"
    for fname in "${file_list[@]}"; do
      fname="${fname// /}"
      local fpath="${worktree}/${fname}"
      [[ ! -f "$fpath" ]] && continue
      if grep -q "$symbol" "$fpath" 2>/dev/null; then
        found=1
        break
      fi
    done

    if [[ $found -eq 1 ]]; then
      echo "CONTRACT_OK: $symbol" >> "$result_file"
    else
      echo "CONTRACT_FAIL: $symbol (not found in ${files_csv})" >> "$result_file"
      failed=1
    fi
  done <<< "$exports"

  return $failed
}

# ── 生成追问 Prompt ───────────────────────────────────────────────────
# 把失败原因、CONTRACT 要求、当前文件摘要拼成一条追问
generate_inject_prompt() {
  local agent_name="$1"
  local worktree="$2"
  local check_file="$3"    # /tmp/wt-conductor-check-<agent>.txt
  local round="$4"

  local files_csv
  files_csv=$(contract_files_for_agent "$agent_name")

  local exports
  exports=$(contract_exports_for_agent "$agent_name")

  # 读取验证失败的条目
  local failures
  failures=$(grep -E "^(SYNTAX_FAIL|CONTRACT_FAIL|MISSING_FILE):" "$check_file" 2>/dev/null || echo "(无具体错误记录)")

  # 读取当前文件内容摘要（前 60 行）
  local file_preview=""
  IFS=',' read -ra file_list <<< "$files_csv"
  for fname in "${file_list[@]}"; do
    fname="${fname// /}"
    local fpath="${worktree}/${fname}"
    if [[ -f "$fpath" ]]; then
      local snippet
      snippet=$(head -60 "$fpath" 2>/dev/null || echo "(读取失败)")
      file_preview="${file_preview}
=== ${fname} (前60行) ===
${snippet}
"
    else
      file_preview="${file_preview}
=== ${fname} === (文件不存在)
"
    fi
  done

  cat <<EOF
[Conductor 追问 — 第 ${round} 轮]

集成验证发现你的输出存在问题，请修复后重新 commit。

## 验证失败详情
${failures}

## CONTRACT 要求（你必须满足）
所需文件: ${files_csv}
必须导出的符号:
${exports}

## 当前文件内容摘要
${file_preview}

## 任务要求
1. 修复上述所有失败项（缺失符号请在对应文件中添加实现）
2. 确保文件语法正确，可独立加载
3. 完成后执行: git add -A && git commit -m "[${agent_name}] fix: conductor-round-${round}"
4. 输出 ##RESULT## 标记说明本次修复内容

请现在开始修复。
EOF
}

# ── 等待 Agent 完成（Phase 0 / Phase 3）────────────────────────────────
# 等待 AGENT_STATUS[i] 从 waiting/retry 变为 done 或 failed
# 超时则标记为 failed
wait_for_agents() {
  local phase_label="$1"   # 用于日志区分 "Phase0" / "Phase3-roundN"
  local timeout_secs="$2"
  local deadline=$(( $(date +%s) + timeout_secs ))

  emit "PHASE" "$phase_label wait_start timeout=${timeout_secs}s agents=${AGENT_NAMES[*]}"

  # 记录本次等待的起始"看到 AGENT_DONE 的行数"基线
  # 避免把上一轮的 AGENT_DONE 误认为本轮完成
  declare -a baseline_lines=()
  for i in "${!AGENT_NAMES[@]}"; do
    local log="${AGENT_LOGS[$i]}"
    if [[ -f "$log" ]]; then
      baseline_lines+=("$(wc -l < "$log")")
    else
      baseline_lines+=(0)
    fi
  done

  while true; do
    local all_settled=1

    for i in "${!AGENT_NAMES[@]}"; do
      local name="${AGENT_NAMES[$i]}"
      local st="${AGENT_STATUS[$i]}"

      # 已经是终态（done/failed/pass）跳过
      [[ "$st" == "done" || "$st" == "failed" || "$st" == "pass" ]] && continue

      # 检查 log 文件里 baseline 之后是否出现 AGENT_DONE
      local log="${AGENT_LOGS[$i]}"
      local done_count=0
      if [[ -f "$log" ]]; then
        local total_lines
        total_lines=$(wc -l < "$log")
        local base=${baseline_lines[$i]}
        if [[ $total_lines -gt $base ]]; then
          done_count=$(tail -n "+$((base+1))" "$log" | grep -c "AGENT_DONE" 2>/dev/null || true)
        fi
      fi

      if [[ $done_count -gt 0 ]]; then
        AGENT_STATUS[$i]="done"
        emit "AGENT_STATUS" "agent=$name status=done phase=$phase_label"
      else
        all_settled=0   # 还有 agent 在等
      fi
    done

    [[ $all_settled -eq 1 ]] && break

    # 检查超时
    if [[ $(date +%s) -ge $deadline ]]; then
      for i in "${!AGENT_NAMES[@]}"; do
        if [[ "${AGENT_STATUS[$i]}" == "waiting" || "${AGENT_STATUS[$i]}" == "retry" ]]; then
          AGENT_STATUS[$i]="failed"
          emit "AGENT_STATUS" "agent=${AGENT_NAMES[$i]} status=failed reason=timeout phase=$phase_label"
        fi
      done
      break
    fi

    sleep "$POLL_INTERVAL"
  done
}

# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

emit "CONDUCTOR_START" "repo=$REPO_DIR contract=$CONTRACT_FILE agents=${AGENT_NAMES[*]} max_rounds=$MAX_GLOBAL_ROUNDS"

# ── Phase 0：等待所有 Agent 首轮完成 ──────────────────────────────────
emit "PHASE" "0"
wait_for_agents "Phase0" "$PHASE0_TIMEOUT"

# 统计 Phase 0 结果
done_count_p0=0
for st in "${AGENT_STATUS[@]}"; do
  [[ "$st" == "done" ]] && done_count_p0=$((done_count_p0+1))
done
log_plain "Phase 0 完成: ${done_count_p0}/${AGENT_COUNT} agents done"

# ── 主循环：Phase 1 → 2 → 3，最多 MAX_GLOBAL_ROUNDS 轮 ──────────────
GLOBAL_ROUND=0

while [[ $GLOBAL_ROUND -lt $MAX_GLOBAL_ROUNDS ]]; do
  GLOBAL_ROUND=$((GLOBAL_ROUND+1))
  log_plain "═══ 全局第 ${GLOBAL_ROUND}/${MAX_GLOBAL_ROUNDS} 轮 ═══"

  # ── Phase 1：集成验证 ────────────────────────────────────────────────
  emit "PHASE" "1 round=$GLOBAL_ROUND"

  any_failed=0
  for i in "${!AGENT_NAMES[@]}"; do
    local_name="${AGENT_NAMES[$i]}"
    local_wt="${AGENT_WORKTREES[$i]}"
    local_st="${AGENT_STATUS[$i]}"
    check_file="/tmp/wt-conductor-check-${local_name}.txt"

    # 已标记为 failed（超时）跳过验证
    if [[ "$local_st" == "failed" ]]; then
      log_plain "跳过验证: $local_name (已超时失败)"
      continue
    fi

    # 清空并重建 check 文件
    : > "$check_file"

    # ── a. 语法检查 ──────────────────────────────────────────────────
    files_csv=$(contract_files_for_agent "$local_name" || echo "")
    syntax_ok=0
    if [[ -n "$files_csv" ]]; then
      if syntax_check_files "$local_wt" "$files_csv" "$check_file"; then
        syntax_ok=1
      fi
    else
      echo "syntax_check: no files defined in CONTRACT for $local_name" >> "$check_file"
      syntax_ok=1   # 无文件定义 → 跳过语法检查视为通过
    fi

    # ── b. CONTRACT 符号检查 ─────────────────────────────────────────
    contract_ok=0
    if contract_check_agent "$local_name" "$local_wt" "$check_file"; then
      contract_ok=1
    fi

    # 记录检查结果
    s_label=$( [[ $syntax_ok -eq 1 ]] && echo "ok" || echo "fail" )
    c_label=$( [[ $contract_ok -eq 1 ]] && echo "ok" || echo "fail" )
    emit "CHECK_RESULT" "agent=$local_name syntax=${s_label} contract=${c_label}"

    if [[ $syntax_ok -eq 1 && $contract_ok -eq 1 ]]; then
      AGENT_STATUS[$i]="pass"
      emit "AGENT_STATUS" "agent=$local_name status=pass round=$GLOBAL_ROUND"
    else
      any_failed=1
      AGENT_STATUS[$i]="retry"
      emit "AGENT_STATUS" "agent=$local_name status=retry round=$GLOBAL_ROUND"
    fi
  done

  # 全部通过 → 退出主循环
  if [[ $any_failed -eq 0 ]]; then
    log_plain "所有 Agent 验证通过，退出主循环"
    break
  fi

  # ── Phase 2：对失败 Agent 注入追问 ──────────────────────────────────
  emit "PHASE" "2 round=$GLOBAL_ROUND"

  for i in "${!AGENT_NAMES[@]}"; do
    local_name="${AGENT_NAMES[$i]}"
    local_wt="${AGENT_WORKTREES[$i]}"

    [[ "${AGENT_STATUS[$i]}" != "retry" ]] && continue

    check_file="/tmp/wt-conductor-check-${local_name}.txt"
    inject_file="/tmp/wt-inject-${local_name}.txt"

    # 生成追问内容并写入 inject 文件（orchestrate.sh 会轮询它）
    generate_inject_prompt "$local_name" "$local_wt" "$check_file" "$GLOBAL_ROUND" \
      > "$inject_file"

    inject_reason=$(grep -E "^(SYNTAX_FAIL|CONTRACT_FAIL|MISSING_FILE):" "$check_file" \
      | head -3 | tr '\n' ';' || echo "check_failed")
    emit "INJECT" "agent=$local_name round=$GLOBAL_ROUND reason=${inject_reason}"

    # 将 agent 状态重置为 waiting，等待新的 AGENT_DONE
    AGENT_STATUS[$i]="waiting"
    emit "AGENT_STATUS" "agent=$local_name status=waiting reason=inject_sent"
  done

  # ── Phase 3：等待追问完成 ────────────────────────────────────────────
  emit "PHASE" "3 round=$GLOBAL_ROUND"

  # 检查是否有 agent 在等待（有注入发出）
  has_waiting=0
  for st in "${AGENT_STATUS[@]}"; do
    [[ "$st" == "waiting" ]] && has_waiting=1 && break
  done

  if [[ $has_waiting -eq 1 ]]; then
    # 等待时间 = 原始超时的一半（追问任务应该比初始任务快）
    inject_timeout=$(( PHASE0_TIMEOUT / 2 ))
    wait_for_agents "Phase3-round${GLOBAL_ROUND}" "$inject_timeout"
  fi

done   # 主循环结束

# ── Phase 4：输出最终报告 ─────────────────────────────────────────────
emit "PHASE" "4"

pass_count=0
fail_count=0
for st in "${AGENT_STATUS[@]}"; do
  case "$st" in
    pass) pass_count=$((pass_count+1)) ;;
    *)    fail_count=$((fail_count+1)) ;;
  esac
done

{
  echo "# Conductor 最终报告"
  echo "生成时间: $(date)"
  echo "仓库: $REPO_DIR"
  echo "CONTRACT: $CONTRACT_FILE"
  echo "全局轮数: $GLOBAL_ROUND / $MAX_GLOBAL_ROUNDS"
  echo ""
  echo "## Agent 状态汇总"
  echo "通过: ${pass_count}  失败: ${fail_count}  总计: ${AGENT_COUNT}"
  echo ""
  echo "| Agent | 状态 | Worktree | 分支 |"
  echo "|-------|------|----------|------|"
  for i in "${!AGENT_NAMES[@]}"; do
    echo "| ${AGENT_NAMES[$i]} | ${AGENT_STATUS[$i]} | ${AGENT_WORKTREES[$i]} | ${AGENT_BRANCHES[$i]} |"
  done
  echo ""
  echo "## 验证详情"
  for i in "${!AGENT_NAMES[@]}"; do
    local_name="${AGENT_NAMES[$i]}"
    check_file="/tmp/wt-conductor-check-${local_name}.txt"
    echo ""
    echo "### ${local_name} (${AGENT_STATUS[$i]})"
    if [[ -f "$check_file" ]]; then
      cat "$check_file"
    else
      echo "(无验证记录)"
    fi
  done
} > "$CONDUCTOR_REPORT"

emit "CONDUCTOR_DONE" "agents_pass=${pass_count} agents_fail=${fail_count} total_rounds=${GLOBAL_ROUND}"
log_plain "最终报告: $CONDUCTOR_REPORT"
log_plain "Conductor 日志: $CONDUCTOR_LOG"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Conductor 完成"
echo "║  通过: ${pass_count} / ${AGENT_COUNT}    失败: ${fail_count} / ${AGENT_COUNT}"
echo "║  轮数: ${GLOBAL_ROUND} / ${MAX_GLOBAL_ROUNDS}"
echo "║  报告: $CONDUCTOR_REPORT"
echo "╚══════════════════════════════════════════════════════════╝"

# 全部通过退出 0，否则退出 1
[[ $fail_count -eq 0 ]]
