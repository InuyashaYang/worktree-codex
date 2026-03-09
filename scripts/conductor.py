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
    if not exports_m:
        return True, []

    symbols = []
    for line in exports_m.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            parts = line[2:].split()
            if parts:
                symbols.append(parts[-1])

    if not symbols:
        return True, []

    failures = []
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

    return (len(failures) == 0), failures


# ─────────────────────────────────────────────────────────────────────
# 状态文件（供 dashboard.py 读取）
# ─────────────────────────────────────────────────────────────────────

STATE_FILE = "/tmp/wt-conductor-state.json"


def write_state_file(
    agents_cfg: List[Dict],
    status_map: Dict[str, str],
    max_slots: int,
    running_set: Set[str],
    waiting_set: Set[str],
    passed: Set[str],
    failed: Set[str],
) -> None:
    """写 conductor 状态到 /tmp/wt-conductor-state.json，dashboard.py SSE 帧附带读取"""
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
        "updated_at": time.time(),
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
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

        self.agents_map: Dict[str, Dict] = {a["name"]: a for a in self.agents_cfg}

        # 状态映射：name → "waiting"|"running"|"verifying"|"pass"|"inject"|"fail"
        self.status: Dict[str, str] = {a["name"]: "waiting" for a in self.agents_cfg}

        self.passed: Set[str] = set()   # 已验证通过
        self.failed: Set[str] = set()   # 永久失败

        # slot 管理：name → subprocess.Popen
        self.running_procs: Dict[str, subprocess.Popen] = {}

        # log 文件读取基线（行数），用于检测新的 AGENT_DONE
        self.log_baselines: Dict[str, int] = {}

        # 每个 agent 的追问轮次
        self.agent_rounds: Dict[str, int] = {a["name"]: 0 for a in self.agents_cfg}

        self.conductor_log = "/tmp/wt-conductor.log"
        self.conductor_report = "/tmp/wt-conductor-report.txt"
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
        write_state_file(
            agents_cfg=self.agents_cfg,
            status_map=self.status,
            max_slots=self.max_slots,
            running_set=running_set,
            waiting_set=waiting_set,
            passed=self.passed,
            failed=self.failed,
        )

    # ── AGENT_DONE 检测 ───────────────────────────────────────────────

    def _check_agent_done(self, name: str) -> bool:
        """检查 log 中 baseline 之后是否出现新的 AGENT_DONE。检测到则更新 baseline。"""
        a = self.agents_map[name]
        log_path = a.get("log", f"/tmp/wt-{name}.log")
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
        inject_file = f"/tmp/wt-inject-{name}.txt"
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
        inject_file = f"/tmp/wt-inject-{name}.txt"
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
        log_path = a.get("log", f"/tmp/wt-{name}.log")
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
            base_url_key,            "http://152.53.52.170:3003/v1",
        )
        model_key = "CODEX_MODEL"
        env[model_key] = self.codex_model or env.get(model_key, "gpt-5.3-codex")
        bin_key = "CODEX_BIN"
        env[bin_key] = self.codex_bin or env.get(bin_key, "")
        env["DASHBOARD_PORT"] = str(self.dashboard_port)
        # 把 prompt 通过环境变量传入，避免 shell 转义问题
        env["WT_TASK_PROMPT"] = prompt

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

        slot_num = len(self.running_procs) + 1
        self._emit(
            "SLOT_ALLOC",
            f"agent={name} slot={slot_num} total_running={slot_num}",
        )

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
            self.running_procs[name] = proc
            self.status[name] = "running"
            self.agent_rounds[name] += 1
            self._emit(
                "AGENT_STARTED",
                f"agent={name} pid={proc.pid} round={self.agent_rounds[name]}",
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
            if proc:
                slot_num = len(self.running_procs) + 1
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
            self._emit("INJECT", f"agent={name} round={round_num} reason={reason[:200]}")
            self._write_inject(name, all_failures, round_num)
            self.status[name] = "inject"
            self._emit(
                "SLOT_FREE",
                f"agent={name} reason=inject total_running={len(self.running_procs)} (slot保持占用等待追问)",
            )

        self._write_state()

    # ── 检查 agent 是否满足启动条件 ───────────────────────────────────

    def _can_start(self, name: str) -> bool:
        """检查 agent 的所有依赖是否已 pass"""
        a = self.agents_map[name]
        for dep in (a.get("depends") or []):
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
                    break  # 按拓扑顺序，这一个卡住了就不再检查后面的
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

        # ── 生成报告 ──────────────────────────────────────────────────
        elapsed = round(time.time() - self.start_time, 1)
        pass_count = len(self.passed)
        fail_count = len(self.failed)
        total_rounds = max(self.agent_rounds.values()) if self.agent_rounds else 0

        self._emit(
            "CONDUCTOR_DONE",
            f"pass={pass_count} fail={fail_count} rounds={total_rounds} elapsed={elapsed}s",
        )

        # 写报告文件
        try:
            with open(self.conductor_report, "w", encoding="utf-8") as f:
                f.write("# Conductor 最终报告\n")
                f.write(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"仓库: {self.repo}\n")
                f.write(f"CONTRACT: {self.contract}\n")
                f.write(f"总轮数: {total_rounds}  耗时: {elapsed}s\n\n")
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
