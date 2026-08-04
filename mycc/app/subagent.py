"""
Subagent — 全新 messages[] 上下文隔离，只返回摘要，最多 30 轮。

  Parent Agent                           Subagent
  +------------------+                  +------------------+
  | messages=[...]   |                  | messages=[task]  | <-- fresh
  |                  |   dispatch       |                  |
  | tool: task       | ---------------> | own while loop   |
  |   description=".."|                 |   bash/read/...  |
  |                  |   summary only   |   (max 30 turns) |
  | result = "..."   | <--------------- | return last text |
  +------------------+                  +------------------+
"""

from tools import run_bash, run_read, run_write, run_edit, run_glob
from hooks import trigger_hooks

# 子 agent 专属 system prompt — 禁止再次委托
SUB_SYSTEM = "You are a coding agent. Complete the task you were given, then return a concise summary. Do not delegate further."

# 子 agent 工具集 — 没有 task（防止递归套娃），没有 todo_write
SUB_TOOLS = [
    {"name": "bash",
     "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},

    {"name": "read_file",
     "description": "Read file contents with line numbers.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "offset": {"type": "integer"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},

    {"name": "write_file",
     "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},

    {"name": "edit_file",
     "description": "Replace exact string match in a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_string": {"type": "string"},
                                     "new_string": {"type": "string"},
                                     "replace_all": {"type": "boolean"}},
                      "required": ["path", "old_string", "new_string"]}},

    {"name": "glob",
     "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]

SUB_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


def extract_text(content) -> str:
    """从 assistant content 里提取纯文本"""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(b, "text", "") for b in content
        if getattr(b, "type", None) == "text"
    )


def spawn_subagent(client, model: str, description: str) -> str:
    """创建子 agent，全新上下文，只返回最终摘要。"""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]  # 全新上下文

    for _ in range(30):  # 安全上限
        response = client.messages.create(
            model=model,
            system=SUB_SYSTEM,
            messages=messages,
            tools=SUB_TOOLS,
            max_tokens=10000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # 子 agent 也走权限 hook
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(blocked),
                })
                continue

            handler = SUB_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            trigger_hooks("PostToolUse", block, output)
            print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})

    # 模型总结 — 额外调一次 LLM，从完整历史提取结构化摘要
    print(f"  \033[90m[sub] Summarizing...\033[0m")
    messages.append({
        "role": "user",
        "content": (
            "Summarize what you accomplished above. "
            "Include: (1) what was done, (2) what files were changed, "
            "(3) any issues or remaining work."
        ),
    })
    try:
        summary = client.messages.create(
            model=model,
            system="You are a helpful summarizer. Be concise and factual.",
            messages=messages[-20:],  # 只给最近 20 轮，避免超出上下文
            max_tokens=2000,
        )
        result = extract_text(summary.content)
    except Exception as e:
        result = f"Summary failed: {e}"

    # 兜底：模型调用失败或返回空
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent finished without summary."

    print(f"\033[35m[Subagent done]\033[0m")
    return result
