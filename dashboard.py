#!/usr/bin/env python3
"""
worktree-codex 实时监控展板 v1.1
- 任务生命周期绑定：idle 保持页面，/register 追加 agent，/reload 重置
- 规则式解析：tokens / elapsed / diff stat / turns / retries / session_id / shell cmds
- 甘特图时间线（所有 agent 横向对比）
- 串行等效耗时 vs 实际并行耗时
- 点击卡片展开完整 log
- token/line 效率比
- gemini-2.5-flash：代码质量简评 + 弯路检测 + 最差 prompt 指出 + 改进 prompt 片段
"""

import argparse, glob, json, os, re, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """每个请求用独立线程，防止 LLM 调用阻塞整个服务器"""
    daemon_threads = True

# ──────────────────────────────────────────────
# 全局状态
# ──────────────────────────────────────────────

LLM_CFG: dict = {}   # 全局 LLM 配置，由 main() 填充

class State:
    def __init__(self, log_paths):
        self.log_paths    = list(log_paths)
        self.mode         = "active"
        self.ai_triggered = False
        self.ai_cache     = {}
        self.lock         = threading.Lock()

    def reload(self, new_paths):
        with self.lock:
            self.log_paths    = list(new_paths)
            self.mode         = "active"
            self.ai_triggered = False
            self.ai_cache.clear()

    def register(self, log_path):
        with self.lock:
            if log_path not in self.log_paths:
                self.log_paths.append(log_path)
            self.mode         = "active"
            self.ai_triggered = False
            self.ai_cache.clear()

    def set_idle(self):
        with self.lock:
            self.mode = "idle"

STATE: State = None

# ──────────────────────────────────────────────
# 规则式 Log 解析
# ──────────────────────────────────────────────

def parse_intent_trail(text: str) -> dict:
    """
    从 log 中提取所有 ##TAG## 标记，构建带时间戳的完整事件序列。

    格式：##TAG## [HH:MM:SS] 内容
    支持的 TAG：
      TASK_GOAL / TASK_FILES / TASK_CTX        — 任务快照（开始时）
      INTENT / OBSTACLE / DECISION / RESULT    — Codex 推理轨迹（Codex 输出）
      RETRY / RETRY_PLAN / CODEX_EXIT          — 执行控制事件（orchestrate 输出）
      OUTCOME_DIFF / OUTCOME_FILES             — 结果快照（结束时）
      AUTO_COMMIT / TASK_FAILED / AGENT_DONE   — 收尾事件

    返回：
      timeline: [{ts, tag, text}]   — 完整有序事件序列，带时间戳
      task_goal, task_files         — 任务意图摘要
      intents, obstacles, decisions — 分类列表（无时间戳，用于卡片快速展示）
      result                        — 最终 RESULT（最后一条）
      outcome_diff, outcome_files   — 结果快照
      has_obstacles                 — 是否遇到障碍（bool）
      has_retries                   — 是否发生重试（bool）
    """
    # ── 1. 提取所有标记事件，保留原始顺序和时间戳 ──────────────────────
    TAG_PATTERN = re.compile(
        r'##(\w+)##\s*(?:\[(\d{2}:\d{2}:\d{2})\])?\s*(.*)', re.MULTILINE
    )
    timeline = []
    for m in TAG_PATTERN.finditer(text):
        tag, ts, content = m.group(1), m.group(2) or "", m.group(3).strip()
        if content or ts:
            timeline.append({"ts": ts, "tag": tag, "text": content})

    # ── 2. 从 timeline 提取各分类列表 ────────────────────────────────────
    def by_tag(*tags):
        return [e["text"] for e in timeline if e["tag"] in tags and e["text"]]

    goal_events  = by_tag("TASK_GOAL")
    files_events = by_tag("TASK_FILES")
    intents      = by_tag("INTENT")
    obstacles    = by_tag("OBSTACLE")
    decisions    = by_tag("DECISION")
    results      = by_tag("RESULT")
    odiff        = by_tag("OUTCOME_DIFF")
    ofiles       = by_tag("OUTCOME_FILES")

    # ── 3. 关联 shell 命令到最近的 INTENT ────────────────────────────────
    # 在 timeline 中找到每条 shell 命令前最近的 INTENT，建立上下文映射
    shell_with_ctx = []
    last_intent = ""
    for line in text.splitlines():
        tag_m = TAG_PATTERN.match(line.strip())
        if tag_m and tag_m.group(1) == "INTENT":
            last_intent = tag_m.group(3).strip()
        cmd_m = re.match(r'(?:Running|Executing|shell_call)[:\s]+`([^`\n]{1,80})`', line)
        if cmd_m:
            shell_with_ctx.append({
                "cmd":     cmd_m.group(1),
                "context": last_intent  # 此命令在哪个 INTENT 下发出
            })

    return {
        "timeline":      timeline,                    # 完整有序事件流
        "task_goal":     goal_events[0]  if goal_events  else "",
        "task_files":    files_events[0] if files_events else "",
        "intents":       intents,
        "obstacles":     obstacles,
        "decisions":     decisions,
        "result":        results[-1] if results else "",
        "outcome_diff":  odiff[0]  if odiff  else "",
        "outcome_files": ofiles[0] if ofiles else "",
        "has_obstacles": len(obstacles) > 0,
        "has_retries":   any(e["tag"] in ("RETRY", "RETRY_PLAN") for e in timeline),
        "shell_with_ctx": shell_with_ctx[:30],        # 最多30条，带意图上下文
    }


def parse_log(path: str) -> dict:
    empty = {"status": "waiting", "name": Path(path).stem,
             "tokens": None, "tokens_in": None, "tokens_out": None,
             "model": "unknown", "start_t": None, "end_t": None,
             "elapsed": None, "last_line": "", "log": "",
             "has_warning": False, "path": path,
             "files_changed": None, "insertions": None, "deletions": None,
             "turns": None, "retries": 0, "session_id": None,
             "shell_cmds": [], "tok_per_line": None,
             "intent_trail": {}}
    try:
        text = Path(path).read_text(errors="replace")
    except FileNotFoundError:
        return empty

    name_m    = re.search(r"\] (\S+) starting", text)
    name      = name_m.group(1) if name_m else Path(path).stem
    # exit code：优先从 ##CODEX_EXIT## / ##AGENT_DONE## 标记读，其次老格式
    codex_exit_m = re.search(r'##CODEX_EXIT##[^\n]*code=(\d+)', text)
    agent_done_m = re.search(r'##AGENT_DONE##[^\n]*exit_code=(\d+)', text)
    exit_m       = re.search(r'\] \S+ codex exited with code (\d+)', text)
    if codex_exit_m:
        exit_code = int(codex_exit_m.group(1))
    elif agent_done_m:
        exit_code = int(agent_done_m.group(1))
    elif exit_m:
        exit_code = int(exit_m.group(1))
    else:
        exit_code = None

    tokens_m   = re.search(r"tokens used\s*\n([\d,]+)", text)
    tokens     = int(tokens_m.group(1).replace(",", "")) if tokens_m else None
    tin_m      = re.search(r"input[:\s]+([\d,]+)\s*token", text, re.I)
    tout_m     = re.search(r"output[:\s]+([\d,]+)\s*token", text, re.I)
    tokens_in  = int(tin_m.group(1).replace(",", "")) if tin_m else None
    tokens_out = int(tout_m.group(1).replace(",", "")) if tout_m else None

    model_m    = re.search(r"model=(\S+)", text)
    model      = model_m.group(1) if model_m else "unknown"

    sid_m      = re.search(r"session id:\s*(\S+)", text)
    session_id = sid_m.group(1) if sid_m else None

    if "AGENT_DONE" in text:
        status = "done" if exit_code == 0 else "failed"
    elif exit_m and exit_code != 0:
        status = "failed"
    elif "starting in" in text:
        status = "running"
    else:
        status = "waiting"

    times   = re.findall(r"\[(\d{2}:\d{2}:\d{2})\]", text)
    start_t = times[0] if times else None
    end_t   = times[-1] if len(times) > 1 else None

    elapsed = None
    if start_t and end_t and start_t != end_t:
        def to_sec(t):
            h, m, s = map(int, t.split(":"))
            return h * 3600 + m * 60 + s
        elapsed = to_sec(end_t) - to_sec(start_t)

    lines_list = [l.strip() for l in text.splitlines() if l.strip()]
    last_line  = lines_list[-1] if lines_list else ""
    has_warning = bool(re.search(r"warning:.*metadata.*not found", text, re.I))

    diff_m        = re.search(r"(\d+) file[s]? changed", text)
    ins_m         = re.search(r"(\d+) insertion", text)
    del_m         = re.search(r"(\d+) deletion", text)
    files_changed = int(diff_m.group(1)) if diff_m else None
    insertions    = int(ins_m.group(1)) if ins_m else None
    deletions     = int(del_m.group(1)) if del_m else None

    # turn 数（codex 每次工具调用算一轮，用 "shell_call" 行估算）
    shell_cmds = re.findall(r'(?:Running|Executing|shell_call)[:\s]+`([^`\n]{1,80})`', text)
    turns      = len(shell_cmds) if shell_cmds else None

    # 重试/重连次数
    retries = len(re.findall(r"Reconnecting\.\.\.", text))

    # token/line 效率比
    total_lines = (insertions or 0) + (deletions or 0)
    tok_per_line = round(tokens / total_lines, 1) if tokens and total_lines > 0 else None

    return {"name": name, "status": status, "exit_code": exit_code,
            "tokens": tokens, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "model": model, "start_t": start_t, "end_t": end_t, "elapsed": elapsed,
            "last_line": last_line, "log": text,          # 全文，前端截取
            "has_warning": has_warning, "path": path,
            "files_changed": files_changed, "insertions": insertions, "deletions": deletions,
            "turns": turns, "retries": retries, "session_id": session_id,
            "shell_cmds": shell_cmds[:20],                # 最多20条
            "tok_per_line": tok_per_line,
            "intent_trail": parse_intent_trail(text)}

def collect_stats(agents):
    total_tokens   = sum(a["tokens"] or 0 for a in agents)
    done    = sum(1 for a in agents if a["status"] == "done")
    fail    = sum(1 for a in agents if a["status"] == "failed")
    run     = sum(1 for a in agents if a["status"] == "running")
    wait    = sum(1 for a in agents if a["status"] == "waiting")
    elapsed_list   = [a["elapsed"] for a in agents if a.get("elapsed")]
    serial_equiv   = sum(elapsed_list) if elapsed_list else None
    actual_parallel = max(elapsed_list) if elapsed_list else None
    saved = round(serial_equiv - actual_parallel, 1) if serial_equiv and actual_parallel else None
    return {"total": len(agents), "done": done, "failed": fail,
            "running": run, "waiting": wait, "total_tokens": total_tokens,
            "all_done": (done + fail) == len(agents) and len(agents) > 0,
            "serial_equiv": serial_equiv, "actual_parallel": actual_parallel,
            "time_saved": saved}

# ──────────────────────────────────────────────
# gemini-2.5-flash 智能分析（后台线程，失败静默）
# ──────────────────────────────────────────────

def ai_analyze_async(agents):
    """后台线程调用 LLM 分析，失败静默。LLM 端点从全局 LLM_CFG 读取。"""
    if LLM_CFG.get("disabled"):
        return

    def _run():
        resp = None
        try:
            import httpx

            # API Key 优先级：命令行传入 > 环境变量 OPENAI_API_KEY > openclaw.json OPENROUTER_API_KEY > 空
            api_key = LLM_CFG.get("api_key")
            if not api_key:
                api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                try:
                    cfg = json.load(open(os.path.expanduser("~/.openclaw/openclaw.json")))
                    api_key = (cfg.get("env") or {}).get("OPENROUTER_API_KEY", "")
                except Exception:
                    api_key = ""

            base_url = LLM_CFG["base_url"].rstrip("/")
            model    = LLM_CFG["model"]

            # 构建 prompt — 加入 intent_trail 数据
            agent_blocks = []
            for a in agents:
                elapsed_str  = f"{a['elapsed']}s" if a.get("elapsed") else "?"
                tpl_str      = f"{a['tok_per_line']} tok/line" if a.get("tok_per_line") else "?"
                diff_str     = f"+{a.get('insertions',0)}-{a.get('deletions',0)} lines" if a.get("files_changed") else "no diff"
                turns_str    = f"{a['turns']} shell calls" if a.get("turns") else "?"
                retries_str  = f"{a['retries']} retries" if a.get("retries") else "0 retries"
                log_tail     = a["log"][-150:].strip() if a.get("log") else "(no log)"

                # 加入 intent_trail 摘要
                it = a.get("intent_trail") or {}
                trail_lines = []
                if it.get("task_goal"):
                    trail_lines.append(f"  目标: {it['task_goal'][:100]}")
                for s in (it.get("intents") or [])[:4]:
                    trail_lines.append(f"  →意图: {s[:80]}")
                for s in (it.get("obstacles") or []):
                    trail_lines.append(f"  ⚠障碍: {s[:80]}")
                for s in (it.get("decisions") or []):
                    trail_lines.append(f"  ⑃决策: {s[:80]}")
                if it.get("result"):
                    trail_lines.append(f"  ✓结果: {it['result'][:100]}")
                # shell 命令和意图关联
                for sc in (it.get("shell_with_ctx") or [])[:5]:
                    ctx = f"[{sc['context'][:40]}] " if sc.get("context") else ""
                    trail_lines.append(f"  $ {ctx}{sc['cmd'][:60]}")
                trail_summary = "\n".join(trail_lines) if trail_lines else "  (无可解释性标记)"

                agent_blocks.append(
                    f"=== {a['name']} ===\n"
                    f"status={a['status']} model={a['model']} tokens={a['tokens']} "
                    f"elapsed={elapsed_str} efficiency={tpl_str}\n"
                    f"diff={diff_str} turns={turns_str} {retries_str}"
                    + (" ⚠no-model-metadata" if a.get("has_warning") else "") + "\n"
                    f"推理轨迹:\n{trail_summary}\n"
                    f"log tail:\n{log_tail}"
                )

            user_msg = (
                f"并行编码任务数据（含推理轨迹）：\n\n" + "\n\n".join(agent_blocks) + "\n\n"
                "直接输出bullet（不超过7条，每条含具体数字或引用轨迹内容）：\n"
                "• token效率：最费/最省各谁，tok/line是否合理\n"
                "• 有无弯路（重试/OBSTACLE/反复修改）\n"
                "• 推理质量：INTENT→DECISION→RESULT链条是否清晰合理\n"
                "• 哪个决策最值得关注（好的或坏的）\n"
                "• 代码质量简评（从log tail和diff判断）\n"
                "• 最差prompt是哪个agent，问题在哪\n"
                "• 1条改进prompt或协议片段（代码块）"
            )
            print(f"[dashboard] AI analyze: model={model} base={base_url} prompt={len(user_msg)}chars")

            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={"model": model,
                      "messages": [
                          {"role": "system", "content":
                           "你是代码Agent效率审查员。只输出bullet list，不超过6条，每条含具体数字，无开场白。"},
                          {"role": "user", "content": user_msg}
                      ],
                      "max_tokens": 600},
                timeout=30,
            )
            resp_json = resp.json()
            content = (resp_json.get("choices", [{}])[0]
                       .get("message", {}).get("content") or "")
            result = content.strip() if content else "（LLM 返回空内容）"
            STATE.ai_cache["last"] = result
        except Exception as e:
            raw = ""
            try:
                raw = resp.text[:300] if resp is not None else "(no response)"
            except Exception:
                pass
            STATE.ai_cache["last"] = f"（AI 分析不可用：{e} | raw: {raw}）"

    threading.Thread(target=_run, daemon=True).start()

# ──────────────────────────────────────────────
# HTML
# ──────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>worktree-codex dashboard</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #c9d1d9; font-family: 'Cascadia Code','Fira Code',monospace; font-size: 13px; }
header { background: #161b22; border-bottom: 1px solid #30363d; padding: 12px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
header h1 { font-size: 15px; color: #58a6ff; white-space: nowrap; }
#overall { display: flex; gap: 10px; font-size: 12px; color: #8b949e; flex-wrap: wrap; align-items: center; }
#time-saved { font-size: 12px; color: #3fb950; font-weight: bold; }
#idle-banner { display: none; background: #21262d; color: #8b949e; font-size: 12px; padding: 6px 20px; border-bottom: 1px solid #30363d; }
.badge { padding: 2px 8px; border-radius: 10px; font-weight: bold; font-size: 11px; }
.badge.done    { background: #238636; color: #fff; }
.badge.running { background: #1f6feb; color: #fff; }
.badge.failed  { background: #da3633; color: #fff; }
.badge.waiting { background: #30363d; color: #8b949e; }
.badge.idle    { background: #30363d; color: #f0883e; }

/* 甘特图 */
#gantt { margin: 12px 16px 0; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; display: none; }
#gantt h3 { font-size: 12px; color: #58a6ff; margin-bottom: 10px; }
.gantt-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 11px; }
.gantt-label { width: 80px; text-align: right; color: #8b949e; flex-shrink: 0; }
.gantt-track { flex: 1; height: 16px; background: #21262d; border-radius: 3px; position: relative; }
.gantt-bar { position: absolute; height: 100%; border-radius: 3px; }
.gantt-bar.done    { background: #238636; }
.gantt-bar.running { background: #1f6feb; animation: pulse 1.5s infinite; }
.gantt-bar.failed  { background: #da3633; }
.gantt-time { width: 40px; font-size: 10px; color: #8b949e; }

/* 卡片 */
#agents { display: flex; flex-wrap: wrap; gap: 12px; padding: 16px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; width: 340px; padding: 14px; transition: border-color .3s; cursor: pointer; }
.card:hover { border-color: #58a6ff44; }
.card.running { border-color: #1f6feb; }
.card.done    { border-color: #238636; }
.card.failed  { border-color: #da3633; }
.card-header  { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.card-name    { font-size: 14px; font-weight: bold; color: #e6edf3; }
.card-meta    { font-size: 11px; color: #8b949e; margin-bottom: 6px; line-height: 1.6; }
.progress-bar { height: 3px; background: #30363d; border-radius: 2px; overflow: hidden; margin-bottom: 8px; }
.progress-fill { height: 100%; border-radius: 2px; transition: width .5s; }
.fill-running { background: #1f6feb; animation: pulse 1.5s infinite; }
.fill-done    { background: #238636; }
.fill-failed  { background: #da3633; }
.fill-waiting { background: #30363d; width: 0%; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.log-box { background: #0d1117; border: 1px solid #21262d; border-radius: 4px; padding: 8px; height: 60px; overflow-y: auto; font-size: 11px; color: #8b949e; white-space: pre-wrap; word-break: break-all; }
.stat-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.stat-chip { font-size: 11px; padding: 2px 7px; border-radius: 8px; background: #21262d; }
.chip-token  { color: #f0883e; }
.chip-diff   { color: #3fb950; }
.chip-time   { color: #8b949e; }
.chip-warn   { color: #d29922; }
.chip-turns  { color: #a371f7; }
.chip-eff    { color: #58a6ff; }

/* Intent Trail */
.trail-box     { margin: 6px 0 4px; border-left: 2px solid #30363d; padding-left: 8px; display: flex; flex-direction: column; gap: 5px; max-height: 130px; overflow-y: auto; }
.trail-row     { font-size: 11px; line-height: 1.6; white-space: normal; overflow: hidden; text-overflow: ellipsis; word-break: break-all; }
.trail-goal    { color: #58a6ff; }
.trail-intent  { color: #c9d1d9; }
.trail-obstacle{ color: #d29922; }
.trail-decision{ color: #a371f7; }
.trail-result  { color: #3fb950; font-weight: bold; }

/* 展开 log 弹窗 */
#log-modal { display: none; position: fixed; inset: 0; background: #000a; z-index: 100; align-items: center; justify-content: center; }
#log-modal.open { display: flex; }
#log-modal-inner { background: #161b22; border: 1px solid #30363d; border-radius: 10px; width: 85vw; max-height: 85vh; display: flex; flex-direction: column; }
#log-modal-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid #30363d; flex-shrink: 0; }
#log-modal-title { font-size: 13px; font-weight: bold; color: #e6edf3; }
#log-modal-close { cursor: pointer; color: #8b949e; font-size: 18px; line-height: 1; }

/* 弹窗 tab 切换 */
#log-modal-tabs { display: flex; gap: 0; border-bottom: 1px solid #30363d; flex-shrink: 0; }
.modal-tab { padding: 8px 16px; font-size: 12px; color: #8b949e; cursor: pointer; border-bottom: 2px solid transparent; }
.modal-tab.active { color: #58a6ff; border-bottom-color: #58a6ff; }

#log-modal-body { flex: 1; overflow-y: auto; padding: 12px 16px; font-size: 11px; color: #8b949e; white-space: pre-wrap; word-break: break-all; }

/* Timeline 视图 */
#log-modal-timeline { flex: 1; overflow-y: auto; padding: 12px 16px; display: none; }
.tl-event { display: flex; gap: 10px; margin-bottom: 6px; align-items: flex-start; }
.tl-ts    { font-size: 10px; color: #484f58; width: 60px; flex-shrink: 0; padding-top: 2px; font-family: monospace; }
.tl-dot   { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; margin-top: 3px; }
.tl-text  { font-size: 11px; line-height: 1.5; flex: 1; word-break: break-word; }
.tl-tag   { font-size: 10px; padding: 1px 5px; border-radius: 4px; margin-right: 5px; font-weight: bold; }

/* tag 颜色映射 */
.tag-TASK_GOAL,.tag-TASK_START { background:#1f3a5f; color:#58a6ff; }
.tag-TASK_FILES,.tag-TASK_CTX  { background:#1b2a1b; color:#3fb950; }
.tag-INTENT    { background:#21262d; color:#c9d1d9; }
.tag-OBSTACLE  { background:#3a2a00; color:#d29922; }
.tag-DECISION  { background:#2a1f3a; color:#a371f7; }
.tag-RESULT    { background:#1b3a1b; color:#3fb950; }
.tag-RETRY,.tag-RETRY_PLAN { background:#3a1f1f; color:#f85149; }
.tag-CODEX_EXIT,.tag-AUTO_COMMIT { background:#21262d; color:#8b949e; }
.tag-OUTCOME_DIFF,.tag-OUTCOME_FILES { background:#1f2d1f; color:#3fb950; }
.tag-AGENT_DONE { background:#238636; color:#fff; }
.tag-TASK_FAILED{ background:#da3633; color:#fff; }
.tag-default    { background:#21262d; color:#8b949e; }

/* shell 命令上下文视图 */
#log-modal-shells { flex: 1; overflow-y: auto; padding: 12px 16px; display: none; }
.shell-item { margin-bottom: 8px; border-left: 2px solid #30363d; padding-left: 10px; }
.shell-ctx  { font-size: 10px; color: #8b949e; margin-bottom: 2px; font-style: italic; }
.shell-cmd  { font-size: 11px; color: #3fb950; font-family: monospace; }

/* AI 分析 */
#ai-box { margin: 0 16px 16px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; display: none; }
#ai-box h3 { font-size: 12px; color: #58a6ff; margin-bottom: 8px; }
#ai-content { font-size: 12px; color: #c9d1d9; white-space: pre-wrap; line-height: 1.7; }
#ai-loading { color: #8b949e; font-size: 11px; }
footer { text-align: center; padding: 8px; color: #30363d; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>⚡ worktree-codex</h1>
  <div id="overall"></div>
  <div id="time-saved"></div>
</header>
<div id="idle-banner">⏸ 任务已完成，等待下次任务… 页面不失效</div>
<div id="gantt"><h3>📊 时间线</h3><div id="gantt-rows"></div></div>
<div id="agents"></div>
<div id="ai-box">
  <h3>🤖 gemini-2.5-flash 分析</h3>
  <div id="ai-loading">正在分析…</div>
  <div id="ai-content" style="display:none"></div>
</div>
<footer>worktree-codex v1.2 · <span id="conn-status">connecting…</span></footer>

<!-- log 展开弹窗（三 tab） -->
<div id="log-modal">
  <div id="log-modal-inner">
    <div id="log-modal-header">
      <span id="log-modal-title"></span>
      <span id="log-modal-close">✕</span>
    </div>
    <div id="log-modal-tabs">
      <div class="modal-tab active" data-tab="timeline" onclick="switchTab('timeline')">🧠 Timeline</div>
      <div class="modal-tab" data-tab="shells" onclick="switchTab('shells')">⌨ Shells</div>
      <div class="modal-tab" data-tab="raw" onclick="switchTab('raw')">📄 Raw Log</div>
    </div>
    <div id="log-modal-timeline" style="display:flex;flex-direction:column"></div>
    <div id="log-modal-shells"   style="display:none;flex-direction:column"></div>
    <div id="log-modal-body"     style="display:none"></div>
  </div>
</div>

<script>
const statusLabel = {done:'✅ done', running:'⚡ running', failed:'❌ failed', waiting:'⏳ waiting'};
const progressPct = {done:100, running:60, failed:100, waiting:0};
let allAgents = [];

function esc(t) {
  return String(t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── 甘特图 ──
function renderGantt(agents) {
  const finished = agents.filter(a => a.elapsed);
  if (!finished.length) { document.getElementById('gantt').style.display='none'; return; }
  document.getElementById('gantt').style.display='block';
  const maxE = Math.max(...finished.map(a => a.elapsed));
  document.getElementById('gantt-rows').innerHTML = agents.map(a => {
    if (!a.elapsed) return '';
    const pct = (a.elapsed / maxE * 100).toFixed(1);
    return `<div class="gantt-row">
      <span class="gantt-label">${esc(a.name)}</span>
      <div class="gantt-track">
        <div class="gantt-bar ${a.status}" style="width:${pct}%"></div>
      </div>
      <span class="gantt-time">${a.elapsed}s</span>
    </div>`;
  }).join('');
}

// ── 卡片 ──
function renderCard(a) {
  const pct   = progressPct[a.status] || 0;
  const time  = (a.start_t && a.end_t && a.start_t !== a.end_t)
    ? `${a.start_t} → ${a.end_t}` : (a.start_t || '–');
  const chips = [];
  if (a.tokens)         chips.push(`<span class="stat-chip chip-token">🔥 ${a.tokens.toLocaleString()} tok</span>`);
  if (a.elapsed!=null)  chips.push(`<span class="stat-chip chip-time">⏱ ${a.elapsed}s</span>`);
  if (a.files_changed!=null) chips.push(`<span class="stat-chip chip-diff">📝 ${a.files_changed}f +${a.insertions??0} -${a.deletions??0}</span>`);
  if (a.tok_per_line!=null)  chips.push(`<span class="stat-chip chip-eff">⚡ ${a.tok_per_line} tok/ln</span>`);
  if (a.turns!=null)    chips.push(`<span class="stat-chip chip-turns">🔄 ${a.turns} calls</span>`);
  if (a.retries>0)      chips.push(`<span class="stat-chip chip-warn">↩ ${a.retries} retry</span>`);
  if (a.has_warning)    chips.push(`<span class="stat-chip chip-warn">⚠ no metadata</span>`);

  // ── Intent Trail（可解释性轨迹） ──
  const it = a.intent_trail || {};
  let trailHtml = '';
  if (it.task_goal || it.intents?.length || it.obstacles?.length || it.result) {
    const rows = [];
    if (it.task_goal)
      rows.push(`<div class="trail-row trail-goal">🎯 ${esc(it.task_goal.slice(0,120))}</div>`);
    (it.intents||[]).forEach(s =>
      rows.push(`<div class="trail-row trail-intent">→ ${esc(s.slice(0,100))}</div>`));
    (it.obstacles||[]).forEach(s =>
      rows.push(`<div class="trail-row trail-obstacle">⚠ ${esc(s.slice(0,100))}</div>`));
    (it.decisions||[]).forEach(s =>
      rows.push(`<div class="trail-row trail-decision">⑃ ${esc(s.slice(0,100))}</div>`));
    if (it.result)
      rows.push(`<div class="trail-row trail-result">✓ ${esc(it.result.slice(0,120))}</div>`);
    if (rows.length)
      trailHtml = `<div class="trail-box">${rows.join('')}</div>`;
  }

  return `<div class="card ${a.status}" id="card-${a.name}" onclick="openLog('${a.name}')">
    <div class="card-header">
      <span class="card-name">${esc(a.name)}</span>
      <span class="badge ${a.status}">${statusLabel[a.status]||a.status}</span>
    </div>
    <div class="card-meta">${esc(a.model)} · ${esc(time)}</div>
    <div class="progress-bar"><div class="progress-fill fill-${a.status}" style="width:${pct}%"></div></div>
    ${trailHtml || `<div class="log-box">${esc(a.last_line)}</div>`}
    <div class="stat-row">${chips.join('')}</div>
  </div>`;
}

// ── 顶栏 ──
function renderOverall(s, mode) {
  const idle = mode === 'idle' ? '<span class="badge idle">⏸ idle</span>' : '';
  return `<span>agents: <b>${s.total}</b></span>
    <span class="badge done">${s.done} done</span>
    <span class="badge running">${s.running} run</span>
    <span class="badge failed">${s.failed} fail</span>
    <span>🔥 ${s.total_tokens.toLocaleString()} tok</span>${idle}`;
}
function renderTimeSaved(s) {
  if (!s.time_saved || s.time_saved <= 0) return '';
  return `⚡ 并行节省 ${s.time_saved}s（串行需 ${s.serial_equiv}s，实际 ${s.actual_parallel}s）`;
}

// ── log 弹窗（三 tab：Timeline / Shells / Raw Log） ──
const TAG_COLORS = {
  TASK_GOAL:'#58a6ff', TASK_START:'#58a6ff', TASK_FILES:'#3fb950', TASK_CTX:'#3fb950',
  INTENT:'#c9d1d9', OBSTACLE:'#d29922', DECISION:'#a371f7', RESULT:'#3fb950',
  RETRY:'#f85149', RETRY_PLAN:'#f85149', CODEX_EXIT:'#8b949e', AUTO_COMMIT:'#8b949e',
  OUTCOME_DIFF:'#3fb950', OUTCOME_FILES:'#3fb950',
  AGENT_DONE:'#3fb950', TASK_FAILED:'#f85149',
};

function openLog(name) {
  const a = allAgents.find(x => x.name === name);
  if (!a) return;

  document.getElementById('log-modal-title').textContent = `${name}`;
  // raw log
  document.getElementById('log-modal-body').textContent = a.log || '(empty)';

  // timeline
  const it = a.intent_trail || {};
  const tl = it.timeline || [];
  const tlEl = document.getElementById('log-modal-timeline');
  if (tl.length) {
    tlEl.innerHTML = tl.map(e => {
      const color = TAG_COLORS[e.tag] || '#8b949e';
      const tagCls = `tag-${e.tag}` in document.styleSheets[0]?.cssRules ? `tag-${e.tag}` : 'tag-default';
      return `<div class="tl-event">
        <span class="tl-ts">${esc(e.ts||'')}</span>
        <span class="tl-dot" style="background:${color}"></span>
        <span class="tl-text">
          <span class="tl-tag tag-${e.tag}">${esc(e.tag)}</span>${esc(e.text)}
        </span>
      </div>`;
    }).join('');
  } else {
    tlEl.innerHTML = '<div style="color:#484f58;padding:20px">无可解释性标记（需使用新版 orchestrate.sh）</div>';
  }

  // shells with context
  const sc = it.shell_with_ctx || [];
  const shEl = document.getElementById('log-modal-shells');
  if (sc.length) {
    shEl.innerHTML = sc.map(s => `<div class="shell-item">
      <div class="shell-ctx">${s.context ? '📌 ' + esc(s.context) : '（无关联意图）'}</div>
      <div class="shell-cmd">$ ${esc(s.cmd)}</div>
    </div>`).join('');
  } else {
    shEl.innerHTML = '<div style="color:#484f58;padding:20px">无 shell 命令记录</div>';
  }

  switchTab('timeline');
  document.getElementById('log-modal').classList.add('open');
}

function switchTab(tab) {
  document.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('log-modal-body').style.display     = 'none';
  document.getElementById('log-modal-timeline').style.display = 'none';
  document.getElementById('log-modal-shells').style.display   = 'none';
  if (tab === 'raw') {
    document.getElementById('log-modal-body').style.display = 'flex';
    document.querySelector('[data-tab=raw]').classList.add('active');
  } else if (tab === 'shells') {
    document.getElementById('log-modal-shells').style.display = 'flex';
    document.querySelector('[data-tab=shells]').classList.add('active');
  } else {
    document.getElementById('log-modal-timeline').style.display = 'flex';
    document.querySelector('[data-tab=timeline]').classList.add('active');
  }
}

document.getElementById('log-modal-close').onclick = () =>
  document.getElementById('log-modal').classList.remove('open');
document.getElementById('log-modal').onclick = e => {
  if (e.target === document.getElementById('log-modal'))
    document.getElementById('log-modal').classList.remove('open');
};

// ── SSE ──
let es = null;
let lastMsgAt = Date.now();
const DEAD_TIMEOUT = 35000; // 35s 没收到任何消息则判定假死

// 定时检查假死
setInterval(() => {
  if (es && Date.now() - lastMsgAt > DEAD_TIMEOUT) {
    document.getElementById('conn-status').textContent = '⚠ heartbeat timeout, reconnecting…';
    connect();
  }
}, 5000);

function connect() {
  if (es) es.close();
  es = new EventSource('/events');
  lastMsgAt = Date.now();
  document.getElementById('conn-status').textContent = 'connected';

  es.onmessage = e => {
    lastMsgAt = Date.now();
    const data = JSON.parse(e.data);
    if (data.type === 'ping') {
      // 纯心跳，只更新时间戳，不渲染
      document.getElementById('conn-status').textContent = `✅ alive · ${new Date().toLocaleTimeString()}`;
      return;
    }
    if (data.type === 'agents') {
      allAgents = data.agents;
      document.getElementById('overall').innerHTML     = renderOverall(data.stats, data.mode);
      document.getElementById('time-saved').textContent = renderTimeSaved(data.stats);
      document.getElementById('idle-banner').style.display = data.mode==='idle' ? 'block' : 'none';
      renderGantt(data.agents);
      const c = document.getElementById('agents');
      data.agents.forEach(a => {
        const ex = document.getElementById('card-' + a.name);
        const html = renderCard(a);
        if (ex) ex.outerHTML = html; else c.insertAdjacentHTML('beforeend', html);
      });
    } else if (data.type === 'ai_waiting') {
      document.getElementById('ai-box').style.display = 'block';
      document.getElementById('ai-loading').style.display = 'block';
      document.getElementById('ai-content').style.display = 'none';
    } else if (data.type === 'ai_analysis') {
      document.getElementById('ai-box').style.display = 'block';
      document.getElementById('ai-loading').style.display = 'none';
      const ct = document.getElementById('ai-content');
      ct.style.display = 'block';
      ct.textContent = data.text;
    } else if (data.type === 'reload') {
      allAgents = [];
      document.getElementById('agents').innerHTML = '';
      document.getElementById('gantt-rows').innerHTML = '';
      document.getElementById('gantt').style.display = 'none';
      document.getElementById('ai-box').style.display = 'none';
      document.getElementById('idle-banner').style.display = 'none';
      document.getElementById('time-saved').textContent = '';
    }
  };
  es.onerror = () => {
    document.getElementById('conn-status').textContent = 'reconnecting…';
    setTimeout(connect, 3000);
  };
}
connect();
</script>
</body>
</html>"""

# ──────────────────────────────────────────────
# HTTP Handler
# ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/":
            self._send(200, "text/html", HTML.encode())
        elif self.path == "/events":
            self._sse()
        elif self.path == "/state":
            with STATE.lock:
                agents = [parse_log(p) for p in STATE.log_paths]
                stats  = collect_stats(agents)
            body = json.dumps({"agents": agents, "stats": stats, "mode": STATE.mode}).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send(400, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
            return

        if self.path == "/reload":
            paths = []
            for p in data.get("logs", []):
                expanded = glob.glob(p)
                paths.extend(expanded if expanded else [p])
            STATE.reload(paths)
            print(f"[dashboard] reload: {len(paths)} log(s)")
            self._send(200, "application/json", json.dumps({"ok": True, "logs": paths}).encode())

        elif self.path == "/register":
            log_path = data.get("log", "")
            if not log_path:
                self._send(400, "application/json", json.dumps({"ok": False, "error": "missing log"}).encode())
                return
            STATE.register(log_path)
            print(f"[dashboard] register: {log_path} (total={len(STATE.log_paths)})")
            self._send(200, "application/json",
                       json.dumps({"ok": True, "log": log_path, "total": len(STATE.log_paths)}).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def push(data: dict):
            msg = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(msg.encode())
            self.wfile.flush()

        last_mode  = None
        last_ping  = time.time()
        PING_INTERVAL = 15  # 秒，前端超过 35s 没收到任何消息则重连

        try:
            while True:
                with STATE.lock:
                    mode     = STATE.mode
                    paths    = list(STATE.log_paths)
                    ai_cache = dict(STATE.ai_cache)

                if last_mode == "idle" and mode == "active":
                    push({"type": "reload"})
                last_mode = mode

                agents = [parse_log(p) for p in paths]
                stats  = collect_stats(agents)
                push({"type": "agents", "agents": agents, "stats": stats, "mode": mode})

                if mode == "active" and stats["all_done"] and not STATE.ai_triggered:
                    with STATE.lock:
                        STATE.ai_triggered = True
                    push({"type": "ai_waiting"})
                    ai_analyze_async(agents)
                    STATE.set_idle()

                if "last" in ai_cache:
                    push({"type": "ai_analysis", "text": ai_cache["last"]})
                    with STATE.lock:
                        STATE.ai_cache.clear()

                # 心跳：每 PING_INTERVAL 秒推一次 ping，防止连接假死
                now = time.time()
                if now - last_ping >= PING_INTERVAL:
                    push({"type": "ping", "ts": int(now)})
                    last_ping = now

                time.sleep(2 if mode == "idle" else 1)

        except (BrokenPipeError, ConnectionResetError):
            pass

# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

def main():
    global STATE, LLM_CFG
    parser = argparse.ArgumentParser(description="worktree-codex 实时监控展板")
    parser.add_argument("--logs", nargs="*", default=[],
                        help="log 文件路径（可选；agent 启动时自动 POST /register 追加）")
    parser.add_argument("--port", type=int, default=7789)
    # LLM 配置（AI 分析模块，兼容任意 OpenAI Chat API 端点）
    parser.add_argument("--llm-base-url", default=None,
                        help="AI 分析用的 base URL，默认 http://YOUR_PROXY_HOST:PORT/v1（自建代理）")
    parser.add_argument("--llm-api-key", default=None,
                        help="AI 分析用的 API Key，默认从 ~/.profile 读 OPENAI_API_KEY")
    parser.add_argument("--llm-model", default="gemini-2.5-flash",
                        help="AI 分析用的模型，默认 gemini-2.5-flash；多模态任务用 qwen3-30b-vl")
    parser.add_argument("--no-ai", action="store_true",
                        help="禁用 AI 分析（任务完成后不调用 LLM）")
    args = parser.parse_args()

    # 解析 LLM 配置
    LLM_CFG = {
        "base_url": args.llm_base_url or "http://YOUR_PROXY_HOST:PORT/v1",
        "api_key":  args.llm_api_key,   # None = 运行时从 ~/.profile / openclaw.json fallback
        "model":    args.llm_model,
        "disabled": args.no_ai,
    }

    paths = []
    for p in (args.logs or []):
        expanded = glob.glob(p)
        paths.extend(expanded if expanded else [p])

    STATE = State(paths)
    print(f"[dashboard] 展板地址: http://localhost:{args.port}")
    print(f"[dashboard] 初始 log: {len(paths)} 个")
    if LLM_CFG["disabled"]:
        print("[dashboard] AI 分析: 已禁用 (--no-ai)")
    else:
        print(f"[dashboard] AI 分析: {LLM_CFG['model']} @ {LLM_CFG['base_url']}")

    URL = f"http://localhost:{args.port}"
    URL_FILE = "/tmp/wt-dashboard.url"

    # 写固定路径，让任何脚本/AI 都能 cat 到当前展板地址
    try:
        with open(URL_FILE, "w") as f:
            f.write(URL + "\n")
    except Exception:
        pass

    print(f"[dashboard] ✅ 展板已启动: {URL}")
    print(f"[dashboard] 地址文件:    {URL_FILE}  (cat 随时查)")

    server = ThreadingHTTPServer(("", args.port), Handler)
    try:
        server.serve_forever()
    finally:
        # 前台退出时清理 URL 文件
        try:
            import os as _os
            if open(URL_FILE).read().strip() == URL:
                _os.unlink(URL_FILE)
        except Exception:
            pass

if __name__ == "__main__":
    main()
