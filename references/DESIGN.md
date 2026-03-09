# worktree-codex — 设计文档

> 每个组件为什么这么写，设计取舍在哪里，当前实现到了哪里。

---

## 一、核心问题与解题思路

**问题：** AI Coding Agent（Codex / Claude Code）是单线程的——一次只能干一件事。大型项目顺序跑很慢，但直接把多个 Agent 指向同一个 git 仓库会产生文件冲突。

**解题：** 用 `git worktree` 给每个 Agent 开一个独立的工作目录，共享 `.git` 对象库但各自在独立分支上写文件。Agent 之间物理隔离，完成后各自 commit，最后合并 PR。

**类比：** 就像多个开发者在同一个 repo 上各自开 feature branch，区别是"开发者"是 AI，"分支切换"是 worktree。

---

## 二、组件地图

```
用户
 │
 ├─ launch.sh          ← 展板一键启动（入口）
 │
 ├─ setup_worktrees.sh ← 创建 git worktree（隔离）
 │
 ├─ orchestrate.sh     ← 单个 Agent 的完整生命周期管理
 │   ├─ 注册到展板
 │   ├─ 调 Codex/Claude Code
 │   ├─ 指数退避 retry
 │   └─ 自动 commit
 │
 ├─ push_and_pr.sh     ← 推送分支 + 创建 GitHub PR
 │
 └─ dashboard.py       ← 实时监控展板
     ├─ SSE 推送（前端 1s 刷新）
     ├─ 规则式 log 解析
     ├─ 甘特图 / token 统计
     └─ LLM 效率分析（任务完成后触发）
```

---

## 三、各组件设计详解

### 3.1 setup_worktrees.sh — 隔离的基础

**为什么用 worktree 而不是 clone？**

`git clone` 会复制整个对象库，大仓库很慢；worktree 共享 `.git`，只需建目录和分支，秒级完成。

**关键设计决策：**

```bash
ORIGINAL_BRANCH=$(git symbolic-ref --short HEAD ...)
git branch "$BRANCH" "$ORIGINAL_BRANCH"   # 明确基点
git worktree add "$WORKTREE_PATH" "$BRANCH"
```

- 明确从 `ORIGINAL_BRANCH` 分叉，而不是从"当前 HEAD"——因为并行时多个 Agent 依次调用脚本，HEAD 可能被上一个 Agent 的操作影响。
- 分支名加时间戳 + 4 位随机码（`feature/agent-a-20260305-a3f2`），防止并发时撞名。

**当前实现状态：** ✅ 完整。有旧 worktree 时自动清理再重建。

**已知限制：**
- worktree 必须放在 WSL 本地文件系统（`~/projects/...`），不能放 `/mnt/c/`——NTFS 挂载下 git 文件锁行为异常。

---

### 3.2 orchestrate.sh — Agent 生命周期管理

这是整个系统的"发动机"，一个 Agent 的完整生命都在这里。

**结构：**
```
注册展板 → Codex 执行（带 retry）→ 检查 commit → 标记 AGENT_DONE
```

**设计决策 1：为什么 retry 用指数退避？**

Codex 的失败场景分两类：
- **暂时性**（网络抖动、API 限频 429、代理 503）：值得重试
- **永久性**（prompt 错误、权限问题）：重试无意义

指数退避（10s → 20s）给暂时性错误留恢复时间，同时不会无限等待。最多 3 次机会（`CODEX_MAX_RETRIES=2`，默认可覆盖）。

```bash
# exit 0 → 成功，直接 break
# exit 1 但 log 里有完成标记 → Codex 部分版本行为，视为成功
# 其他 → 可重试
if [ "$EXIT_CODE" -eq 1 ] && grep -q "task.*complete\|AGENT_DONE\|no changes" "$LOG_FILE"; then
    EXIT_CODE=0; break
fi
```

**设计决策 2：为什么自动 commit？**

Codex 有时在完成任务后不自行 commit（取决于 prompt 写法和版本）。脚本检测 `git status --porcelain`，有未提交变更就自动 `git add -A && git commit`，保证 worktree 干净可合并。

**设计决策 3：展板注册为什么是静默失败？**

```bash
curl -s --max-time 1 ... || true
```

展板是可选组件。如果没有启动展板，这一行不应该让整个 Agent 任务失败。`|| true` 保证主流程继续。

**环境变量覆盖点：**

| 变量 | 默认 | 覆盖场景 |
|------|------|---------|
| `CODEX_MAX_RETRIES` | 2 | 网络很差时调高 |
| `CODEX_RETRY_BASE` | 10s | 快速测试时调低 |
| `CODEX_MODEL` | gpt-5.3-codex | 换模型 |
| `CODEX_BIN` | ~/.npm-global/bin/codex | 换 Claude Code |
| `DASHBOARD_PORT` | 7789 | 多展板时区分 |

**当前实现状态：** ✅ retry 完整，自动 commit 完整，展板注册静默失败。

**未实现：** Agent 间通信——当前 Agent 无法读取其他 Agent 的输出。如需串行依赖（A 完成后 B 才能开始），需要在 orchestrate.sh 外层手动控制顺序。

---

### 3.3 dashboard.py — 可观测性

**为什么用 SSE 而不是 WebSocket？**

SSE（Server-Sent Events）是单向的服务端推流，实现极简（标准库 `http.server` 即可），浏览器原生支持自动重连。展板不需要双向通信，SSE 够用且零依赖。

WebSocket 需要第三方库（`websockets`/`aiohttp`），杀鸡用牛刀。

**为什么用规则式解析而不是 LLM 解析 log？**

log 解析是热路径，每秒执行一次。LLM 调用有延迟和成本，规则式正则 `<100ms`。LLM 只在任务**完成后**做一次离线效率分析，不在实时路径上。

```python
# 规则式：精确、快、零成本
tokens_m = re.search(r"tokens used\s*\n([\d,]+)", text)
exit_m   = re.search(r"\] \S+ codex exited with code (\d+)", text)
```

**任务生命周期状态机：**

```
active（SSE 推送，1s 刷新）
  │
  └─ all_done=True → 触发 LLM 分析 → set_idle()
                                          │
idle（2s 刷新，页面不失效）  ←────────────┘
  │
  └─ POST /reload → active（新任务开始）
```

**为什么 idle 不关闭 SSE 连接？**

任务完成后用户可能还在看数据。关闭连接会让浏览器重连，重连瞬间会清空页面状态。idle 模式保持连接，只是降低刷新频率（1s → 2s），页面数据永久可读。

**心跳设计：**

```python
PING_INTERVAL = 15   # 每 15s 发一次 ping
# 前端：35s 没收到任何消息 → 判定假死 → 重连
```

Nginx / 反向代理默认 60s 无活动断连。15s ping 远低于这个阈值，防止连接被中间件掐断。

**LLM 分析为什么在后台线程？**

```python
threading.Thread(target=_run, daemon=True).start()
```

LLM 调用可能 10-30s。如果阻塞 SSE 循环，这期间前端收不到任何更新，看起来像卡死。后台线程让主循环继续推送 ping，LLM 结果出来后在下一轮 SSE 帧里推送。

**LLM 配置解耦：**

```python
LLM_CFG = {
    "base_url": args.llm_base_url or "http://YOUR_PROXY/v1",
    "api_key":  args.llm_api_key,   # 命令行 > OPENAI_API_KEY env > openclaw.json
    "model":    args.llm_model,     # 默认 gemini-2.5-flash
    "disabled": args.no_ai,
}
```

AI 分析用的 LLM 和 Codex 用的 LLM **职责不同**：前者用 `/chat/completions`（任意厂商），后者用 `/responses`（OpenAI 专有）。分开配置，互不干扰。

**当前实现状态：**
- ✅ SSE 实时推送 + 心跳防断
- ✅ 规则式解析（tokens/elapsed/diff/turns/retries/tok_per_line）
- ✅ 甘特图时间线
- ✅ 串行节省时间计算
- ✅ 展开 log 弹窗（点卡片）
- ✅ LLM 效率分析（后台线程，失败静默）
- ✅ `/register` `/reload` `/state` REST 接口
- ❌ Agent 间依赖可视化（串行任务的前后关系）
- ❌ log 搜索 / 过滤
- ❌ 历史任务对比（多次运行的 token 趋势）

---

### 3.4 launch.sh — 一键入口

**为什么需要一个包装脚本？**

`dashboard.py --llm-base-url ... --llm-api-key ... --llm-model ... --port ...` 参数太多，每次手打容易出错。`launch.sh` 提供：
- 合理的默认值（端口 7789，gemini-2.5-flash）
- `--bg`：后台运行 + PID 文件管理（重复启动自动替换旧实例）
- `--open`：自动打开浏览器（WSL2 用 `cmd.exe /c start`，Linux 用 `xdg-open`）
- `--help`：从注释生成帮助文档

**当前实现状态：** ✅ 完整。

---

### 3.5 push_and_pr.sh — 安全的 Token 传递

**为什么不直接把 token 写进 remote URL？**

```bash
# ❌ 危险：token 暴露在 git remote -v 输出里
git remote set-url origin "https://TOKEN@github.com/..."

# ✅ 安全：token 通过环境变量传入，不进 git 配置
export GIT_ASKPASS="$ASKPASS_SCRIPT"
git remote set-url origin "https://github.com/..."   # 干净 URL
GIT_ASKPASS="$ASKPASS_SCRIPT" git push origin "$BRANCH"
```

`GIT_ASKPASS` 是 git 的标准凭据机制，token 存在临时脚本里，push 完立即删除。即使 `git log` 或 `git remote -v` 输出被记录，也不会泄漏 token。

**当前实现状态：** ✅ 安全的 token 传递，PR 创建完整。

---

## 四、设计上的整体取舍

| 取舍点 | 选择 | 理由 |
|--------|------|------|
| 隔离方式 | worktree vs clone | worktree 秒级，共享对象库 |
| 通信协议 | SSE vs WebSocket | SSE 零依赖，展板不需要双向通信 |
| log 解析 | 规则式 vs LLM | 规则式在热路径（1s/次），LLM 只做离线分析 |
| 失败处理 | 指数退避 retry | 区分暂时/永久失败，不无限等待 |
| 展板可选 | 静默失败注册 | 展板不在 Agent 主路径上，不能让它阻塞任务 |
| LLM 配置 | 完全参数化 | 不绑死厂商，自建代理/OpenRouter 均可 |

---

## 五、当前未覆盖的场景（已知 Gap）

1. **Agent 间通信** — A 的输出作为 B 的输入，目前需要手动串行（conductor 的 Phase 0 等待全员完成后再做验证，解决了"等所有人都跑完再对齐"的问题，但尚不支持 A 的产物直接作为 B 的输入 prompt）
2. **动态任务分配** — 某个 Agent 完成后自动认领下一个任务，目前静态分配
3. **合并冲突处理** — push_and_pr.sh 创建 PR 后，冲突需要人工解决
4. **历史对比** — 多次运行的 token/速度趋势，展板每次重启清空
5. **Claude Code 支持完整性** — orchestrate.sh 主要为 Codex 设计，Claude Code 的参数略有不同（`--dangerously-skip-permissions` vs `--full-auto`）
6. **conductor 与展板集成** — conductor 日志（`/tmp/wt-conductor.log`）目前不注册到 dashboard.py，无法可视化追问轮次

---

## 六、主控轮询架构（conductor）

### 6.1 设计动机

worktree-codex v1 里，多个 Agent 并行跑完就完事了——没有验证每个 Agent 的输出是否满足接口约定，也没有机制在验证失败后自动追问修复。

"集成"阶段的问题往往在 PR 合并时才暴露（符号名拼错、文件没生成、接口不匹配），这时候 Codex session 已经结束，需要人工再次启动。

conductor 解决的核心问题：**让主控在所有 Agent 首轮跑完后，自动验证接口约定，对失败 Agent 注入追问，驱动多轮修复，无需人工介入。**

### 6.2 组件关系

```
用户
 │
 ├─ conductor.sh          ← 主控轮询器（新增）
 │   ├─ Phase 0: 等待所有 Agent AGENT_DONE
 │   ├─ Phase 1: 语法检查 + CONTRACT 符号验证
 │   ├─ Phase 2: 写追问到 /tmp/wt-inject-<name>.txt
 │   ├─ Phase 3: 等待追问完成 → 回 Phase 1
 │   └─ Phase 4: 输出 /tmp/wt-conductor-report.txt
 │
 └─ orchestrate.sh        ← 改造：增加注入监听
     └─ 每轮结束检查 /tmp/wt-inject-<name>.txt
         ├─ 有内容 → emit INJECT_TURN → run_turn(inject_prompt) → 清除文件
         └─ 无内容 → 原有逻辑（RESULT+COMMIT 检查 / 自动追问）
```

### 6.3 CONTRACT 格式与解析

CONTRACT.md 是纯 Markdown，供 conductor 用 awk/sed/grep 解析，无需 jq 或 Python。

**格式：**
```markdown
## Agent: agent-name
Files: file1.js, file2.css
Exports:
- class ClassName
- method methodName
- function funcName
```

**解析逻辑：**
- `awk` 提取 `## Agent: <name>` 块内的 `Files:` 行和 `Exports:` 行
- `- <kind> <symbol>` 格式中取最后一个词作为符号名
- `grep -q <symbol> <file>` 验证符号在对应文件中存在（简单字符串匹配）

### 6.4 追问注入机制

orchestrate.sh 的主循环每轮结束后，先检查注入文件再做原有判断：

```
turn 1: 主任务
loop:
  ┌─ 检查 /tmp/wt-inject-<name>.txt
  │   有内容 → run inject turn → 清除文件 → continue
  │   无内容 ↓
  ├─ RESULT + COMMIT 满足 → break
  ├─ turn < MAX_TURNS → 自动追问 → continue
  └─ break
```

注入文件由 conductor Phase 2 写入，包含：失败原因 + CONTRACT 要求 + 当前文件内容摘要（前60行）。

### 6.5 日志标记（conductor）

```
##CONDUCTOR_START##    启动时写入
##PHASE## 0|1|2|3|4   各阶段入口
##AGENT_STATUS## agent=xxx status=waiting|done|failed|pass|retry
##CHECK_RESULT## agent=xxx syntax=ok|fail contract=ok|fail
##INJECT## agent=xxx round=N reason=...
##CONDUCTOR_DONE## agents_pass=N agents_fail=M total_rounds=K
```

orchestrate.sh 新增：
```
##INJECT_TURN##        conductor 注入轮开始
```

### 6.6 设计取舍

| 取舍点 | 选择 | 理由 |
|--------|------|------|
| 符号验证方式 | grep 字符串匹配 | 不依赖 AST 解析，零依赖，适用于 JS/Python/Bash 等多语言 |
| 注入通信方式 | 临时文件 `/tmp/wt-inject-*.txt` | conductor 和 orchestrate 是独立进程，文件是最简单的 IPC |
| 等待机制 | 轮询 log 文件行数 | AGENT_DONE 是结构化标记，稳定可靠；避免进程间 pipe |
| 追问超时 | Phase 0 超时 30min，Phase 3 超时 15min | 追问任务应比初始任务快，给更短的窗口 |
| CONTRACT 格式 | Markdown + awk 解析 | 人类可读，AI 可直接生成，无需 JSON/YAML schema |
