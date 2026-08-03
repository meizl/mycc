#!/usr/bin/env python3
"""
Agent Loop — 流式调用 + 工具执行循环。
工具定义和实现在 tools.py 中。
"""

import json
import os

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

from tools import TOOLS, TOOL_HANDLERS
from hooks import trigger_hooks, init_hooks

# ── 初始化 ──────────────────────────────────────────────

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = "You are a coding agent. Use tools to solve tasks. Act, don't explain."


# ── The core pattern: streaming agent loop ───────────────────────

def agent_loop(messages: list):
    while True:
        content_blocks = {}
        stop_reason = None

        with client.messages.stream(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
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

        # 组装 assistant 消息
        assistant_content = [
            block for _, block in sorted(content_blocks.items())
        ]

        # JSON 字符串 → dict
        for block in assistant_content:
            if block["type"] == "tool_use" and isinstance(block["input"], str):
                block["input"] = json.loads(block["input"])

        messages.append({"role": "assistant", "content": assistant_content})

        if stop_reason != "tool_use":
            # s04: Stop hook — 可注入消息让循环继续
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 执行工具
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
