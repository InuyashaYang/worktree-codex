# CONTRACT.md — Agent 接口契约模板
#
# 本文件由 conductor.sh 解析，用于验证各 Agent 的输出是否满足接口约定。
#
# ══════════════════════════════════════════════════════════════════════
# 格式规范（供 conductor.sh 的 awk/grep 解析，请严格遵守）：
#
# 每个 Agent 块以 "## Agent: <name>" 开头（name 必须与 --agents 参数一致）
# Files:   该 Agent 应创建/修改的文件，逗号分隔（conductor 会在这些文件里 grep 符号）
# Exports: 该 Agent 必须导出的符号列表，每行以 "- <kind> <symbol>" 格式列出
#          kind 可以是 class / method / function / const / export 等（conductor 取最后一词）
#          conductor 用 grep 在 Files 指定的文件里验证 <symbol> 字符串存在
#
# 注意事项：
# - "## Agent:" 后的名字区分大小写，必须完全匹配
# - "Files:" 行里的文件名是相对于 worktree 根目录的路径
# - Exports 块在下一个 "## Agent:" 或文件末尾时结束
# - 以 "#" 开头的行（本说明区）会被 awk 跳过（awk 只匹配 "^## Agent:" 开头）
# - Files/Exports 行必须顶格写（无缩进），"-" 项也必须顶格
# ══════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────
# 示例项目：chess-game（国际象棋游戏）
# 两个 Agent 并行实现棋盘 UI 和游戏逻辑
#
# 任务分工：
#   agent-board → 负责 DOM 棋盘渲染（board.js, board.css）
#   agent-logic → 负责游戏规则和状态机（logic.js）
# ──────────────────────────────────────────────────────────────────────

## Agent: agent-board
Files: board.js, board.css
Exports:
- class ChessBoard
- method initBoard
- method highlightCell
- method clearHighlights
- method onCellClick
- method renderPiece
- const BOARD_SIZE

## Agent: agent-logic
Files: logic.js
Exports:
- class ChessLogic
- method getInitialBoard
- method isValidMove
- method applyMove
- method isInCheck
- method getAvailableMoves
- const PIECE_TYPES

# ──────────────────────────────────────────────────────────────────────
# 如何扩展：添加新 Agent
#
# 1. 复制以下模板，填入实际内容
# 2. name 必须与 conductor.sh --agents 参数中的 name 一致
# 3. Files 填实际会生成的文件（相对 worktree 根）
# 4. Exports 按"- <kind> <symbol>"格式列出所有必须存在的符号
#
## Agent: agent-NEW_NAME
# Files: src/newfile.js, src/newfile.css
# Exports:
# - class NewClassName
# - method newMethodName
# - function newFunctionName
# - const NEW_CONSTANT
# ──────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────
# 示例 2：全栈 TODO 应用，三 Agent 分工
# ──────────────────────────────────────────────────────────────────────

## Agent: agent-api
Files: api/routes.js, api/handlers.js
Exports:
- function createTask
- function listTasks
- function updateTask
- function deleteTask
- const router

## Agent: agent-ui
Files: frontend/app.js, frontend/components.js
Exports:
- class TaskList
- class TaskForm
- method renderTask
- method submitForm

## Agent: agent-db
Files: db/schema.js, db/queries.js
Exports:
- function initDb
- function insertTask
- function selectTasks
- function updateTaskById
- function deleteTaskById
- const DB_PATH

# ──────────────────────────────────────────────────────────────────────
# conductor.sh 解析逻辑（参考，不影响格式）
#
# 伪代码：
#   for each "## Agent: <name>" block:
#     files = split(Files_line, ",")
#     exports = [last_word(line) for line in Exports if line starts with "- "]
#     for each symbol in exports:
#       found = any(grep(symbol, file) for file in files)
#       if not found: CHECK_FAIL
#
# grep 匹配是简单字符串匹配（非正则），因此：
#   - "ChessBoard" 会匹配 "class ChessBoard {"
#   - "initBoard"  会匹配 "initBoard() {"
#   - "BOARD_SIZE" 会匹配 "const BOARD_SIZE = 8"
# ──────────────────────────────────────────────────────────────────────
