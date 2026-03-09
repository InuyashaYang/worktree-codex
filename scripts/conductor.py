#!/usr/bin/env python3
"""
worktree-codex/scripts/conductor.py
异步事件循环调度器 — DAG + slot 池 + 上游注入

调度模型：
  - 事件循环（主线程，每 poll_interval 秒）
  - Kahn 算法 BFS 拓扑排序确定启动顺序
  - slot 池限制最大并发数
  - 每个 agent 完成后立即验证（不等其他人）
  - 验证通过：释放 slot，解锁 DAG 下游
  - 验证失败：写 inject 文件，保持 slot 占用（等待追问完成）
  - 下游 prompt 中 {{agent-name}} 占位符 → 替换为上游实际文件内容摘要

用法:
  python3 conductor.py --tasks tasks.yaml [选项]
  python3 conductor.py --tasks tasks.yaml --max-slots 3 --poll-interval 5

Python 3.8+，无第三方依赖（只用标准库）。
YAML 解析：优先 PyYAML，无则用内置 mini-parser。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ─────────────────────────────────────────────────────────────────────
# 内置 YAML mini-parser
# 支持：顶层 dict、嵌套 dict（缩进）、list（- item）、
#       行内 list [a,b]、多行字符串块 |、数字/bool/null 自动转换、注释
# ─────────────────────────────────────────────────────────────────────

def _yaml_scalar(s: str) -> Any:
    s = s.strip()
    if s in ("true", "True", "yes", "Yes"):
        return True
    if s in ("false", "False", "no", "No"):
        return False
    if s in ("null", "Null", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def _yaml_inline_list(s: str) -> List:
    inner = s.strip()[1:-1]
    return [_yaml_scalar(x) for x in inner.split(",") if x.strip()]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_yaml_block(lines: List[Tuple[int, str]], pos: int, base: int) -> Tuple[Any, int]:
    """
    从 pos 开始解析缩进 > base 的 YAML 块。
    lines: [(indent, stripped_content), ...]
    返回 (value, next_pos)
    """
    if pos >= len(lines):
        return None, pos

    cur_ind, cur_c = lines[pos]
    if cur_ind <= base:
        return None, pos

    # 判断是 list 还是 dict
    if cur_c.startswith("- ") or cur_c == "-":
        # ── list ──
        result = []
        while pos < len(lines):
            ind, c = lines[pos]
            if ind < cur_ind:
                break
            if ind > cur_ind:
                pos += 1
                continue
            if not (c.startswith("- ") or c == "-"):
                break
            item_raw = c[1:].strip() if c != "-" else ""
            if not item_raw:
                # 值在下一行
                pos += 1
                sub, pos = _parse_yaml_block(lines, pos, ind)
                result.append(sub)
                continue
            if item_raw.startswith("[") and item_raw.endswith("]"):
                result.append(_yaml_inline_list(item_raw))
                pos += 1
                continue
            if ":" in item_raw and not item_raw.startswith('"'):
                # list item 是 dict（"- key: val"）
                sub_dict: Dict[str, Any] = {}
                _parse_kv_into(item_raw, sub_dict, lines, pos, ind)
                pos += 1
                # 继续读同一 list item 内缩进更深的字段
                while pos < len(lines):
                    si, sc = lines[pos]
                    if si <= ind:
                        break
                    _parse_kv_into(sc, sub_dict, lines, pos, si - 1)
                    pos += 1
                result.append(sub_dict)
                continue
            result.append(_yaml_scalar(item_raw))
            pos += 1
        return result, pos
    else:
        # ── dict ──
        result_d: Dict[str, Any] = {}
        while pos < len(lines):
            ind, c = lines[pos]
            if ind < cur_ind:
                break
            if ind > cur_ind:
                pos += 1
                continue
            if c.startswith("- "):
                break
            if ":" not in c:
                pos += 1
                continue
            key, _, v_rest = c.partition(":")
            key = key.strip()
            v = v_rest.strip()
            if v == "|":
                # 多行字符串块
                pos += 1
                block_lines = []
                block_ind = None
                while pos < len(lines):
                    bi, bl = lines[pos]
                    if block_ind is None:
                        block_ind = bi
                    if bi < block_ind:
                        break
                    block_lines.append(bl)
                    pos += 1
                result_d[key] = "\n".join(block_lines)
                continue
            if v.startswith("[") and v.endswith("]"):
                result_d[key] = _yaml_inline_list(v)
                pos += 1
                continue
            if not v:
                # 值在下一行
                pos += 1
                sub, pos = _parse_yaml_block(lines, pos, ind)
                result_d[key] = sub
                continue
            result_d[key] = _yaml_scalar(v)
            pos += 1
        return result_d, pos


def _parse_kv_into(c: str, d: Dict, lines: List, pos: int, base: int) -> None:
    """把 'key: val' 解析进字典 d（原地修改，不处理子块）"""
    if ":" not in c:
        return
    key, _, v_rest = c.partition(":")
    key = key.strip()
    v = v_rest.strip()
    if v == "|":
        return  # 多行块由调用方处理
    if v.startswith("[") and v.endswith("]"):
        d[key] = _yaml_inline_list(v)
    elif v:
        d[key] = _yaml_scalar(v)
    else:
        d[key] = None


def load_yaml_mini(text: str) -> Any:
    """内置 YAML mini-parser 入口"""
    processed: List[Tuple[int, str]] = []
    for raw_line in text.splitlines():
        stripped = raw_line.rstrip()
        content = stripped.strip()
        if not content or content.startswith("#"):
            continue
        processed.append((_indent(stripped), content))
    val, _ = _parse_yaml_block(processed, 0, -1)
    return val


def load_config(path: str) -> Dict:
    """
    加载 tasks.yaml（或 tasks.json）配置文件。
    优先 PyYAML，无则内置 mini-parser，.json 用 json 模块。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    text = p.read_text(encoding="utf-8")
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    result = load_yaml_mini(text)
    if result is None:
        raise ValueError(f"无法解析配置文件: {path}")
    return result


# ─────────────────────────────────────────────────────────────────────
# DAG 拓扑排序（Kahn 算法 BFS）
# ─────────────────────────────────────────────────────────────────────

def topo_sort(agents: List[Dict]) -> List[str]:
    """
    Kahn 算法拓扑排序：
    1. 计算每个节点入度
    2. 入度 0 的节点入队
    3. 取出节点，其子节点入度减 1，入度降为 0 则入队
    4. 结果数 != 总数 → 存在循环依赖
    """
    names: Set[str] = {a["name"] for a in agents}
    in_degree: Dict[str, int] = {a["name"]: 0 for a in agents}
    children: Dict[str, List[str]] = defaultdict(list)

    for a in agents:
        for dep in (a.get("depends") or []):
            if dep not in names:
                raise ValueError(
                    f"Agent '{a['name']}' 依赖了不存在的 agent: '{dep}'\n"
                    f"已知 agent: {sorted(names)}"
                )
            children[dep].append(a["name"])
            in_degree[a["name"]] += 1

    queue: deque = deque(n for n, d in in_degree.items() if d == 0)
    order: List[str] = []

    while queue:
        node = queue.popleft()
        order.append(node)
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(agents):
        cycle_nodes = [n for n, d in in_degree.items() if d > 0]
        raise ValueError(
            f"检测到循环依赖！涉及节点: {cycle_nodes}\n"
            "请检查 tasks.yaml 中的 depends 字段。"
        )
    return order


# ─────────────────────────────────────────────────────────────────────
# 上游文件摘要（{{agent-name}} 占位符注入）
# ─────────────────────────────────────────────────────────────────────

def build_upstream_summary(agent_cfg: Dict, repo: str) -> str:
    """读取上游 agent 的实际产出文件（前150行），生成摘要字符串"""
    agent_name = agent_cfg["name"]
    worktree = agent_cfg.get("worktree", agent_cfg.get("worktree_path", ""))
    files = agent_cfg.get("files", [])

    lines_out = [f"=== {agent_name} 实际产出（已通过验证）==="]
    for fname in files:
        fname = fname.strip()
        candidates = []
        if worktree:
            candidates.append(os.path.join(worktree, fname))
        if repo:
            candidates.append(os.path.join(repo, fname))

        content = None
        for fpath in candidates:
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        content = "".join(f.readlines()[:150])
                    break
                except Exception:
                    pass
        lines_out.append(f"--- {fname} ---")
        lines_out.append(content if content is not None else "(文件不存在或无法读取)")

    return "\n".join(lines_out)


def inject_upstream_placeholders(
    prompt: str,
    agent_cfg: Dict,
    agents_map: Dict[str, Dict],
    repo: str,
    passed: Set[str],
) -> str:
    """将 prompt 中的 {{dep-name}} 替换为上游 agent 的实际文件摘要"""
    for dep_name in (agent_cfg.get("depends") or []):
        placeholder = "{{" + dep_name + "}}"
        if placeholder not in prompt:
            continue
        if dep_name not in passed:
            prompt = prompt.replace(
                placeholder,
                f"(警告: {dep_name} 尚未通过验证，内容不可用)"
            )
            continue
        dep_cfg = agents_map.get(dep_name, {})
        summary = build_upstream_summary(dep_cfg, repo)
        prompt = prompt.replace(placeholder, summary)
    return prompt


# ─────────────────────────────────────────────────────────────────────
# 语法检查
# ─────────────────────────────────────────────────────────────────────

_SKIP_EXTS = {".css", ".html", ".md", ".txt", ".yaml", ".yml", ".toml", ".json"}


def syntax_check_files(
    worktree: str,
    files: List[str],
    syntax_cmd: str,
) -> Tuple[bool, List[str]]:
    """
    对 worktree 中的文件运行语法检查命令。
    返回 (全部通过, 失败详情列表)
    """
    if not syntax_cmd:
        return True, []

    failures = []
    for fname in files:
        fname = fname.strip()
        if os.path.splitext(fname)[1].lower() in _SKIP_EXTS:
            continue
        fpath = os.path.join(worktree, fname)
        if not os.path.isfile(fpath):
            failures.append(f"MISSING_FILE: {fname}")
            continue
        try:
            r = subprocess.run(
                f"{syntax_cmd} {fpath}",
                shell=True, capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                out = (r.stdout + r.stderr).strip()[:200]
                failures.append(f"SYNTAX_FAIL: {fname} — {out}")
        except subprocess.TimeoutExpired:
            failures.append(f"SYNTAX_TIMEOUT: {fname}")
        except Exception as e:
            failures.append(f"SYNTAX_ERROR: {fname} — {e}")

    return (len(failures) == 0), failures


# ─────────────────────────────────────────────────────────────────────
# CONTRACT 验证
# ─────────────────────────────────────────────────────────────────────

def contract_check(
    agent_name: str,
    worktree: str,
    files: List[str],
    contract_file: str,
) -> Tuple[bool, List[str]]:
    """
    从 CONTRACT.md 提取该 agent 的 Exports 符号，验证在文件中存在。
    返回 (全部通过, 失败详情列表)
    """
    if not contract_file or not os.path.isfile(contract_file):
        return True, []
    try:
        ct = Path(contract_file).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return True, []

    block_m = re.search(
        r"^## Agent:\s*" + re.escape(agent_name) + r"\s*$", ct, re.MULTILINE
    )
    if not block_m:
        return True, []

    block_start = block_m.end()
    next_m = re.search(r"^## Agent:", ct[block_start:], re.MULTILINE)
    block = ct[block_start: block_start + next_m.start() if next_m else len(ct)]

    exports_m = re.search(r"^Exports:\s*\n((?:^- .*\n?)*)", block, re.MULTILINE)
    symbols = []
    if exports_m:
        for line in exports_m.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                parts = line[2:].split()
                if parts:
                    symbols.append(parts[-1])

    # CSS Classes 验证（class 名需出现在 .css/.html 文件中）
    css_m = re.search(r"^CSS Classes[^:]*:\s*\n((?:^- .*\n?)*)", block, re.MULTILINE)
    css_classes = []
    if css_m:
        for line in css_m.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                cls = line[2:].strip()
                if cls:
                    css_classes.append(cls)

    if not symbols and not css_classes:
        return True, []

    failures = []

    # 验证 JS exports
    for sym in symbols:
        found = False
        for fname in files:
            fpath = os.path.join(worktree, fname.strip())
            if not os.path.isfile(fpath):
                continue
            try:
                if sym in Path(fpath).read_text(encoding="utf-8", errors="replace"):
                    found = True
                    break
            except Exception:
                pass
        if not found:
            failures.append(f"CONTRACT_FAIL: {sym} (未在 {files} 中找到)")

    # 验证 CSS classes（在 .css 或 .html 文件里查找 .classname 或 class="classname"）
    css_files = [f for f in files if f.endswith(".css") or f.endswith(".html")]
    for cls in css_classes:
        found = False
        for fname in css_files:
            fpath = os.path.join(worktree, fname.strip())
            if not os.path.isfile(fpath):
                continue
            try:
                content = Path(fpath).read_text(encoding="utf-8", errors="replace")
                # 匹配 .classname { 或 class="... classname ..."
                if (f".{cls}" in content or f'"{cls}"' in content
                        or f"'{cls}'" in content or f" {cls} " in content):
                    found = True
                    break
            except Exception:
                pass
        if not found and css_files:
            failures.append(f"CSS_FAIL: .{cls} (未在 {css_files} 中找到)")

    return (len(failures) == 0), failures


# ─────────────────────────────────────────────────────────────────────
# 状态文件（供 dashboard.py 读取）
# ─────────────────────────────────────────────────────────────────────

STATE_FILE = "/tmp/wt-conductor-state.json"   # 默认值，实际由 Conductor 实例覆盖


def write_state_file(
    agents_cfg: List[Dict],
    status_map: Dict[str, str],
    max_slots: int,
    running_set: Set[str],
    waiting_set: Set[str],
    passed: Set[str],
    failed: Set[str],
    await_human: Optional[str] = None,
    paused: bool = False,
    state_file: str = STATE_FILE,   # per-project 路径，由 Conductor 传入
) -> None:
    """写 conductor 状态到状态文件，dashboard.py SSE 帧附带读取"""
    nodes = [
        {
            "id": a["name"],
            "status": status_map.get(a["name"], "waiting"),
            "depends": a.get("depends") or [],
        }
        for a in agents_cfg
    ]
    edges = [
        [dep, a["name"]]
        for a in agents_cfg
        for dep in (a.get("depends") or [])
    ]
    state = {
        "slots": {
            "max": max_slots,
            "running": sorted(running_set),
            "waiting": sorted(waiting_set),
            "done": sorted(passed),
            "failed": sorted(failed),
        },
        "dag": {"nodes": nodes, "edges": edges},
        "await_human": await_human,
        "paused": paused,
        "updated_at": time.time(),
    }
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# Conductor 主类
# ─────────────────────────────────────────────────────────────────────

class Conductor:
    """
    异步事件循环调度器。

    agent 状态机：
      waiting   → running   (slot 空闲 + 依赖 pass → fork orchestrate.sh)
      running   → verifying (检测到 AGENT_DONE)
      verifying → pass      (验证通过 → 释放 slot，解锁下游)
      verifying → inject    (验证失败 → 写 inject 文件，保持 slot)
      inject    → running   (inject 被消费 → 等待下一次 AGENT_DONE)
      pass / fail → terminal
    """

    def __init__(
        self,
        tasks: Dict,
        max_slots: int = 5,
        poll_interval: int = 8,
        dashboard_port: int = 7789,
        openai_api_key: str = "",
        openai_base_url: str = "",
        codex_model: str = "gpt-5.3-codex",
        codex_bin: str = "",
        script_dir: str = "",
    ):
        self.agents_cfg: List[Dict] = tasks.get("agents", [])
        self.repo: str = tasks.get("repo", "")
        self.contract: str = tasks.get("contract", "")
        self.worktrees_base: str = tasks.get("worktrees_base", "")
        self.syntax_check_cmd: str = tasks.get("syntax_check", "")
        # tasks.yaml 里的 max_slots 优先于命令行参数
        tasks_max_slots = tasks.get("max_slots")
        self.max_slots: int = int(tasks_max_slots) if tasks_max_slots else max_slots
        self.poll_interval: int = poll_interval
        self.dashboard_port: int = int(tasks.get("dashboard_port") or dashboard_port)
        self.openai_api_key: str = openai_api_key
        self.openai_base_url: str = openai_base_url
        self.codex_model: str = codex_model
        self.codex_bin: str = codex_bin or os.path.expanduser("~/.npm-global/bin/codex")
        self.script_dir: str = script_dir
        self.notify_sh: str = os.path.join(self.script_dir, "notify.sh")
        self.gateway_port: str = str(tasks.get("gateway_port") or os.environ.get("WT_GATEWAY_PORT", "3000"))
        self.session_key: str = tasks.get("session_key") or os.environ.get("WT_SESSION_KEY", "agent:main:main")

        self.agents_map: Dict[str, Dict] = {a["name"]: a for a in self.agents_cfg}

        # 状态映射：name → "waiting"|"running"|"verifying"|"pass"|"inject"|"fail"
        self.status: Dict[str, str] = {a["name"]: "waiting" for a in self.agents_cfg}

        self.passed: Set[str] = set()   # 已验证通过
        self.failed: Set[str] = set()   # 永久失败

        # slot 管理：name → subprocess.Popen
        self.running_procs: Dict[str, subprocess.Popen] = {}

        # log 文件读取基线（行数），用于检测新的 AGENT_DONE
        self.log_baselines: Dict[str, int] = {}

        # 每个 agent 的追问轮次（inject 轮次，不含初始第1轮）
        self.agent_rounds: Dict[str, int] = {a["name"]: 0 for a in self.agents_cfg}

        # inject 上限：超过则标 fail 释放 slot，防止 DAG 卡死
        # tasks.yaml 可用 max_inject_rounds 覆盖
        self.max_inject_rounds: int = int(tasks.get("max_inject_rounds") or 3)

        self.llm_model: str = tasks.get("llm_model") or "gpt-4.1-mini"

        self.await_human: Optional[str] = None
        self.paused: bool = False

        # ── run_id 命名空间：所有 IPC 文件集中在一个目录 ─────────────
        _proj = os.path.basename(self.repo.rstrip("/"))
        _ts   = time.strftime("%Y%m%d-%H%M%S")
        self.run_id   = f"{_proj}-{_ts}"
        self.run_dir  = f"/tmp/wt/{self.run_id}"
        os.makedirs(self.run_dir, exist_ok=True)

        self.conductor_log        = f"{self.run_dir}/conductor.log"
        self.conductor_report     = f"{self.run_dir}/conductor-report.txt"
        self.conductor_state_file = f"{self.run_dir}/conductor-state.json"
        self.human_input_file     = f"{self.run_dir}/human-input.json"
        self.inject_prefix        = f"{self.run_dir}/inject-"   # + agentname + .txt

        # 写 run_id 文件让外部工具（dashboard、shell）能找到最新 run
        _latest = "/tmp/wt/latest-run-id"
        os.makedirs("/tmp/wt", exist_ok=True)
        with open(_latest, "w") as _f:
            _f.write(self.run_id)

        self.start_time = time.time()
        self.topo_order: List[str] = []

    # ── 日志 ─────────────────────────────────────────────────────────

    def _emit(self, tag: str, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"##{tag}## [{ts}] {msg}"
        print(line, flush=True)
        try:
            with open(self.conductor_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        try:
            with open(self.conductor_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ── 状态快照 ─────────────────────────────────────────────────────

    def _write_state(self) -> None:
        running_set = set(self.running_procs.keys())
        waiting_set = {
            n for n, s in self.status.items()
            if s == "waiting"
        }
        kwargs = dict(
            agents_cfg=self.agents_cfg,
            status_map=self.status,
            max_slots=self.max_slots,
            running_set=running_set,
            waiting_set=waiting_set,
            passed=self.passed,
            failed=self.failed,
            await_human=self.await_human,
            paused=self.paused,
        )
        # per-project 独立状态文件（防多项目冲突）
        write_state_file(**kwargs, state_file=self.conductor_state_file)
        # 同时写兼容路径（dashboard 默认读这个）
        write_state_file(**kwargs, state_file=STATE_FILE)

    # ── AGENT_DONE 检测 ───────────────────────────────────────────────

    def _check_agent_done(self, name: str) -> bool:
        """检查 log 中 baseline 之后是否出现新的 AGENT_DONE。检测到则更新 baseline。"""
        a = self.agents_map[name]
        log_path = a.get("log") or f"{self.run_dir}/agent-{name}.log"
        if not os.path.isfile(log_path):
            return False
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception:
            return False
        baseline = self.log_baselines.get(name, 0)
        for line in all_lines[baseline:]:
            if "AGENT_DONE" in line:
                self.log_baselines[name] = len(all_lines)
                return True
        return False

    # ── inject 消费检测 ───────────────────────────────────────────────

    def _inject_consumed(self, name: str) -> bool:
        """文件不存在或为空 = 已被 orchestrate.sh 消费"""
        inject_file = f"{self.inject_prefix}{name}.txt"
        if not os.path.isfile(inject_file):
            return True
        try:
            return os.path.getsize(inject_file) == 0
        except Exception:
            return True

    # ── 写 inject 文件 ────────────────────────────────────────────────

    def _write_inject(self, name: str, failures: List[str], round_num: int) -> None:
        """生成追问内容并写入 /tmp/wt-inject-<name>.txt"""
        a = self.agents_map[name]
        files = a.get("files", [])
        worktree = a.get("worktree", a.get("worktree_path", ""))

        # 当前文件摘要（前60行）
        file_preview_parts = []
        for fname in files:
            fname = fname.strip()
            candidates = []
            if worktree:
                candidates.append(os.path.join(worktree, fname))
            if self.repo:
                candidates.append(os.path.join(self.repo, fname))
            content = None
            for fp in candidates:
                if os.path.isfile(fp):
                    try:
                        with open(fp, encoding="utf-8", errors="replace") as f:
                            content = "".join(f.readlines()[:60])
                        break
                    except Exception:
                        pass
            file_preview_parts.append(
                f"\n=== {fname} (前60行) ===\n" + (content or "(文件不存在)")
            )
        file_preview = "".join(file_preview_parts)

        # CONTRACT 要求摘要
        contract_section = ""
        if self.contract and os.path.isfile(self.contract):
            try:
                ct = Path(self.contract).read_text(encoding="utf-8", errors="replace")
                block_m = re.search(
                    r"^## Agent:\s*" + re.escape(name) + r"\s*$", ct, re.MULTILINE
                )
                if block_m:
                    block_start = block_m.start()
                    next_m = re.search(r"^## Agent:", ct[block_start + 1:], re.MULTILINE)
                    block_end = block_start + 1 + next_m.start() if next_m else len(ct)
                    contract_section = ct[block_start:block_end].strip()
            except Exception:
                pass

        failure_text = "\n".join(failures) if failures else "(无具体失败记录)"

        inject_content = f"""[Conductor 追问 — 第 {round_num} 轮]

集成验证发现你的输出存在问题，请修复后重新 commit。

## 验证失败详情
{failure_text}

## CONTRACT 要求
{contract_section or '(无 CONTRACT 定义)'}

## 当前文件内容摘要
{file_preview}

## 任务要求
1. 修复上述所有失败项
2. 确保文件语法正确，可独立加载
3. 完成后执行: git add -A && git commit -m "[{name}] fix: conductor-round-{round_num}"
4. 输出 ##RESULT## 标记说明本次修复内容

请现在开始修复。
"""
        inject_file = f"{self.inject_prefix}{name}.txt"
        try:
            with open(inject_file, "w", encoding="utf-8") as f:
                f.write(inject_content)
        except Exception as e:
            self._log(f"警告: 无法写 inject 文件 {inject_file}: {e}")

    # ── fork orchestrate.sh ───────────────────────────────────────────

    def _fork_agent(self, name: str) -> None:
        """
        fork orchestrate.sh 启动 agent。
        注入上游占位符后，将 prompt 通过环境变量传入（避免 shell 转义问题）。
        """
        a = self.agents_map[name]
        worktree = a.get("worktree", a.get("worktree_path", ""))
        branch = a.get("branch", f"feature/{name}")
        log_path = a.get("log") or f"{self.run_dir}/agent-{name}.log"
        prompt = a.get("prompt", "")

        # 替换上游占位符
        prompt = inject_upstream_placeholders(
            prompt, a, self.agents_map, self.repo, self.passed
        )

        orchestrate = os.path.join(self.script_dir, "orchestrate.sh")
        if not os.path.isfile(orchestrate):
            # fallback: 找 scripts/orchestrate.sh
            orchestrate = os.path.join(
                os.path.dirname(self.script_dir), "scripts", "orchestrate.sh"
            )
        if not os.path.isfile(orchestrate):
            self._emit("ERROR", f"找不到 orchestrate.sh: {orchestrate}")
            self.status[name] = "fail"
            self.failed.add(name)
            return

        env = os.environ.copy()
        env["OPENAI_API_KEY"] = self.openai_api_key or env.get("OPENAI_API_KEY", "")
        base_url_key = "OPENAI_BASE_URL"
        env[base_url_key] = self.openai_base_url or env.get(
            base_url_key, "http://152.53.52.170:3003/v1",
        )
        model_key = "CODEX_MODEL"
        env[model_key] = self.codex_model or env.get(model_key, "gpt-5.3-codex")
        bin_key = "CODEX_BIN"
        env[bin_key] = self.codex_bin or env.get(bin_key, "")
        env["DASHBOARD_PORT"] = str(self.dashboard_port)
        env["WT_TASK_PROMPT"] = prompt
        # run_dir 命名空间：orchestrate.sh 用这个派生 inject/agent-log 路径
        env["WT_RUN_DIR"] = self.run_dir

        # 准备日志目录
        os.makedirs(os.path.dirname(log_path) if os.path.dirname(log_path) else ".", exist_ok=True)

        cmd = [
            "bash", orchestrate,
            self.repo,
            name,
            worktree,
            branch,
            "${WT_TASK_PROMPT}",
            log_path,
        ]
        # 实际执行时用 bash -c 展开环境变量
        bash_cmd = (
            f'bash "{orchestrate}" '
            f'"{self.repo}" "{name}" "{worktree}" "{branch}" '
            f'"$WT_TASK_PROMPT" "{log_path}"'
        )

        self._log(f"  fork: {bash_cmd[:120]}...")

        # 初始化 log 基线（当前行数，以便后续只检测新行）
        if os.path.isfile(log_path):
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    self.log_baselines[name] = sum(1 for _ in f)
            except Exception:
                self.log_baselines[name] = 0
        else:
            self.log_baselines[name] = 0

        try:
            proc = subprocess.Popen(
                bash_cmd,
                shell=True,
                env=env,
            )
            self.running_procs[name] = proc   # 先 append
            self.status[name] = "running"
            self.agent_rounds[name] += 1
            slot_num = len(self.running_procs)  # append 之后计数才准
            self._emit(
                "SLOT_ALLOC",
                f"agent={name} slot={slot_num} total_running={slot_num} pid={proc.pid} round={self.agent_rounds[name]}",
            )
        except Exception as e:
            self._emit("ERROR", f"fork 失败 agent={name}: {e}")
            self.status[name] = "fail"
            self.failed.add(name)

    # ── 验证 agent ─────────────────────────────────────────────────────

    def _verify_agent(self, name: str) -> None:
        """
        对 agent 做语法检查 + CONTRACT 验证。
        通过 → 标记 pass，释放 slot，解锁下游。
        失败 → 写 inject 文件，状态变 inject，保持 slot 占用。
        """
        a = self.agents_map[name]
        files = a.get("files", [])
        worktree = a.get("worktree", a.get("worktree_path", ""))
        round_num = self.agent_rounds[name]

        self._emit("AGENT_DONE_EVENT", f"agent={name} round={round_num}")

        # 语法检查
        syntax_ok, syntax_fails = syntax_check_files(
            worktree, files, self.syntax_check_cmd
        )
        # CONTRACT 验证
        contract_ok, contract_fails = contract_check(
            name, worktree, files, self.contract
        )

        s_label = "ok" if syntax_ok else "fail"
        c_label = "ok" if contract_ok else "fail"
        self._emit("VERIFY", f"agent={name} syntax={s_label} contract={c_label}")

        all_failures = syntax_fails + contract_fails

        if syntax_ok and contract_ok:
            # ── 验证通过 ──
            self.status[name] = "pass"
            self.passed.add(name)
            # 释放 slot
            proc = self.running_procs.pop(name, None)
            if proc is not None:
                self._emit(
                    "SLOT_FREE",
                    f"agent={name} reason=pass total_running={len(self.running_procs)}",
                )
            # 解锁下游
            unlocked = [
                a2["name"]
                for a2 in self.agents_cfg
                if name in (a2.get("depends") or [])
            ]
            if unlocked:
                self._emit("UNLOCK", f"agent={name} unlocked={','.join(unlocked)}")
        else:
            # ── 验证失败 ──
            reason = "; ".join(all_failures[:3])
            # 检查 inject 轮次上限，防止 agent 无限追问占用 slot 卡死 DAG
            if round_num >= self.max_inject_rounds:
                # 超限 → 发 AWAIT_HUMAN，暂停等待人类指令
                self.await_human = f"agent={name} 已追问 {round_num} 轮仍未通过（{reason[:120]}），请指示"
                self.status[name] = "inject"  # 保持 inject 状态，不标 fail
                self._emit("AWAIT_HUMAN", self.await_human)
                self._notify("await_human", self.await_human)
                self._write_state()
                # 不释放 slot，不 pop running_procs，保持现状等人类决策
            else:
                self._emit("INJECT", f"agent={name} round={round_num} reason={reason[:200]}")
                self._write_inject(name, all_failures, round_num)
                self.status[name] = "inject"
                self._emit(
                    "SLOT_FREE",
                    f"agent={name} reason=inject total_running={len(self.running_procs)} (slot保持占用等待追问)",
                )

        self._write_state()

    # ── 检查 agent 是否满足启动条件 ───────────────────────────────────

    def _merge_to_main(self) -> None:
        """
        精准合并：把每个 pass agent 的目标文件从 worktree checkout 到 main。
        不做 git merge（避免共享文件冲突），只精准复制目标文件。
        完成后在 main 做一次 commit。
        """
        import shutil

        self._emit("MERGE_START", f"agents={','.join(sorted(self.passed))}")
        repo = self.repo
        merged_files: List[str] = []
        errors: List[str] = []

        for name in self.topo_order:
            if name not in self.passed:
                continue
            a = self.agents_map[name]
            worktree = a.get("worktree", "")
            files: List[str] = a.get("files") or []
            if not worktree or not files:
                continue
            for fname in files:
                src = os.path.join(worktree, fname)
                dst = os.path.join(repo, fname)
                if not os.path.isfile(src):
                    errors.append(f"{name}/{fname}: 文件不存在")
                    continue
                # 确保目标目录存在
                os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) else None
                try:
                    shutil.copy2(src, dst)
                    merged_files.append(fname)
                except Exception as e:
                    errors.append(f"{name}/{fname}: 复制失败 {e}")

        if errors:
            for err in errors:
                self._emit("MERGE_WARN", err)

        if not merged_files:
            self._emit("MERGE_SKIP", "没有文件需要合并")
            return

        # 在 main 做一次 commit
        try:
            agent_list = ",".join(sorted(self.passed))
            result = subprocess.run(
                f"cd {repo!r} && git add -A && "
                f"git commit -m 'merge: {agent_list}' --no-verify",
                shell=True, capture_output=True, text=True,
            )
            if result.returncode == 0:
                self._emit("MERGE_DONE", f"files={','.join(merged_files)} commit=ok")
            else:
                # 可能 nothing to commit（文件内容和 main 一致）
                self._emit("MERGE_DONE", f"files={','.join(merged_files)} commit=nothing_to_commit")
        except Exception as e:
            self._emit("MERGE_ERROR", f"commit 失败: {e}")

    # ── 事件回报（OpenClaw 生态集成）────────────────────────────────

    def _notify(self, event: str, message: str) -> None:
        """非阻塞调用 notify.sh，失败静默"""
        if not os.path.isfile(self.notify_sh):
            return
        try:
            env = os.environ.copy()
            env["WT_GATEWAY_PORT"] = self.gateway_port
            env["WT_SESSION_KEY"] = self.session_key
            subprocess.Popen(
                ["bash", self.notify_sh, event, message],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log(f"notify 失败（静默）: {e}")

    # ── 人类介入 ──────────────────────────────────────────────────────

    def _handle_human_input(self) -> None:
        try:
            with open(self.human_input_file, encoding="utf-8") as f:
                data = json.load(f)
            os.remove(self.human_input_file)
        except Exception as e:
            self._emit("ERROR", f"读取 human_input 失败: {e}")
            return

        text = data.get("text", "").strip()
        if not text:
            return

        self._emit("HUMAN_INPUT", f"text={text[:100]}")

        # 用 LLM 解读意图
        intent = self._interpret_intent(text)
        self._emit("HUMAN_INTENT", f"intent={intent.get('action')} target={intent.get('target','*')}")

        action = intent.get("action", "unknown")
        target = intent.get("target")   # agent name 或 None（全局）

        if action == "pause":
            self.paused = True
            self.await_human = None
            self._emit("PAUSED", "用户暂停")
        elif action == "resume":
            self.paused = False
            self.await_human = None
            self._emit("RESUMED", "用户恢复")
        elif action == "skip" and target and target in self.status:
            # 强制 pass 目标 agent，解锁下游
            self.status[target] = "pass"
            self.passed.add(target)
            self.running_procs.pop(target, None)
            self.await_human = None
            unlocked = [a["name"] for a in self.agents_cfg
                        if target in (a.get("depends") or []) and self.status[a["name"]] == "waiting"]
            self._emit("SKIP", f"agent={target} → force_pass, unlocked={','.join(unlocked)}")
        elif action == "retry" and target and target in self.status:
            # 重置 agent 状态，重新 fork
            self.status[target] = "waiting"
            self.passed.discard(target)
            self.running_procs.pop(target, None)
            self.agent_rounds[target] = 0
            self.await_human = None
            self._emit("RETRY", f"agent={target} → reset to waiting")
        elif action == "fail" and target and target in self.status:
            self.status[target] = "fail"
            self.failed.add(target)
            self.running_procs.pop(target, None)
            self.await_human = None
            self._emit("FORCE_FAIL", f"agent={target} → force fail")
        elif action == "extend" and target and target in self.status:
            self.max_inject_rounds += 2
            self.await_human = None
            self._emit("EXTEND", f"agent={target} max_inject_rounds → {self.max_inject_rounds}")
        else:
            self._emit("HUMAN_UNRECOGNIZED", f"无法执行: {text[:80]}")

        self._write_state()

    def _interpret_intent(self, text: str) -> Dict:
        if not self.openai_api_key or not self.openai_base_url:
            return self._keyword_intent(text)

        agent_names = list(self.agents_map.keys())
        status_summary = {n: self.status[n] for n in agent_names}

        system_prompt = """你是多Agent编程任务的调度助手。用户会用自然语言描述对当前任务的指令。
你需要输出一个 JSON 对象（不要 markdown）：
{"action": "pause|resume|skip|retry|fail|extend|unknown", "target": "agent名称或null", "reason": "一句话解释"}

action 含义：
- pause: 暂停调度
- resume: 恢复调度
- skip: 强制某agent通过（跳过验证，解锁下游）
- retry: 重置某agent并重试
- fail: 强制某agent失败
- extend: 给某agent延长追问次数(+2轮)
- unknown: 无法识别

只输出 JSON。"""

        user_prompt = f"当前状态：{json.dumps(status_summary, ensure_ascii=False)}\n可用agent：{agent_names}\n用户指令：{text}"

        try:
            import urllib.request as _ur
            payload = json.dumps({"model": self.llm_model, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ], "temperature": 0.1}).encode()
            req = _ur.Request(
                self.openai_base_url.rstrip("/") + "/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.openai_api_key}"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
            content = body["choices"][0]["message"]["content"]
            content = re.sub(r"```[a-z]*\n?", "", content).strip()
            return json.loads(content)
        except Exception as e:
            self._emit("ERROR", f"LLM intent 解析失败: {e}")
            return self._keyword_intent(text)

    def _keyword_intent(self, text: str) -> Dict:
        t = text.lower()
        agent_names = list(self.agents_map.keys())
        target = next((n for n in agent_names if n in t), None)
        if any(w in t for w in ["暂停", "pause", "停一下", "停止"]):
            return {"action": "pause", "target": None, "reason": "关键词"}
        if any(w in t for w in ["恢复", "resume", "继续"]):
            return {"action": "resume", "target": None, "reason": "关键词"}
        if any(w in t for w in ["跳过", "skip", "放行", "强制pass"]):
            return {"action": "skip", "target": target, "reason": "关键词"}
        if any(w in t for w in ["重试", "retry", "重来", "重新"]):
            return {"action": "retry", "target": target, "reason": "关键词"}
        if any(w in t for w in ["失败", "fail", "放弃"]):
            return {"action": "fail", "target": target, "reason": "关键词"}
        if any(w in t for w in ["延长", "extend", "多试", "多几轮"]):
            return {"action": "extend", "target": target, "reason": "关键词"}
        return {"action": "unknown", "target": None, "reason": "无法识别"}

    # ── 检查 agent 是否满足启动条件 ───────────────────────────────────

    def _can_start(self, name: str) -> bool:
        """检查 agent 的所有依赖是否已 pass；若有上游 fail 则直接传播 fail"""
        a = self.agents_map[name]
        for dep in (a.get("depends") or []):
            if dep in self.failed:
                # 上游 fail → 下游不可能启动，立即传播
                self._emit("ERROR", f"agent={name} 上游依赖 {dep} 已失败，跳过")
                self.status[name] = "fail"
                self.failed.add(name)
                self._notify("agent_fail", f"agent {name} 因上游 {dep} 失败而跳过")
                return False
            if dep not in self.passed:
                return False
        return True

    # ── 主事件循环 ────────────────────────────────────────────────────

    def run(self) -> int:
        """
        事件循环主入口。返回 0（全部 pass）或 1（有 fail）。

        每次循环（每 poll_interval 秒）：
          1. 检查 running agents 的 log，有 AGENT_DONE → 立即验证
          2. 检查 inject agents，inject 被消费 → 等待再次 AGENT_DONE
          3. 对 waiting tasks（拓扑顺序），依赖满足 + slot 空闲 → fork
          4. 所有 tasks 终态（pass/fail）→ 退出
        """
        if not self.agents_cfg:
            self._log("错误: tasks.yaml 中没有任何 agent 配置")
            return 1

        # DAG 拓扑排序（验证依赖合法性）
        try:
            self.topo_order = topo_sort(self.agents_cfg)
        except ValueError as e:
            self._log(f"DAG 错误: {e}")
            return 1

        n_agents = len(self.agents_cfg)
        self._emit(
            "CONDUCTOR_START",
            f"max_slots={self.max_slots} agents={n_agents} topo_order={self.topo_order}",
        )

        self._write_state()

        while True:
            # ── 检查全局暂停 ──────────────────────────────────────────────
            if self.paused and not self.await_human:
                self._write_state()
                time.sleep(self.poll_interval)
                continue

            # ── 检查人类输入 ──────────────────────────────────────────────
            if os.path.isfile(self.human_input_file):
                self._handle_human_input()

            # ── Step 1: 检查 running agents ────────────────────────────
            for name in list(self.running_procs.keys()):
                if self.status[name] != "running":
                    continue
                # 检查进程是否已退出（不强制依赖 AGENT_DONE，进程退出也触发验证）
                proc = self.running_procs[name]
                proc_done = proc.poll() is not None

                agent_done_in_log = self._check_agent_done(name)

                if agent_done_in_log or proc_done:
                    self.status[name] = "verifying"
                    self._verify_agent(name)

            # ── Step 2: 检查 inject agents ──────────────────────────────
            for name, st in list(self.status.items()):
                if st != "inject":
                    continue
                if self._inject_consumed(name):
                    # inject 被消费，agent 已收到追问在跑了
                    self.status[name] = "running"
                    self._log(f"inject 已消费，等待 {name} 再次完成...")
                    # 如果进程已结束（不太可能，但防御一下），再次检查
                    proc = self.running_procs.get(name)
                    if proc and proc.poll() is not None:
                        if self._check_agent_done(name):
                            self.status[name] = "verifying"
                            self._verify_agent(name)

            # ── Step 3: 启动满足条件的 waiting agents ───────────────────
            for name in self.topo_order:
                if self.status[name] != "waiting":
                    continue
                if not self._can_start(name):
                    continue
                if len(self.running_procs) >= self.max_slots:
                    self._log(
                        f"slot 已满 ({len(self.running_procs)}/{self.max_slots})，"
                        f"{name} 排队等待..."
                    )
                    continue  # 不 break：继续检查后续 agent，避免误挡无依赖关系的任务
                self._fork_agent(name)
                self._write_state()

            # ── Step 4: 检查是否全部终态 ────────────────────────────────
            terminal = self.passed | self.failed
            if len(terminal) == n_agents:
                break

            # 安全检查：如果没有 running/inject/waiting，但还有未完成的 → 卡死
            active = {n for n, s in self.status.items()
                      if s in ("running", "verifying", "inject", "waiting")}
            if not active:
                self._log("警告: 没有活跃 agent，但还有未完成任务，可能存在 DAG 问题")
                # 把剩余 waiting 的标记为 fail
                for name, st in self.status.items():
                    if st == "waiting":
                        self.status[name] = "fail"
                        self.failed.add(name)
                        self._emit("ERROR", f"agent={name} 卡死（依赖无法满足）")
                break

            self._write_state()
            time.sleep(self.poll_interval)

        # ── 精准文件合并到 main ────────────────────────────────────────
        if self.passed:
            self._merge_to_main()

        # ── 生成报告 ──────────────────────────────────────────────────
        elapsed = round(time.time() - self.start_time, 1)
        pass_count = len(self.passed)
        fail_count = len(self.failed)
        # 各 agent 追问轮次汇总（每个 agent 独立计）
        max_rounds = max(self.agent_rounds.values()) if self.agent_rounds else 0
        total_rounds_sum = sum(self.agent_rounds.values())

        self._emit(
            "CONDUCTOR_DONE",
            f"pass={pass_count} fail={fail_count} max_rounds={max_rounds} total_inject_rounds={total_rounds_sum} elapsed={elapsed}s",
        )

        # OpenClaw 生态回报
        self._notify("done", f"项目 {os.path.basename(self.repo)} 完成！{pass_count}/{n_agents} 通过，耗时 {elapsed}s。产出已合并到 main 分支。")
        if fail_count > 0:
            self._notify("agent_fail", f"项目 {os.path.basename(self.repo)} 完成，但 {fail_count} 个 agent 失败。详见展板 http://localhost:{self.dashboard_port}")

        # 写报告文件
        try:
            with open(self.conductor_report, "w", encoding="utf-8") as f:
                f.write("# Conductor 最终报告\n")
                f.write(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"仓库: {self.repo}\n")
                f.write(f"CONTRACT: {self.contract}\n")
                f.write(f"总追问轮: {total_rounds_sum} (最多单 agent {max_rounds} 轮)  耗时: {elapsed}s\n\n")
                f.write("## Agent 状态汇总\n")
                f.write(f"通过: {pass_count}  失败: {fail_count}  总计: {n_agents}\n\n")
                f.write("| Agent | 状态 | 追问轮次 |\n")
                f.write("|-------|------|----------|\n")
                for a in self.agents_cfg:
                    n = a["name"]
                    st = self.status[n]
                    rounds = self.agent_rounds[n]
                    f.write(f"| {n} | {st} | {rounds} |\n")
        except Exception as e:
            self._log(f"写报告失败: {e}")

        self._log(f"最终报告: {self.conductor_report}")
        self._log(f"Conductor 日志: {self.conductor_log}")

        print("\n" + "="*60)
        print(f"  Conductor 完成")
        print(f"  通过: {pass_count} / {n_agents}    失败: {fail_count} / {n_agents}")
        print(f"  耗时: {elapsed}s")
        print(f"  报告: {self.conductor_report}")
        print("="*60)

        return 0 if fail_count == 0 else 1


# ─────────────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="worktree-codex conductor — 异步事件循环 DAG 调度器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 conductor.py --tasks tasks.yaml
  python3 conductor.py --tasks tasks.yaml --max-slots 3 --poll-interval 5

tasks.yaml 格式:
  max_slots: 5
  repo: /path/to/repo
  contract: /path/to/CONTRACT.md
  worktrees_base: /path/to/worktrees
  syntax_check: "node --check"
  agents:
    - name: agent-logic
      files: [logic.js]
      depends: []
      log: /tmp/agent-logic.log
      branch: feature/agent-logic
      worktree: /path/to/worktrees/agent-logic
      prompt: |
        你是 agent-logic，负责实现 logic.js。
""",
    )
    parser.add_argument("--tasks", required=True,
                        help="tasks.yaml（或 tasks.json）路径")
    parser.add_argument("--max-slots", type=int, default=5,
                        help="最大并发 slot 数（默认 5，tasks.yaml 中可覆盖）")
    parser.add_argument("--poll-interval", type=int, default=8,
                        help="事件循环轮询间隔秒数（默认 8）")
    parser.add_argument("--dashboard-port", type=int, default=7789,
                        help="展板端口（默认 7789）")
    parser.add_argument("--openai-api-key", default="",
                        help="API Key（默认从环境变量继承）")
    parser.add_argument("--openai-base-url", default="",
                        help="API Base URL（默认从环境变量继承）")
    parser.add_argument("--codex-model", default="gpt-5.3-codex",
                        help="Codex 模型名（默认 gpt-5.3-codex）")
    parser.add_argument("--codex-bin", default="",
                        help="codex 可执行文件路径（默认 ~/.npm-global/bin/codex）")
    args = parser.parse_args()

    # 加载配置
    try:
        tasks = load_config(args.tasks)
    except (FileNotFoundError, ValueError) as e:
        print(f"配置加载失败: {e}", file=sys.stderr)
        return 1

    # script_dir = tasks.yaml 所在目录（用于查找 orchestrate.sh）
    script_dir = os.path.dirname(os.path.abspath(args.tasks))
    # 如果 tasks.yaml 在根目录，orchestrate.sh 通常在 scripts/ 子目录
    if not os.path.isfile(os.path.join(script_dir, "orchestrate.sh")):
        alt = os.path.join(script_dir, "scripts", "orchestrate.sh")
        if os.path.isfile(alt):
            script_dir = os.path.join(script_dir, "scripts")
        else:
            # conductor.py 自身目录
            self_dir = os.path.dirname(os.path.abspath(__file__))
            if os.path.isfile(os.path.join(self_dir, "orchestrate.sh")):
                script_dir = self_dir

    conductor = Conductor(
        tasks=tasks,
        max_slots=args.max_slots,
        poll_interval=args.poll_interval,
        dashboard_port=args.dashboard_port,
        openai_api_key=args.openai_api_key,
        openai_base_url=args.openai_base_url,
        codex_model=args.codex_model,
        codex_bin=args.codex_bin,
        script_dir=script_dir,
    )
    return conductor.run()


if __name__ == "__main__":
    sys.exit(main())
