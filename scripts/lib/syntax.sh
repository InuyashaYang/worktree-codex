#!/usr/bin/env bash
# lib/syntax.sh — 可插拔语法检查器（Layer 2：基础设施层）
#
# 职责：对单个文件做语法检查，按扩展名分发到对应检查器
# 接口：check_syntax <file>  →  exit 0 = 通过，exit 1 = 失败，stdout 输出诊断信息
# 设计：纯函数，无副作用，不写日志，调用方负责解释 exit code
#
# 支持的语言（按文件扩展名）：
#   .js  .mjs  .cjs  → node --check
#   .ts             → tsc --noEmit（若无 tsconfig 则 tsc --strict --noEmit）
#   .py             → python3 -m py_compile
#   .sh  .bash      → bash -n
#   .json           → python3 -m json.tool
#   其他            → 跳过，返回 0（未知类型不阻塞流程）
#
# 使用方式：
#   source lib/syntax.sh
#   check_syntax path/to/file.js   # 单文件
#   check_syntax_files "a.js b.py" # 多文件，任意一个失败即返回 1

set -euo pipefail

# ── 单文件语法检查 ──────────────────────────────────────────────────
check_syntax() {
  local file="$1"

  if [ ! -f "$file" ]; then
    echo "[syntax] SKIP file not found: $file"
    return 0
  fi

  local ext="${file##*.}"
  local result=0

  case "$ext" in
    js|mjs|cjs)
      if command -v node >/dev/null 2>&1; then
        node --check "$file" 2>&1 || result=$?
      else
        echo "[syntax] SKIP no node found for: $file"
      fi
      ;;

    ts)
      if command -v tsc >/dev/null 2>&1; then
        if [ -f "$(dirname "$file")/tsconfig.json" ]; then
          tsc --noEmit 2>&1 || result=$?
        else
          tsc --strict --noEmit --allowJs false \
              --module esnext --target esnext \
              "$file" 2>&1 || result=$?
        fi
      else
        echo "[syntax] SKIP no tsc found for: $file"
      fi
      ;;

    py)
      if command -v python3 >/dev/null 2>&1; then
        python3 -m py_compile "$file" 2>&1 || result=$?
      else
        echo "[syntax] SKIP no python3 found for: $file"
      fi
      ;;

    sh|bash)
      bash -n "$file" 2>&1 || result=$?
      ;;

    json)
      if command -v python3 >/dev/null 2>&1; then
        python3 -m json.tool "$file" > /dev/null 2>&1 || result=$?
      fi
      ;;

    css|html|md)
      # 无轻量检查器，跳过
      echo "[syntax] SKIP unsupported ext: $ext ($file)"
      ;;

    *)
      echo "[syntax] SKIP unknown ext: $ext ($file)"
      ;;
  esac

  if [ "$result" -eq 0 ]; then
    echo "[syntax] OK  $file"
  else
    echo "[syntax] FAIL $file (exit=$result)"
  fi

  return $result
}

# ── 多文件批量检查 ──────────────────────────────────────────────────
# 参数：空格分隔的文件路径字符串（或 newline 分隔）
# 返回：任意一个失败则 exit 1，全部通过则 exit 0
# stdout：每个文件的检查结果
check_syntax_files() {
  local files_str="$1"
  local overall=0

  while IFS= read -r file; do
    [ -z "$file" ] && continue
    file="$(echo "$file" | xargs)"  # trim whitespace
    check_syntax "$file" || overall=1
  done <<< "$(echo "$files_str" | tr ' ' '\n')"

  return $overall
}
