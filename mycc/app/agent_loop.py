#!/usr/bin/env python3
"""
Agent Loop — 流式调用 + 工具执行循环。
工具定义和实现在 tools.py 中。
"""

import json
import os
import time

# readline：提供命令行编辑能力（方向键、历史记录等）
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

from tools import TOOLS, TOOL_HANDLERS, WORKDIR
from hooks import trigger_hooks, init_hooks
from subagent import spawn_subagent
from skill_loader import list_skills
from compact import compact_pipeline
from memory import build_memory_system, register_memory_hooks, load_memories, inject_memories

# ── 初始化 ──────────────────────────────────────────────

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

from error_recovery import (
    DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS, MAX_CONTINUATIONS, CONTINUATION_PROMPT,
    FALLBACK_MODEL, MAX_RETRIES, MAX_CONSECUTIVE_529,
    RecoveryState, retry_delay, is_rate_limit_error, is_overloaded_error,
)

# s06: 注册 task 工具 — 需要 client 和 MODEL，所以在这里注入
TOOL_HANDLERS["task"] = lambda description: spawn_subagent(client, MODEL, description)

# s09: 注册记忆提取钩子 — turn 结束时后台子线程异步提取
register_memory_hooks(client, MODEL)


# ── CLAUDE.md 加载 ──────────────────────────────────────

def load_claude_md() -> str:
    """从当前目录向上查找 CLAUDE.md，返回带标注的内容。"""
    current = WORKDIR.resolve()
    tried = []
    while True:
        candidate = current / "CLAUDE.md"
        tried.append(str(candidate))
        if candidate.is_file():
            content = candidate.read_text(encoding="utf-8", errors="replace")
            # 截断过长的文件（超过 8000 字符的部分用摘要替代）
            if len(content) > 8000:
                content = content[:8000] + "\n\n... (truncated)"
            return (
                f"<project-context source=\"{candidate}\">\n"
                f"{content}\n"
                f"</project-context>"
            )
        if current.parent == current:
            break  # 已到文件系统根
        current = current.parent
    return ""

# s07: SYSTEM 动态包含技能目录（Layer 1 — 便宜），完整内容通过 load_skill 按需加载（Layer 2）
SYSTEM = (
    f"You are a coding agent.\n"
    f"Skills available:\n{list_skills()}\n"
    "Use load_skill to get full details when needed. "
    "For complex sub-problems, use task to spawn a subagent. "
    "Before multi-step tasks, use todo_write to plan. "
    "Read files before editing — never guess content."
)

# s09: 加载 CLAUDE.md 注入 SYSTEM
claude_md = load_claude_md()
if claude_md:
    SYSTEM = claude_md + "\n\n" + SYSTEM
    print(f"\033[90mLoaded CLAUDE.md\033[0m")

# s09: 注入记忆索引到 SYSTEM（便宜，只读 MEMORY.md）
memory_system = build_memory_system()
if memory_system:
    SYSTEM += "\n\n" + memory_system


# ── The core pattern: streaming agent loop ───────────────────────

def agent_loop(messages: list):
    rounds_since_todo = 0  # s05: nag 计数器
    max_tokens = DEFAULT_MAX_TOKENS          # s11: 动态 max_tokens
    continuation_count = 0                    # s11: 续写次数
    state = RecoveryState(MODEL)              # s11: 恢复状态（529/模型切换）
    while True:
        # s05: 3 轮没更新 todo → 注入提醒
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        # s08: 上下文压缩管道 — 规则 L1/L2/L3 + 投影 L4 + AutoCompact
        api_messages = compact_pipeline(messages, client, MODEL)

        # s09: 按需加载相关记忆 + 注入（只在当前是用户消息时生效）
        memories_content = load_memories(client, MODEL, messages)
        api_messages = inject_memories(api_messages, memories_content)

        # s11: 429/529 退避重试 — 包裹流式调用
        for retry_attempt in range(MAX_RETRIES):
            content_blocks = {}
            stop_reason = None

            try:
                with client.messages.stream(
                    model=state.current_model, system=SYSTEM, messages=api_messages,
                    tools=TOOLS, max_tokens=max_tokens,
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_start":
                            block = event.content_block

                            if block.type == "text":
                                content_blocks[event.index] = {"type": "text", "text": ""}
                                print("\033[0m", end="")

                            elif block.type == "tool_use":
                                content_blocks[event.index] = {
                                    "type": "tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": "",
                                }

                        elif event.type == "content_block_delta":
                            delta = event.delta

                            if delta.type == "text_delta":
                                content_blocks[event.index]["text"] += delta.text
                                print(delta.text, end="", flush=True)

                            elif delta.type == "input_json_delta":
                                content_blocks[event.index]["input"] += delta.partial_json

                        elif event.type == "message_delta":
                            stop_reason = event.delta.stop_reason

                # 成功：重置 529 计数器，退出重试循环
                state.consecutive_529 = 0
                break

            except Exception as e:
                # 429 限流 -> 指数退避
                if is_rate_limit_error(e):
                    delay = retry_delay(retry_attempt)
                    print(f"\n  \033[33m[429 rate limit] retry {retry_attempt+1}/{MAX_RETRIES},"
                          f" wait {delay:.1f}s\033[0m")
                    time.sleep(delay)
                    continue

                # 529 过载 -> 退避 + 备用模型
                if is_overloaded_error(e):
                    state.consecutive_529 += 1
                    if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                        if FALLBACK_MODEL:
                            state.current_model = FALLBACK_MODEL
                            state.consecutive_529 = 0
                            print(f"\n  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                                  f" switching to {FALLBACK_MODEL}\033[0m")
                        else:
                            state.consecutive_529 = 0
                            print(f"\n  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                                  f" no FALLBACK_MODEL_ID configured\033[0m")
                    delay = retry_delay(retry_attempt)
                    print(f"  \033[33m[529 overloaded] retry {retry_attempt+1}/{MAX_RETRIES},"
                          f" wait {delay:.1f}s\033[0m")
                    time.sleep(delay)
                    continue

                # 非瞬时错误 -> 往上抛（Path 2 prompt_too_long 以后会在这里处理）
                raise
        else:
            # 全部重试耗尽
            print(f"  \033[31m[unrecoverable] max retries ({MAX_RETRIES}) exceeded\033[0m")
            return

        # 组装 assistant 消息
        assistant_content = [
            block for _, block in sorted(content_blocks.items())
        ]

        # JSON 字符串 → dict
        for block in assistant_content:
            if block["type"] == "tool_use" and isinstance(block["input"], str):
                block["input"] = json.loads(block["input"])

        messages.append({"role": "assistant", "content": assistant_content})

        # s11: max_tokens 恢复 — 升级 + 续写
        if stop_reason == "max_tokens":
            if continuation_count == 0:
                max_tokens = ESCALATED_MAX_TOKENS
                print(f"\n  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
            if continuation_count < MAX_CONTINUATIONS:
                continuation_count += 1
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                print(f"  \033[33m[max_tokens] continuation"
                      f" {continuation_count}/{MAX_CONTINUATIONS}\033[0m")
                continue
            print(f"  \033[31m[max_tokens] recovery limit reached"
                  f" ({MAX_CONTINUATIONS} continuations)\033[0m")
            return

        if stop_reason != "tool_use":
            # s04: Stop hook — memory extract runs in bg thread via s09 hook
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 执行工具
        rounds_since_todo += 1  # s05: 每轮工具执行递增
        tool_results = []
        for block in assistant_content:
            if block["type"] == "tool_use":
                name = block["name"]
                args = block["input"]
                print(f"\n\033[33m> {name}\033[0m", end="")

                # s04: hook 取代硬编码的 check_permission()
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": str(blocked),
                    })
                    continue

                handler = TOOL_HANDLERS.get(name)
                output = handler(**args) if handler else f"Unknown tool: {name}"

                trigger_hooks("PostToolUse", block, output)

                # s05: todo_write 命中 → 重置 nag 计数器
                if name == "todo_write":
                    rounds_since_todo = 0

                print(str(output)[:200])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": output,
                })

        messages.append({"role": "user", "content": tool_results})


# ── Entry point ──────────────────────────────────────────

if __name__ == "__main__":
    init_hooks()
    print("Agent Loop — 流式 + 多工具 + Hooks")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36m>> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # 打印模型的最终文本回复
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
