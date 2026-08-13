"""
工具实现 + 定义 + 分发映射。
"""

import ast
import difflib
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from pathlib import Path

from skill_loader import load_skill as _load_skill, skill_manage
from cron import run_schedule_cron, run_list_crons, run_cancel_cron, run_cron_results
from message_bus import BUS
from protocol import ProtocolState, pending_requests, new_request_id, match_response

WORKDIR = Path.cwd()

# ═══════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    """路径安全校验：禁止逃逸工作目录"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# ═══════════════════════════════════════════════════════════
#  文件系统
# ═══════════════════════════════════════════════════════════

def run_read(path: str, offset: int = 0, limit: int = None) -> str:
    """读取文件，带行号和截断"""
    try:
        lines = safe_path(path).read_text().splitlines()
        total = len(lines)
        if offset:
            lines = lines[offset:]
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({total - offset - limit} more lines)"]
        numbered = [f"{i + 1 + offset:>6}\t{line}" for i, line in enumerate(lines)]
        return "\n".join(numbered)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件（自动创建父目录）"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """字符串精确替换，返回 unified diff"""
    try:
        file_path = safe_path(path)
        old_text = file_path.read_text()
        count = old_text.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path}"
        new_text = old_text.replace(old_string, new_string) if replace_all \
                   else old_text.replace(old_string, new_string, 1)
        file_path.write_text(new_text)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=path, tofile=path, lineterm=""
        ))
        n = count if replace_all else 1
        return f"Edited {path} ({n} replacement{'s' if n > 1 else ''})\n{diff}"
    except Exception as e:
        return f"Error: {e}"


def run_diff(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """预览替换后的 diff，不实际修改文件"""
    try:
        file_path = safe_path(path)
        old_text = file_path.read_text()
        count = old_text.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path}"
        new_text = old_text.replace(old_string, new_string) if replace_all \
                   else old_text.replace(old_string, new_string, 1)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=path, tofile=path, lineterm=""
        ))
        return diff if diff else "(no change)"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    """按 glob 模式匹配文件"""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(sorted(results)) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def run_grep(pattern: str, glob: str = "**/*", ignore_case: bool = True) -> str:
    """搜索文件内容（正则匹配），返回 文件名:行号:内容"""
    import glob as g
    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
        results = []
        for filepath in g.glob(glob, root_dir=WORKDIR, recursive=True):
            full_path = WORKDIR / filepath
            if not full_path.resolve().is_relative_to(WORKDIR):
                continue
            if not full_path.is_file():
                continue
            try:
                for i, line in enumerate(full_path.read_text().splitlines(), 1):
                    if regex.search(line):
                        results.append(f"{filepath}:{i}: {line.strip()[:200]}")
                        if len(results) >= 100:
                            break
            except (UnicodeDecodeError, PermissionError):
                pass
            if len(results) >= 100:
                break
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def run_ls(path: str = ".") -> str:
    """列出目录内容"""
    try:
        target = safe_path(path)
        entries = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        out = []
        for entry in entries:
            if entry.name.startswith("."):
                continue  # 跳过隐藏文件
            kind = "d" if entry.is_dir() else "f"
            size = entry.stat().st_size
            out.append(f"{kind}  {size:>8}  {entry.name}")
        return "\n".join(out) if out else "(empty)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  执行
# ═══════════════════════════════════════════════════════════

def run_bash(command: str) -> str:
    """执行 shell 命令"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  网络
# ═══════════════════════════════════════════════════════════

def run_web_fetch(url: str) -> str:
    """获取网页内容（纯文本）"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 coding-agent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            # 去掉 HTML 标签，返回纯文本（简单处理）
            html = resp.read().decode("utf-8", errors="replace")
            # 简单去标签
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:10000] if text else "(empty page)"
    except urllib.error.URLError as e:
        return f"Error fetching URL: {e}"
    except Exception as e:
        return f"Error: {e}"


def run_web_search(query: str) -> str:
    """搜索网页（DuckDuckGo HTML，免 API Key）"""
    try:
        url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 coding-agent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # 解析 DuckDuckGo Lite 结果
        results = re.findall(
            r'<a[^>]*class="result-link"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<td[^>]*class="result-snippet"[^>]*>(.*?)</td>',
            html, re.DOTALL
        )
        if not results:
            return "(no results)"
        out = []
        for i, (url, title, snippet) in enumerate(results[:10], 1):
            title = re.sub(r"<[^>]+>", "", title).strip()
            snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            out.append(f"{i}. {title}\n   {url}\n   {snippet}\n")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  交互
# ═══════════════════════════════════════════════════════════

def run_ask(question: str) -> str:
    """向用户提问（当 agent 需要澄清或确认时）"""
    print(f"\n\033[35m🤔 {question}\033[0m")
    try:
        answer = input("\033[35m> \033[0m").strip()
        return answer if answer else "(user skipped)"
    except (EOFError, KeyboardInterrupt):
        return "(user cancelled)"


# ═══════════════════════════════════════════════════════════
#  任务管理 — 磁盘持久化 + 依赖图
# ═══════════════════════════════════════════════════════════

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)

CURRENT_TODOS: list[dict] = []  # s05: todo_write 规划的步骤（会话级，内存）


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str              # pending | in_progress | done | cancelled
    owner: object            # Optional[str] — 认领者（避免 str | None 语法）
    blockedBy: list          # 依赖的前置任务 ID


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks_from_disk() -> list[Task]:
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def can_start(task_id: str) -> bool:
    """检查所有 blockedBy 依赖是否已完成。缺失的依赖视为阻塞。"""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "done":
            return False
    return True


def run_task_create(subject: str, description: str = "",
                    blockedBy=None) -> str:
    """创建任务，写入磁盘 JSON 文件"""
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_task_list() -> str:
    """列出所有任务（从磁盘读取）"""
    tasks = list_tasks_from_disk()
    if not tasks:
        return "(no tasks)"
    out = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "◉",
                "done": "✓", "cancelled": "✗"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        out.append(f"[{icon}] {t.id}: {t.subject} [{t.status}]{owner}{deps}")
    return "\n".join(out)


def run_task_update(task_id: str, status: str) -> str:
    """更新任务状态。设为 in_progress 时校验依赖是否满足。"""
    try:
        task = load_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

    old = task.status

    # 设为 in_progress 时检查依赖
    if status == "in_progress" and not can_start(task_id):
        blocked = [d for d in task.blockedBy
                   if not _task_path(d).exists() or load_task(d).status != "done"]
        return f"Error: {task_id} is blocked by: {blocked}"

    task.status = status
    if status in ("done", "cancelled"):
        task.owner = None  # 释放认领
    if status == "in_progress" and not task.owner:
        task.owner = "agent"
    save_task(task)

    msg = f"{task_id} ({task.subject}): {old} → {status}"
    print(f"  \033[33m[update] {msg}\033[0m")

    # 完成时报告解封的下游任务
    if status == "done":
        unblocked = [t.subject for t in list_tasks_from_disk()
                     if t.status == "pending" and t.blockedBy and can_start(t.id)]
        if unblocked:
            msg += f"\nUnblocked: {', '.join(unblocked)}"
            print(f"  \033[32m[unblocked] {', '.join(unblocked)}\033[0m")

    return msg


def run_get_task(task_id: str) -> str:
    """返回任务完整 JSON 详情"""
    try:
        task = load_task(task_id)
        return json.dumps(asdict(task), indent=2)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


# ── s17: 自治 worker 任务助手 — 扫描 + 认领 + 完成 ──

def scan_unclaimed_tasks() -> list[dict]:
    """找 pending、无 owner、依赖全部 done 的任务。"""
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        try:
            task = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: str, owner: str = "agent") -> str:
    """认领 pending 任务。设 owner + in_progress，写盘。"""
    try:
        task = load_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        blocked = [d for d in task.blockedBy
                   if not _task_path(d).exists() or load_task(d).status != "done"]
        return f"Error: {task_id} is blocked by: {blocked}"

    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    """完成 in_progress 任务。设 done + 释放 owner，报告解封的下游任务。"""
    try:
        task = load_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"

    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"

    task.status = "done"
    task.owner = None
    save_task(task)

    unblocked = [t.subject for t in list_tasks_from_disk()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── s05: todo_write — 批量规划步骤 ──

def _normalize_todos(todos):
    """校验并标准化 todos 输入（支持 list 或 JSON 字符串）"""
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    """批量更新当前任务计划，终端打印格式化列表"""
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


# ═══════════════════════════════════════════════════════════
#  s15: 后台任务 — daemon 线程异步执行 + 通知注入
# ═══════════════════════════════════════════════════════════

_bg_counter = 0
background_tasks: dict[str, dict] = {}   # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}   # bg_id → output
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """关键词兜底：可能超过 30s 的命令自动走后台。只对 bash 生效。"""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """模型显式声明优先；否则走关键词兜底。"""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def start_background_task(tool_use_id: str, tool_name: str,
                          tool_input: dict) -> str:
    """在 daemon 线程中执行工具。返回 bg_id，立即返回不阻塞。"""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = tool_input.get("command", tool_name)

    def worker():
        handler = TOOL_HANDLERS.get(tool_name)
        try:
            result = handler(**tool_input) if handler else f"Unknown tool: {tool_name}"
        except Exception as e:
            result = f"Error: {e}"
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = str(result)

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": tool_use_id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """收集已完成的 background 结果，格式化为 <task_notification>。
    每个结果只投递一次（pop 出 dict）。"""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id, None)
            output = background_results.pop(bg_id, "")
        if task is None:
            continue
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications


# ═══════════════════════════════════════════════════════════
#  s16: Agent Teams — teammate 异步派发 + MessageBus 通信
# ═══════════════════════════════════════════════════════════

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """占位 — 实际 handler 由 agent_loop.py 注入（需要 client 和 MODEL）。"""
    return "Error: spawn_teammate not initialized (injected by agent_loop)"


def run_send_message(to: str, content: str) -> str:
    """通过 MessageBus 发送消息给其他 agent。"""
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    """查看 Lead 的收件箱。消费性读取 + 路由协议响应。"""
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)


def consume_lead_inbox(route_protocol: bool = True) -> list[dict]:
    """读 Lead 收件箱：路由协议响应，返回全部消息。"""
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False))
    return msgs


# ── s16: 协议工具 — shutdown / plan 审批 ──

def run_request_shutdown(teammate: str) -> str:
    """要求 teammate 优雅退出。"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request", {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request → {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown request sent to {teammate} (req: {req_id})"


def run_request_plan(teammate: str, task: str) -> str:
    """要求 teammate 提交一个计划。"""
    BUS.send("lead", teammate, f"Please submit a plan for: {task}", "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    """批准或拒绝一个已提交的计划。"""
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    if state.status != "pending":
        return f"Request {request_id} already {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender,
             feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "✓" if approve else "✗"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {'approved' if approve else 'rejected'} ({request_id})"


# ═══════════════════════════════════════════════════════════
#  工具定义
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # ── 文件系统 ──
    {"name": "read_file",
     "description": "Read file contents with line numbers. Returns numbered lines, optionally offset/limit.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "File path"},
                                     "offset": {"type": "integer", "description": "Line number to start from (0-based)"},
                                     "limit": {"type": "integer", "description": "Max lines to return"}},
                      "required": ["path"]}},

    {"name": "write_file",
     "description": "Write content to a file. Creates parent directories automatically.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},

    {"name": "edit_file",
     "description": "Replace exact string match in a file. Returns a unified diff of the change. Set replace_all=true to replace all occurrences.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_string": {"type": "string"},
                                     "new_string": {"type": "string"},
                                     "replace_all": {"type": "boolean"}},
                      "required": ["path", "old_string", "new_string"]}},

    {"name": "diff",
     "description": "Preview a unified diff of what an edit WOULD change, without actually modifying the file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_string": {"type": "string"},
                                     "new_string": {"type": "string"},
                                     "replace_all": {"type": "boolean"}},
                      "required": ["path", "old_string", "new_string"]}},

    {"name": "glob",
     "description": "Find files matching a glob pattern (e.g. '**/*.py').",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},

    {"name": "grep",
     "description": "Search file contents with regex. Returns path:line:content.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string", "description": "Regex pattern"},
                                     "glob": {"type": "string", "description": "File filter, e.g. '**/*.py'"},
                                     "ignore_case": {"type": "boolean"}},
                      "required": ["pattern"]}},

    {"name": "ls",
     "description": "List directory contents (non-hidden files).",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}},
                      "required": []}},

    # ── 执行 ──
    {"name": "bash",
     "description": "Run a shell command. Set run_in_background=true for slow commands (install, build, test, deploy) to avoid blocking.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"},
                                     "run_in_background": {"type": "boolean",
                                                           "description": "Run in daemon thread, return placeholder immediately"}},
                      "required": ["command"]}},

    # ── 网络 ──
    {"name": "web_fetch",
     "description": "Fetch a URL and return its text content.",
     "input_schema": {"type": "object",
                      "properties": {"url": {"type": "string"}},
                      "required": ["url"]}},

    {"name": "web_search",
     "description": "Search the web and return top results with titles and snippets.",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string"}},
                      "required": ["query"]}},

    # ── 交互 ──
    {"name": "ask_user",
     "description": "Ask the user a question when you need clarification or confirmation.",
     "input_schema": {"type": "object",
                      "properties": {"question": {"type": "string"}},
                      "required": ["question"]}},

    # ── 任务管理 ──
    {"name": "todo_write",
     "description": "Create and manage a task list for your current coding session. Use this to plan steps before executing multi-step tasks, and update status as you go.",
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array",
                                               "items": {"type": "object",
                                                         "properties": {"content": {"type": "string", "description": "Task description"},
                                                                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                                                         "required": ["content", "status"]}}},
                      "required": ["todos"]}},

    {"name": "task_create",
     "description": "Create a persistent task with optional blockedBy dependencies. Tasks survive restarts.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"},
                                     "blockedBy": {"type": "array",
                                                   "items": {"type": "string"},
                                                   "description": "Task IDs that must be done before this one"}},
                      "required": ["subject"]}},

    {"name": "task_list",
     "description": "List all persistent tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object",
                      "properties": {},
                      "required": []}},

    {"name": "task_update",
     "description": "Update task status. Setting to in_progress checks blockedBy dependencies — returns error if blocked.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"},
                                     "status": {"type": "string", "enum": ["pending", "in_progress", "done", "cancelled"]}},
                      "required": ["task_id", "status"]}},

    {"name": "get_task",
     "description": "Get full JSON details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},

    # ── 子 agent ──
    {"name": "task",
     "description": "Launch a subagent to handle a complex subtask with fresh context. Subagent returns only the final summary.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string", "description": "Task description for the subagent"}},
                      "required": ["description"]}},

    # ── 定时任务 ──
    {"name": "schedule_cron",
     "description": "Schedule a recurring or one-shot cron job. cron is 5-field: min hour dom month dow. Fires in a subagent — won't interrupt your session.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string", "description": "5-field cron expression, e.g. '0 9 * * *'"},
                          "prompt": {"type": "string", "description": "Task for the subagent to execute when fired"},
                          "recurring": {"type": "boolean", "description": "True=repeat, False=one-shot"},
                          "durable": {"type": "boolean", "description": "True=survive restart"}},
                      "required": ["cron", "prompt"]}},

    {"name": "list_crons",
     "description": "List all registered cron jobs with their schedule and status.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},

    {"name": "cancel_cron",
     "description": "Cancel a cron job by its ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},

    {"name": "cron_results",
     "description": "Show recent cron execution results from subagents.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},

    # ── Agent Teams ──
    {"name": "spawn_teammate",
     "description": "Spawn a teammate agent in a background thread. Teammate works independently and reports back via inbox.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string", "description": "Short name for the teammate"},
                          "role": {"type": "string", "description": "What the teammate specializes in"},
                          "prompt": {"type": "string", "description": "Task description for the teammate"}},
                      "required": ["name", "role", "prompt"]}},

    {"name": "send_message",
     "description": "Send a message to another agent via MessageBus.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string", "description": "Recipient agent name"},
                                     "content": {"type": "string", "description": "Message content"}},
                      "required": ["to", "content"]}},

    {"name": "check_inbox",
     "description": "Check Lead's inbox for messages from teammates (destructive read).",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},

    {"name": "request_shutdown",
     "description": "Request a teammate to shut down gracefully.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},

    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan for review.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},

    {"name": "review_plan",
     "description": "Approve or reject a submitted plan by request_id.",
     "input_schema": {"type": "object",
                      "properties": {
                          "request_id": {"type": "string"},
                          "approve": {"type": "boolean"},
                          "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},

    # ── 技能 ──
    {"name": "load_skill",
     "description": "Load the full content of a skill by name. Use when you need detailed instructions for a specific task type.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string", "description": "Skill name from the catalog"}},
                      "required": ["name"]}},

    {"name": "skill_manage",
     "description": "Create or update a Skill (self-evolution). action='create' to save a reusable method; action='update' to fix a deficient skill (rewrite via content, or fuzzy patch via old_string+new_string).",
     "input_schema": {"type": "object",
                      "properties": {
                          "action": {"type": "string", "enum": ["create", "update"]},
                          "name": {"type": "string", "description": "Skill name (lowercase, digits, - or _)"},
                          "content": {"type": "string", "description": "Full skill body (create, or update rewrite)"},
                          "description": {"type": "string", "description": "One-line description (create only)"},
                          "category": {"type": "string", "description": "code | workflow | tooling | reference | other (optional)"},
                          "old_string": {"type": "string", "description": "Text to replace (update patch mode)"},
                          "new_string": {"type": "string", "description": "Replacement text (update patch mode)"}},
                      "required": ["action", "name"]}},
]

# ═══════════════════════════════════════════════════════════
#  分发映射
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "diff": run_diff,
    "glob": run_glob,
    "grep": run_grep,
    "ls": run_ls,
    "bash": run_bash,
    "web_fetch": run_web_fetch,
    "web_search": run_web_search,
    "ask_user": run_ask,
    "todo_write": run_todo_write,
    "task_create": run_task_create,
    "task_list": run_task_list,
    "task_update": run_task_update,
    "get_task": run_get_task,
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
    "cron_results": run_cron_results,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message,
    "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
    "load_skill": _load_skill,
    "skill_manage": skill_manage,
}
