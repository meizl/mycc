"""
Teammate — 异步子 agent，在线程中运行，通过 MessageBus 回传结果。

与 subagent.py 的 spawn_subagent() 的关系：
  subagent.py: 同步阻塞，Leader 等待结果
  teammate.py: 异步非阻塞，Leader 立即继续，结果通过 BUS 投递

Teammate 复用 spawn_subagent() 的 agent loop，在线程中执行。
完成后通过 BUS.send 把摘要发回 Lead 的收件箱。
"""

import threading

from message_bus import BUS


# 追踪活跃的 teammate
active_teammates: dict[str, bool] = {}


def spawn_teammate_async(name: str, role: str, prompt: str,
                         client, model: str) -> str:
    """Spawn a teammate agent in a background daemon thread.
    Reuses spawn_subagent for the actual agent loop.
    Result is delivered via BUS.send back to 'lead'.
    Returns immediately — does NOT wait for completion."""
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    # Lazy import to avoid circular dependency at module level
    from subagent import spawn_subagent

    def run():
        print(f"  \033[36m[teammate] {name} ({role}) started\033[0m")
        try:
            result = spawn_subagent(client, model, prompt)
        except Exception as e:
            result = f"Teammate {name} failed: {e}"
        BUS.send(name, "lead", result, "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role}. Will report back when done."
