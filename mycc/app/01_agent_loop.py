#!/usr/bin/env python3
import json
import os
import subprocess

# readline：提供命令行编辑能力（方向键、历史记录等）
try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

# Anthropic SDK：调用 Claude 模型
from anthropic import Anthropic
# python-dotenv：从 .env 文件加载环境变量
from dotenv import load_dotenv

# 加载 .env 中的 API Key 等配置，override=True 表示覆盖已有的环境变量
load_dotenv(override=True)

# 如果设置了自定义 base_url（如代理），清除默认的 auth token 避免冲突
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化 Anthropic 客户端，支持自定义 API 地址（代理/私有部署）
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 从环境变量读取模型 ID
MODEL = os.environ["MODEL_ID"]

# 系统提示词：告诉模型它是编码 agent，直接行动不啰嗦
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# ── Tool definition: just bash ────────────────────────────
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


# ── Tool execution ────────────────────────────────────────
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ── The core pattern: streaming agent loop ───────────────────────
def agent_loop(messages: list):
    while True:
        # ── 流式调用 ────────────────────────────────────────────
        content_blocks = {}    # 所有内容块，key=event.index
        stop_reason = None

        with client.messages.stream(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        ) as stream:
            for event in stream:
                # 一个内容块（文本/tool_use）开始
                if event.type == "content_block_start":
                    block = event.content_block

                    if block.type == "text":
                        content_blocks[event.index] = {"type": "text", "text": ""}
                        print("\033[0m", end="")  # 重置颜色

                    elif block.type == "tool_use":
                        content_blocks[event.index] = {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": "",
                        }

                # 内容增量：文本或 tool_use 的 input_json_delta
                elif event.type == "content_block_delta":
                    delta = event.delta

                    if delta.type == "text_delta":
                        content_blocks[event.index]["text"] += delta.text
                        print(delta.text, end="", flush=True)  # 实时打字效果

                    elif delta.type == "input_json_delta":
                        content_blocks[event.index]["input"] += delta.partial_json

                # 消息级别的 delta（stop_reason, usage 等）
                elif event.type == "message_delta":
                    stop_reason = event.delta.stop_reason

        # ── 组装 assistant 消息（按 index 排序保持原始顺序）──────
        assistant_content = [
            block for _, block in sorted(content_blocks.items())
        ]

        # 将 JSON 字符串反序列化为 dict
        for block in assistant_content:
            if block["type"] == "tool_use" and isinstance(block["input"], str):
                block["input"] = json.loads(block["input"])

        messages.append({"role": "assistant", "content": assistant_content})

        # ── 如果模型没有调用工具，结束循环 ────────────────────────
        if stop_reason != "tool_use":
            return

        # ── 执行工具调用，收集结果 ────────────────────────────────
        tool_results = []
        for block in assistant_content:
            if block["type"] == "tool_use":
                cmd = block["input"]["command"]
                print(f"\n\033[33m$ {cmd}\033[0m")
                output = run_bash(cmd)
                print(output[:200])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": output,
                })

        # 将工具结果喂回消息历史，循环继续
        messages.append({"role": "user", "content": tool_results})


# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        # Print the model's final text response
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
