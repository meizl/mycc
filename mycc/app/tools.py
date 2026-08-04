"""
工具实现 + 定义 + 分发映射。
"""

import ast
import difflib
import json
import os
import re
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

from skill_loader import load_skill as _load_skill

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
#  任务管理
# ═══════════════════════════════════════════════════════════

_tasks: list[dict] = []  # 内存中的任务列表
CURRENT_TODOS: list[dict] = []  # s05: todo_write 规划的步骤

def run_task_create(subject: str, description: str = "") -> str:
    """创建任务"""
    task_id = str(len(_tasks) + 1)
    _tasks.append({
        "id": task_id,
        "subject": subject,
        "description": description,
        "status": "pending",
    })
    return f"Task #{task_id} created: {subject}"

def run_task_list() -> str:
    """列出所有任务"""
    if not _tasks:
        return "(no tasks)"
    out = []
    for t in _tasks:
        icon = {"pending": "○", "in_progress": "◉", "done": "✓", "cancelled": "✗"}.get(t["status"], "?")
        out.append(f"[{icon}] #{t['id']} {t['subject']}")
    return "\n".join(out)

def run_task_update(task_id: str, status: str) -> str:
    """更新任务状态 (pending / in_progress / done / cancelled)"""
    for t in _tasks:
        if t["id"] == task_id:
            old = t["status"]
            t["status"] = status
            return f"Task #{task_id}: {old} → {status}"
    return f"Error: task #{task_id} not found"


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
     "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
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
     "description": "Create a task for tracking complex multi-step work.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"}},
                      "required": ["subject"]}},

    {"name": "task_list",
     "description": "List all tasks with their status.",
     "input_schema": {"type": "object",
                      "properties": {},
                      "required": []}},

    {"name": "task_update",
     "description": "Update task status: pending, in_progress, done, or cancelled.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"},
                                     "status": {"type": "string", "enum": ["pending", "in_progress", "done", "cancelled"]}},
                      "required": ["task_id", "status"]}},

    # ── 子 agent ──
    {"name": "task",
     "description": "Launch a subagent to handle a complex subtask with fresh context. Subagent returns only the final summary.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string", "description": "Task description for the subagent"}},
                      "required": ["description"]}},

    # ── 技能 ──
    {"name": "load_skill",
     "description": "Load the full content of a skill by name. Use when you need detailed instructions for a specific task type.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string", "description": "Skill name from the catalog"}},
                      "required": ["name"]}},
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
    "load_skill": _load_skill,
}
