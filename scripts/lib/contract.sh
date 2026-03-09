#!/usr/bin/env bash
# lib/contract.sh — CONTRACT.md 解析器（Layer 2：基础设施层）
#
# 职责：从 CONTRACT.md 纯文本中提取 Agent 的文件列表和导出符号，
#       在对应 worktree 里验证这些符号是否存在
#
# 接口：
#   parse_contract_agents <contract_file>
#       → stdout: 换行分隔的 agent 名列表
#
#   get_agent_files <contract_file> <agent_name>
#       → stdout: 逗号分隔的文件名列表（相对 worktree 根）
#
#   get_agent_exports <contract_file> <agent_name>
#       → stdout: 换行分隔的符号名列表（class/method/function/var 后的名称）
#
#   check_contract <contract_file> <agent_name> <worktree_path>
#       → exit 0 = 全部符号找到，exit 1 = 有缺失
#       → stdout: 每个符号的检查结果（FOUND / MISSING）
#
# CONTRACT.md 格式约定（本库只认这个格式）：
#
#   ## Agent: <name>
#   Files: file1.js, file2.css
#   Exports:
#   - class ClassName
#   - method methodName
#   - function funcName
#   - var varName
#   - const CONST_NAME
#
# 设计：纯函数，无副作用，grep/awk/sed 实现，无 jq/python 依赖

set -euo pipefail

# ── 提取所有 Agent 名称 ─────────────────────────────────────────────
parse_contract_agents() {
  local contract_file="$1"
  if [ ! -f "$contract_file" ]; then
    echo "[contract] ERROR contract file not found: $contract_file" >&2
    return 1
  fi
  # 匹配 "## Agent: <name>" 行，提取 name 部分（去除前后空格）
  grep -E '^## Agent:' "$contract_file" \
    | sed 's/^## Agent:[[:space:]]*//' \
    | sed 's/[[:space:]]*$//'
}

# ── 提取某 Agent 的文件列表 ────────────────────────────────────────
get_agent_files() {
  local contract_file="$1"
  local agent_name="$2"

  if [ ! -f "$contract_file" ]; then
    echo "[contract] ERROR contract file not found: $contract_file" >&2
    return 1
  fi

  # 用 awk：找到对应 Agent 块后，提取 Files: 行，直到下一个 ## 块
  awk -v agent="$agent_name" '
    /^## Agent:/ {
      in_block = ($0 ~ ("Agent: " agent "$"))
    }
    in_block && /^Files:/ {
      sub(/^Files:[[:space:]]*/, "")
      print
      exit
    }
  ' "$contract_file" | tr ',' '\n' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//' | grep -v '^$'
}

# ── 提取某 Agent 的导出符号 ────────────────────────────────────────
get_agent_exports() {
  local contract_file="$1"
  local agent_name="$2"

  if [ ! -f "$contract_file" ]; then
    echo "[contract] ERROR contract file not found: $contract_file" >&2
    return 1
  fi

  # 用 awk：找到 Agent 块后，从 Exports: 开始收集 "- xxx yyy" 行，提取符号名（最后一个词）
  awk -v agent="$agent_name" '
    /^## Agent:/ {
      in_block = ($0 ~ ("Agent: " agent "$"))
      in_exports = 0
    }
    in_block && /^Exports:/ {
      in_exports = 1
      next
    }
    in_block && in_exports && /^- / {
      # "- class ClassName" or "- method methodName" → 取最后一个空格后的词
      n = split($0, parts, " ")
      print parts[n]
    }
    in_block && in_exports && /^[^-]/ && !/^[[:space:]]/ {
      # 非 "- " 行且不是缩进 → 块结束
      in_exports = 0
    }
  ' "$contract_file" | grep -v '^$'
}

# ── 验证某 Agent 的 contract（主函数）──────────────────────────────
check_contract() {
  local contract_file="$1"
  local agent_name="$2"
  local worktree_path="$3"

  if [ ! -f "$contract_file" ]; then
    echo "[contract] ERROR contract file not found: $contract_file"
    return 1
  fi

  if [ ! -d "$worktree_path" ]; then
    echo "[contract] ERROR worktree not found: $worktree_path"
    return 1
  fi

  local overall=0
  local files missing_symbols=""

  # 获取该 Agent 负责的文件
  mapfile -t file_list < <(get_agent_files "$contract_file" "$agent_name" 2>/dev/null || true)
  if [ ${#file_list[@]} -eq 0 ]; then
    echo "[contract] WARN no files defined for agent: $agent_name"
    return 0
  fi

  # 获取该 Agent 应导出的符号
  mapfile -t export_list < <(get_agent_exports "$contract_file" "$agent_name" 2>/dev/null || true)
  if [ ${#export_list[@]} -eq 0 ]; then
    echo "[contract] WARN no exports defined for agent: $agent_name"
    return 0
  fi

  echo "[contract] Checking agent=$agent_name files=${file_list[*]}"

  # 对每个符号，在所有文件里搜索
  for symbol in "${export_list[@]}"; do
    local found=0
    for rel_file in "${file_list[@]}"; do
      local abs_file="$worktree_path/$rel_file"
      if [ ! -f "$abs_file" ]; then
        continue
      fi
      # grep 只要包含符号名（单词边界，不区分语言）
      if grep -q "\b${symbol}\b" "$abs_file" 2>/dev/null; then
        found=1
        break
      fi
    done

    if [ "$found" -eq 1 ]; then
      echo "[contract] FOUND   $symbol"
    else
      echo "[contract] MISSING $symbol"
      missing_symbols="${missing_symbols} ${symbol}"
      overall=1
    fi
  done

  # 检查文件是否实际存在
  for rel_file in "${file_list[@]}"; do
    local abs_file="$worktree_path/$rel_file"
    if [ ! -f "$abs_file" ]; then
      echo "[contract] FILE_MISSING $rel_file"
      overall=1
    fi
  done

  if [ "$overall" -eq 0 ]; then
    echo "[contract] PASS agent=$agent_name"
  else
    echo "[contract] FAIL agent=$agent_name missing:${missing_symbols}"
  fi

  return $overall
}

# ── 生成追问 prompt（供 conductor 注入用）──────────────────────────
# 参数：contract_file, agent_name, worktree_path, check_output（已有检查结果文本）
# stdout：完整的追问 prompt 文本
generate_inject_prompt() {
  local contract_file="$1"
  local agent_name="$2"
  local worktree_path="$3"
  local check_output="$4"

  # 读取该 Agent 负责的文件内容（前 200 行，防止 prompt 太长）
  local files_content=""
  while IFS= read -r rel_file; do
    local abs_file="$worktree_path/$rel_file"
    if [ -f "$abs_file" ]; then
      files_content="${files_content}
=== 当前文件：$rel_file ===
$(head -200 "$abs_file")
"
    else
      files_content="${files_content}
=== 文件不存在：$rel_file ===
"
    fi
  done < <(get_agent_files "$contract_file" "$agent_name" 2>/dev/null || true)

  # 读取 Agent 的 contract 段落
  local contract_section
  contract_section=$(awk -v agent="$agent_name" '
    /^## Agent:/ {
      in_block = ($0 ~ ("Agent: " agent "$"))
    }
    in_block { print }
    !in_block && prev_in_block { exit }
    { prev_in_block = in_block }
  ' "$contract_file" 2>/dev/null || echo "(无法读取 contract)")

  cat <<PROMPT
主控验证发现你负责的模块未通过接口契约检查，请修复以下问题：

## 验证结果
${check_output}

## 你的接口契约要求
${contract_section}

## 当前文件内容
${files_content}

## 修复要求
1. 严格按照接口契约补全所有 MISSING 符号（类/方法/函数）
2. 确保导出方式正确（export class / export function）
3. 文件不存在的，立即创建
4. 修复完成后输出 ##RESULT## 并执行 git add -A && git commit -m "[${agent_name}] contract fix"
PROMPT
}
