"""
Hook 系统 — 把扩展逻辑从 agent loop 里抽出来。

事件:
  UserPromptSubmit — 用户输入后、发 LLM 前
  PreToolUse       — 工具执行前（权限、日志）
  PostToolUse      — 工具执行后（大输出警告）
  Stop             — LLM 不再调工具，准备退出
"""

from pathlib import Path

WORKDIR = Path.cwd()

# ═══════════════════════════════════════════════════════════
#  注册中心
# ═══════════════════════════════════════════════════════════

HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


def register_hook(event: str, callback):
    """把 callback 注册到某个事件上"""
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """触发某个事件的所有回调。
    返回第一个非 None 的结果；全 None 则返回 None。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ═══════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════

def _get_name(block) -> str:
    """兼容 dict 和 SDK 对象两种 block 格式"""
    if isinstance(block, dict):
        return block.get("name", "")
    return getattr(block, "name", "")


def _get_input(block) -> dict:
    if isinstance(block, dict):
        return block.get("input", {})
    return getattr(block, "input", {})


# ═══════════════════════════════════════════════════════════
#  Hook 实现
# ═══════════════════════════════════════════════════════════

DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def permission_hook(block):
    """PreToolUse: 原 s03 三道门权限逻辑，现在以 hook 形式存在。

    Gate 1: 硬拒绝 — DENY_LIST 里的直接拦
    Gate 2: 规则匹配 — 破坏性命令 / 访问工作目录外
    Gate 3: 用户确认 — 匹配到规则后弹 y/N
    """
    name = _get_name(block)
    args = _get_input(block)

    # Gate 1: 硬拒绝列表 (只对 bash)
    if name == "bash":
        command = args.get("command", "")
        for pattern in DENY_LIST:
            if pattern in command:
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return f"Permission denied: {pattern} is on the deny list"

        # Gate 2+3: 破坏性命令
        for kw in DESTRUCTIVE:
            if kw in command:
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {name}({args})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
                break  # 用户同意了，不再检查其他关键词

    # Gate 2+3: 文件工具访问工作目录外
    if name in ("read_file", "write_file", "edit_file", "diff", "glob", "grep", "ls"):
        path = args.get("path", ".")
        try:
            resolved = (WORKDIR / path).resolve()
            if not resolved.is_relative_to(WORKDIR):
                print(f"\n\033[33m⚠  Access outside workspace\033[0m")
                print(f"   Tool: {name}({args})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
        except (ValueError, OSError):
            # 路径非法（比如包含 \0），直接拦
            print(f"\n\033[33m⚠  Invalid path\033[0m")
            print(f"   Tool: {name}({args})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"

    return None


def log_hook(block) -> None:
    """PreToolUse: 记录每次工具调用"""
    name = _get_name(block)
    args = _get_input(block)
    args_preview = str(list(args.values())[:2])[:60]
    print(f"\033[90m[HOOK] {name}({args_preview})\033[0m")
    return None


def large_output_hook(block, output: str) -> None:
    """PostToolUse: 输出超过 100k 字符时警告"""
    if len(str(output)) > 100000:
        name = _get_name(block)
        print(f"\033[33m[HOOK] ⚠ Large output from {name}: {len(str(output))} chars\033[0m")
    return None


def context_inject_hook(query: str) -> None:
    """UserPromptSubmit: 用户输入后打印当前工作目录"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list) -> None:
    """Stop: 会话结束时打印工具调用统计"""
    tool_count = sum(
        1 for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


# ═══════════════════════════════════════════════════════════
#  初始化 — 一次性注册所有 hook
# ═══════════════════════════════════════════════════════════

def init_hooks():
    register_hook("UserPromptSubmit", context_inject_hook)
    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)
    register_hook("PostToolUse", large_output_hook)
    register_hook("Stop", summary_hook)
