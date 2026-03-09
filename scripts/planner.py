#!/usr/bin/env python3
"""
planner.py — 一句话描述 → 自动规划 DAG → 生成 tasks.yaml + CONTRACT.md → 可选直接执行

用法：
  python3 scripts/planner.py \
    --desc "做一个 Markdown 笔记本" \
    --repo /path/to/repo \
    [--agents 3] \
    [--tech "ES module, 无第三方库"] \
    [--max-slots 5] \
    [--openai-api-key sk-xxx] \
    [--openai-base-url http://BASE/v1] \
    [--model gpt-4.1-mini] \
    [--codex-model gpt-5.3-codex] \
    [--codex-bin ~/.npm-global/bin/codex] \
    [--dashboard-port 7789] \
    [--dry-run] \
    [--yes] \
    [--output-dir /path]
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────
# LLM
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个多 Agent 编程任务规划器。
用户会描述一个前端 Web 应用，你需要输出一个 JSON 规划方案，将任务分解为 2-6 个 Agent。

【核心原则：最大化并行】
DAG 层数越少越好。能并行的 Agent 绝不串行。典型结构是两层：
- 第一层（无依赖，全部并行）：logic.js / storage.js / parser.js / style.css 等
- 第二层（依赖所有第一层）：index.html + ui.js（集成层）
只有当 B 必须读取 A 的实际代码才能工作时，B 才依赖 A。
"B 和 A 共享数据格式" 不等于 B 依赖 A，数据格式写进 CONTRACT 即可。

规则：
1. 每个文件只能属于一个 agent（所有 agent 的 files 列表无重叠）
2. style.css 单独为一个 agent，不依赖任何人（并行第一层）
3. 集成 UI agent（index.html + 主 JS）依赖所有其他 agent，在最后一层
4. 有 depends 的 agent 的 prompt 必须包含 {{dep-name}} 占位符，例如依赖 agent-logic 则写 {{agent-logic}}
5. {{dep-name}} 会在运行时被替换为上游 agent 的实际产出文件摘要（前150行）
6. global_data_format 描述跨 agent 共享的数据结构，用于防止接口不匹配
7. 所有 agent 的 name 必须以 "agent-" 开头
8. 输出纯 JSON，不要 markdown 代码块，不要任何解释文字

输出格式（严格遵守）：
{
  "project_name": "string",
  "description": "string",
  "tech_stack": "string",
  "global_data_format": "string（描述跨 agent 的核心数据结构，markdown 格式）",
  "agents": [
    {
      "name": "agent-xxx",
      "files": ["file.js"],
      "depends": [],
      "role": "一句话角色描述",
      "exports": ["export function foo()", "export class Bar"],
      "css_classes": [],
      "prompt": "完整的 agent 任务 prompt（多行字符串，包含所有必要细节）"
    }
  ]
}"""


def call_llm(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict],
    attempt: int = 1,
) -> str:
    """Call /chat/completions and return the assistant message content."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    print(f"  [planner] 调用 LLM（第 {attempt} 次）model={model} …", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}") from e

    # Extract content
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"响应格式异常: {body}") from e


def llm_plan(
    base_url: str,
    api_key: str,
    model: str,
    desc: str,
    n_agents: int,
    tech: str,
    max_retries: int = 2,
) -> Dict:
    """Ask LLM to produce a planning JSON, with retry on validation failure."""
    user_prompt = f"描述：{desc}\n期望 agent 数量：{n_agents}\n技术栈约束：{tech or '无特殊要求'}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    last_error: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        if last_error and attempt > 1:
            # Append error feedback for retry
            messages.append({
                "role": "assistant",
                "content": "(上次输出有误，见下方错误)",
            })
            messages.append({
                "role": "user",
                "content": f"上次输出验证失败，错误信息：{last_error}\n请修正后重新输出纯 JSON。",
            })

        raw = call_llm(base_url, api_key, model, messages, attempt=attempt)

        # Strip possible markdown fences
        raw_stripped = raw.strip()
        if raw_stripped.startswith("```"):
            lines = raw_stripped.splitlines()
            # Remove first and last fence lines
            inner = []
            for i, line in enumerate(lines):
                if i == 0 and line.startswith("```"):
                    continue
                if i == len(lines) - 1 and line.startswith("```"):
                    continue
                inner.append(line)
            raw_stripped = "\n".join(inner)

        try:
            plan = json.loads(raw_stripped)
        except json.JSONDecodeError as e:
            last_error = f"JSON 解析失败: {e}\n原始内容（前500字）: {raw_stripped[:500]}"
            print(f"  [planner] ⚠ {last_error}", file=sys.stderr)
            if attempt < max_retries:
                continue
            else:
                sys.exit(f"[planner] LLM 返回非 JSON，已重试 {max_retries} 次，退出。")

        try:
            validate_plan(plan)
            return plan
        except ValueError as e:
            last_error = str(e)
            print(f"  [planner] ⚠ 验证失败: {last_error}", file=sys.stderr)
            if attempt < max_retries:
                continue
            else:
                sys.exit(f"[planner] 规划验证失败，已重试 {max_retries} 次，退出。错误: {last_error}")

    # Should not reach here
    sys.exit("[planner] 意外退出。")


# ──────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────

def validate_plan(plan: Dict) -> None:
    """Validate the planning JSON. Raise ValueError on failure."""
    agents = plan.get("agents")
    if not isinstance(agents, list) or len(agents) < 2:
        raise ValueError("agents 必须是包含至少 2 个元素的列表")

    names = {a["name"] for a in agents}

    # All names must start with "agent-"
    for a in agents:
        if not a.get("name", "").startswith("agent-"):
            raise ValueError(f"agent name 必须以 'agent-' 开头: {a.get('name')}")

    # All files across agents must be unique
    all_files: List[str] = []
    for a in agents:
        files = a.get("files", [])
        for f in files:
            if f in all_files:
                raise ValueError(f"文件重叠：{f} 出现在多个 agent 中")
            all_files.append(f)

    # All depends must reference existing agents
    for a in agents:
        for dep in a.get("depends", []):
            if dep not in names:
                raise ValueError(f"agent '{a['name']}' 的 depends 引用了不存在的 agent: {dep}")

    # DAG must be acyclic
    _check_dag_acyclic(agents)

    # Agents with depends must have placeholder in prompt
    for a in agents:
        prompt = a.get("prompt", "")
        for dep in a.get("depends", []):
            placeholder = "{{" + dep + "}}"
            if placeholder not in prompt:
                raise ValueError(
                    f"agent '{a['name']}' 依赖 '{dep}'，但 prompt 中缺少占位符 {placeholder}"
                )


def _check_dag_acyclic(agents: List[Dict]) -> None:
    """DFS cycle detection. Raise ValueError if cycle found."""
    graph: Dict[str, List[str]] = {a["name"]: a.get("depends", []) for a in agents}
    # 0=unvisited, 1=in-stack, 2=done
    state: Dict[str, int] = {name: 0 for name in graph}

    def dfs(node: str, path: List[str]) -> None:
        state[node] = 1
        for dep in graph.get(node, []):
            if state[dep] == 1:
                cycle = " → ".join(path + [dep])
                raise ValueError(f"DAG 中存在环: {cycle}")
            if state[dep] == 0:
                dfs(dep, path + [node])
        state[node] = 2

    for name in graph:
        if state[name] == 0:
            dfs(name, [])


# ──────────────────────────────────────────────
# Topological sort & DAG info
# ──────────────────────────────────────────────

def topological_sort(agents: List[Dict]) -> List[Dict]:
    """Kahn's algorithm topological sort."""
    graph: Dict[str, List[str]] = {a["name"]: list(a.get("depends", [])) for a in agents}
    agent_map = {a["name"]: a for a in agents}
    in_degree: Dict[str, int] = {name: 0 for name in graph}
    # dependents: reverse map
    dependents: Dict[str, List[str]] = {name: [] for name in graph}
    for name, deps in graph.items():
        for dep in deps:
            dependents[dep].append(name)
            in_degree[name] += 1

    queue = [name for name, deg in in_degree.items() if deg == 0]
    queue.sort()  # deterministic
    result = []
    while queue:
        node = queue.pop(0)
        result.append(agent_map[node])
        for child in sorted(dependents[node]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)
    return result


def compute_dag_layers(agents: List[Dict]) -> int:
    """Compute number of layers in the DAG."""
    graph: Dict[str, List[str]] = {a["name"]: list(a.get("depends", [])) for a in agents}
    layer: Dict[str, int] = {}

    def get_layer(name: str) -> int:
        if name in layer:
            return layer[name]
        deps = graph.get(name, [])
        if not deps:
            layer[name] = 1
        else:
            layer[name] = max(get_layer(d) for d in deps) + 1
        return layer[name]

    for a in agents:
        get_layer(a["name"])
    return max(layer.values()) if layer else 1


def compute_dag_slots(agents: List[Dict]) -> Dict[str, int]:
    """Assign display slots in topological order."""
    sorted_agents = topological_sort(agents)
    return {a["name"]: i + 1 for i, a in enumerate(sorted_agents)}


# ──────────────────────────────────────────────
# Setup worktrees
# ──────────────────────────────────────────────

def run_setup_worktrees(
    setup_sh: str,
    repo: str,
    worktrees_base: str,
    agent_names: List[str],
) -> Dict[str, Tuple[str, str]]:
    """
    Run setup_worktrees.sh and parse output.
    Returns dict: agent_name -> (worktree_path, branch)
    """
    cmd = ["bash", setup_sh, repo, worktrees_base] + agent_names
    print(f"  [planner] 运行 setup_worktrees.sh …", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(
            f"[planner] setup_worktrees.sh 失败 (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    # Parse lines: name:worktree_path:branch
    mapping: Dict[str, Tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if len(parts) == 3:
            name, worktree_path, branch = parts
            mapping[name.strip()] = (worktree_path.strip(), branch.strip())

    # Fallback: if no output parsed, generate synthetic paths
    if not mapping:
        print(
            "  [planner] ⚠ setup_worktrees.sh 输出无法解析，使用推导路径。",
            file=sys.stderr,
        )
        for name in agent_names:
            mapping[name] = (
                os.path.join(worktrees_base, name),
                f"wt/{name}",
            )

    return mapping


# ──────────────────────────────────────────────
# File generation
# ──────────────────────────────────────────────

def generate_contract_md(plan: Dict, output_dir: str) -> str:
    """Generate CONTRACT.md and return its path."""
    project_name = plan["project_name"]
    global_data_format = plan.get("global_data_format", "")
    agents = plan["agents"]

    lines = [
        f"# CONTRACT.md — {project_name}",
        "",
        "## Global: DataFormat:",
        global_data_format,
        "",
    ]

    for a in agents:
        name = a["name"]
        files = ", ".join(a.get("files", []))
        exports = a.get("exports", [])
        css_classes = a.get("css_classes", [])

        lines.append(f"## Agent: {name}")
        lines.append(f"Files: {files}")
        if exports:
            lines.append("Exports:")
            for exp in exports:
                lines.append(f"- {exp}")
        if css_classes:
            lines.append("CSS Classes（如有）:")
            for cls in css_classes:
                lines.append(f"- {cls}")
        lines.append("")

    content = "\n".join(lines)
    path = os.path.join(output_dir, "CONTRACT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def generate_tasks_yaml(
    plan: Dict,
    repo: str,
    worktrees_base: str,
    worktree_map: Optional[Dict[str, Tuple[str, str]]],
    output_dir: str,
    max_slots: int,
    dashboard_port: int,
    contract_path: str,
) -> str:
    """Generate tasks.yaml and return its path."""
    agents = plan["agents"]

    def yaml_str(s: str) -> str:
        """Wrap a string value safely for YAML."""
        if "\n" in s or '"' in s or "'" in s or ":" in s or "#" in s or s.startswith("{"):
            # Use block scalar — handled inline for prompt
            return f'"{s}"'
        return s

    def yaml_list(items: List[str]) -> str:
        if not items:
            return "[]"
        return "\n" + "\n".join(f"      - {item}" for item in items)

    lines = [
        f"max_slots: {max_slots}",
        f"repo: {repo}",
        f"contract: {contract_path}",
        f"worktrees_base: {worktrees_base}",
        'syntax_check: "node --check"',
        f"dashboard_port: {dashboard_port}",
        "",
        "agents:",
    ]

    for a in agents:
        name = a["name"]
        files = a.get("files", [])
        depends = a.get("depends", [])
        prompt = a.get("prompt", "")

        if worktree_map and name in worktree_map:
            worktree_path, branch = worktree_map[name]
        else:
            worktree_path = os.path.join(worktrees_base, name)
            branch = f"wt/{name}"

        lines.append(f"  - name: {name}")

        # files list
        if files:
            lines.append("    files:")
            for f in files:
                lines.append(f"      - {f}")
        else:
            lines.append("    files: []")

        # depends list
        if depends:
            lines.append("    depends:")
            for d in depends:
                lines.append(f"      - {d}")
        else:
            lines.append("    depends: []")

        lines.append(f"    log: /tmp/wt-{name}.log")
        lines.append(f"    branch: {branch}")
        lines.append(f"    worktree: {worktree_path}")

        # prompt as block scalar
        lines.append("    prompt: |")
        for pline in prompt.splitlines():
            lines.append(f"      {pline}")
        lines.append("")

    content = "\n".join(lines)
    path = os.path.join(output_dir, "tasks.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ──────────────────────────────────────────────
# Summary / preview
# ──────────────────────────────────────────────

def print_summary(
    plan: Dict,
    desc: str,
    sorted_agents: List[Dict],
    dag_layers: int,
    slots: Dict[str, int],
    contract_path: str,
    tasks_path: str,
) -> None:
    n = len(sorted_agents)
    project_name = plan["project_name"]
    W = 60

    print()
    print("╔" + "═" * W + "╗")
    print(f"║  Planner 规划完成")
    print(f"║  项目：{project_name}")
    print(f"║  描述：{desc}")
    print(f"║  Agent 数：{n}  DAG 层数：{dag_layers}")
    print("╚" + "═" * W + "╝")
    print()

    print("Agent 列表（拓扑顺序）：")
    for a in sorted_agents:
        name = a["name"]
        files_str = ", ".join(a.get("files", []))
        deps_str = str(a.get("depends", []))
        slot = slots[name]
        print(f"  slot {slot:<2}  {name:<20}→ files: {files_str:<25} depends: {deps_str}")

    print()
    print("DAG 结构：")
    _print_dag(sorted_agents)

    print()
    print(f"CONTRACT.md → {contract_path}")
    print(f"tasks.yaml  → {tasks_path}")
    print()


def _print_dag(sorted_agents: List[Dict]) -> None:
    """Print a simple ASCII DAG."""
    # Find root nodes (no depends) and how they connect to dependents
    agent_map = {a["name"]: a for a in sorted_agents}
    # Build reverse map: who depends on me
    dependents: Dict[str, List[str]] = {a["name"]: [] for a in sorted_agents}
    for a in sorted_agents:
        for dep in a.get("depends", []):
            dependents[dep].append(a["name"])

    printed: set = set()
    for a in sorted_agents:
        name = a["name"]
        deps = dependents.get(name, [])
        if not a.get("depends"):
            if deps:
                if len(deps) == 1:
                    print(f"  {name} ──→ {deps[0]}")
                else:
                    for i, dep in enumerate(deps):
                        if i == 0:
                            print(f"  {name} ──┐")
                        elif i == len(deps) - 1:
                            print(f"  {'':>{len(name)}}    └──→ {dep}")
                        else:
                            print(f"  {'':>{len(name)}}    ├──→ {dep}")
            else:
                print(f"  {name} (standalone)")
            printed.add(name)

    # Print anything not yet shown
    for a in sorted_agents:
        name = a["name"]
        if name not in printed:
            deps_str = ", ".join(a.get("depends", []))
            print(f"  [{deps_str}] ──→ {name}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="planner.py — 一句话描述自动规划 DAG，生成 tasks.yaml + CONTRACT.md"
    )
    parser.add_argument("--desc", required=True, help="项目一句话描述")
    parser.add_argument("--repo", required=True, help="目标 repo 路径")
    parser.add_argument("--agents", type=int, default=3, help="期望 agent 数量（2-6，默认 3）")
    parser.add_argument("--tech", default="", help="技术栈约束（可选）")
    parser.add_argument("--max-slots", type=int, default=4, help="并发 slot 数（透传 conductor）")
    parser.add_argument("--openai-api-key", default=None, help="OpenAI API Key")
    parser.add_argument("--openai-base-url", default=None, help="API Base URL")
    parser.add_argument("--model", default="gpt-4.1-mini", help="Planner 用的 LLM 模型")
    parser.add_argument("--codex-model", default=None, help="Codex agent 用的模型（传给 conductor）")
    parser.add_argument("--codex-bin", default=None, help="Codex 可执行文件路径")
    parser.add_argument("--dashboard-port", type=int, default=7789, help="Dashboard 端口")
    parser.add_argument("--dry-run", action="store_true", help="只生成文件，不执行")
    parser.add_argument("--yes", action="store_true", help="跳过确认直接执行")
    parser.add_argument("--output-dir", default=None, help="输出目录（默认 --repo）")
    return parser.parse_args()


def resolve_env(args: argparse.Namespace) -> Tuple[str, str]:
    """Resolve API key and base URL from args or environment."""
    api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = args.openai_base_url or os.environ.get(
        "OPENAI_BASE_URL", "https://api.openai.com/v1"
    )
    if not api_key:
        sys.exit("[planner] 缺少 API Key，请通过 --openai-api-key 或 OPENAI_API_KEY 提供。")
    return api_key, base_url


def main() -> None:
    args = parse_args()

    # Validate agents range
    if not (2 <= args.agents <= 6):
        sys.exit(f"[planner] --agents 必须在 2-6 范围内，当前值: {args.agents}")

    # Resolve paths
    repo = os.path.abspath(args.repo)
    output_dir = os.path.abspath(args.output_dir) if args.output_dir else repo
    worktrees_base = repo + "-worktrees"

    # Script directory (where planner.py lives)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    setup_worktrees_sh = os.path.join(script_dir, "setup_worktrees.sh")
    conductor_py = os.path.join(script_dir, "conductor.py")
    launch_sh = os.path.join(script_dir, "launch.sh")

    # Resolve API credentials
    api_key, base_url = resolve_env(args)

    print(f"\n[planner] 开始规划 → 项目描述: {args.desc}")
    print(f"[planner] repo={repo}  output_dir={output_dir}  model={args.model}")

    # ── Step 1 & 2: Call LLM + validate ──
    plan = llm_plan(
        base_url=base_url,
        api_key=api_key,
        model=args.model,
        desc=args.desc,
        n_agents=args.agents,
        tech=args.tech,
    )

    sorted_agents = topological_sort(plan["agents"])
    dag_layers = compute_dag_layers(plan["agents"])
    slots = compute_dag_slots(plan["agents"])
    agent_names = [a["name"] for a in sorted_agents]

    # ── Step 3: Setup worktrees (skip in dry-run) ──
    worktree_map: Optional[Dict[str, Tuple[str, str]]] = None
    if not args.dry_run:
        if not os.path.exists(repo):
            sys.exit(f"[planner] repo 路径不存在: {repo}")
        if not os.path.exists(setup_worktrees_sh):
            print(
                f"  [planner] ⚠ setup_worktrees.sh 不存在: {setup_worktrees_sh}，跳过 worktree 创建。",
                file=sys.stderr,
            )
        else:
            worktree_map = run_setup_worktrees(
                setup_worktrees_sh, repo, worktrees_base, agent_names
            )

    # ── Step 4: Generate files ──
    os.makedirs(output_dir, exist_ok=True)

    contract_path = generate_contract_md(plan, output_dir)
    tasks_path = generate_tasks_yaml(
        plan=plan,
        repo=repo,
        worktrees_base=worktrees_base,
        worktree_map=worktree_map,
        output_dir=output_dir,
        max_slots=args.max_slots,
        dashboard_port=args.dashboard_port,
        contract_path=contract_path,
    )

    print(f"  [planner] ✓ CONTRACT.md → {contract_path}")
    print(f"  [planner] ✓ tasks.yaml  → {tasks_path}")

    if args.dry_run:
        # Print file contents for inspection
        print("\n── CONTRACT.md 内容 ──")
        with open(contract_path) as f:
            print(f.read())
        print("\n── tasks.yaml 内容 ──")
        with open(tasks_path) as f:
            print(f.read())

    # ── Step 5: Preview + confirm ──
    print_summary(
        plan=plan,
        desc=args.desc,
        sorted_agents=sorted_agents,
        dag_layers=dag_layers,
        slots=slots,
        contract_path=contract_path,
        tasks_path=tasks_path,
    )

    if args.dry_run:
        print("[planner] dry-run 模式，不执行。退出。")
        return

    # Confirm
    if not args.yes:
        try:
            ans = input("继续执行？[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[planner] 取消。")
            return
        if ans not in ("", "y", "yes"):
            print("[planner] 取消。")
            return

    # ── Execute ──
    # 1. Launch dashboard in background
    if os.path.exists(launch_sh):
        print("[planner] 启动展板 …")
        subprocess.Popen(
            ["bash", launch_sh, "--bg"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        print(f"  [planner] ⚠ launch.sh 不存在: {launch_sh}，跳过展板启动。", file=sys.stderr)

    # 2. Run conductor in foreground
    conductor_cmd = [
        sys.executable, conductor_py,
        "--tasks", tasks_path,
        "--max-slots", str(args.max_slots),
    ]
    if args.openai_api_key:
        conductor_cmd += ["--openai-api-key", args.openai_api_key]
    if args.openai_base_url:
        conductor_cmd += ["--openai-base-url", args.openai_base_url]
    if args.codex_model:
        conductor_cmd += ["--codex-model", args.codex_model]
    if args.codex_bin:
        conductor_cmd += ["--codex-bin", args.codex_bin]

    print(f"[planner] 启动 conductor: {' '.join(conductor_cmd)}")
    result = subprocess.run(conductor_cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
