"""
Teammate — 自治驻留 worker（s17）。

与 s15 的一次性 teammate 不同：干完活不退出，进入 IDLE 轮询，
自动认领任务板上无人接的单，60 秒没事干才自杀。

生命周期：
  WORK: 收件箱 → LLM → 工具 → (还有工具调用? 继续) → (干完? → IDLE)
  IDLE: 每 5s 轮询 → 收件箱? → WORK / 有无人认领任务? → 认领 → WORK
                    / 60s 超时? → SHUTDOWN

协议（s16）：
  shutdown_request / shutdown_response
  plan_approval_request / plan_approval_response
"""

import json
import threading
import time

from message_bus import BUS
from protocol import ProtocolState, pending_requests, new_request_id
from subagent import SUB_TOOLS, SUB_HANDLERS
from tools import scan_unclaimed_tasks, claim_task, complete_task


# 追踪活跃的 teammate
active_teammates: dict[str, bool] = {}

# IDLE 轮询参数
IDLE_POLL_INTERVAL = 5   # 秒
IDLE_TIMEOUT = 60        # 秒


def spawn_teammate_async(name: str, role: str, prompt: str,
                         client, model: str) -> str:
    """Spawn an autonomous teammate worker in a background daemon thread.
    Runs a WORK/IDLE lifecycle: works on tasks, idles, auto-claims unclaimed
    tasks, and shuts down after idle timeout. Returns immediately."""
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"You can list and claim tasks from the board. "
              f"Check inbox for protocol messages.")

    def handle_inbox_message(msg: dict, messages: list) -> bool:
        """分发协议消息。返回 True 表示收到 shutdown 请求，需要关闭。"""
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")

        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down gracefully.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            print(f"  \033[35m[protocol] {name} approved shutdown "
                  f"({req_id})\033[0m")
            return True

        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if approve:
                messages.append({"role": "user",
                                 "content": "[Plan approved] Proceed with the task."})
            else:
                messages.append({"role": "user",
                                 "content": f"[Plan rejected] Feedback: {msg['content']}"})
        return False

    def submit_plan(plan: str) -> str:
        """teammate 提交计划给 Lead 审批。"""
        req_id = new_request_id()
        pending_requests[req_id] = ProtocolState(
            request_id=req_id, type="plan_approval",
            sender=name, target="lead",
            status="pending", payload=plan)
        BUS.send(name, "lead", plan, "plan_approval_request",
                 {"request_id": req_id})
        return f"Plan submitted ({req_id}). Waiting for approval..."

    def idle_poll(messages: list) -> str:
        """IDLE 轮询。返回 'work'、'shutdown' 或 'timeout'。"""
        for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
            time.sleep(IDLE_POLL_INTERVAL)

            # 检查收件箱 — 优先分发协议消息
            inbox = BUS.read_inbox(name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        handle_inbox_message(msg, messages)
                        return "shutdown"
                # 非协议消息：注入并恢复工作
                messages.append({"role": "user",
                                 "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
                print(f"  \033[36m[idle] {name} found inbox messages\033[0m")
                return "work"

            # 扫描任务板，自动认领
            unclaimed = scan_unclaimed_tasks()
            if unclaimed:
                task = unclaimed[0]
                result = claim_task(task["id"], name)
                if "Claimed" in result:
                    messages.append({"role": "user",
                                     "content": f"<auto-claimed>Task {task['id']}: "
                                                f"{task['subject']}</auto-claimed>"})
                    print(f"  \033[32m[idle] {name} auto-claimed: "
                          f"{task['subject']}\033[0m")
                    return "work"
                print(f"  \033[33m[idle] {name} claim failed: {result}\033[0m")

        print(f"  \033[31m[idle] {name} timeout ({IDLE_TIMEOUT}s)\033[0m")
        return "timeout"

    def run():
        messages = [{"role": "user", "content": prompt}]

        # teammate 工具集 = subagent 基础 + team 工具
        sub_tools = list(SUB_TOOLS) + [
            {"name": "send_message",
             "description": "Send a message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            {"name": "list_tasks",
             "description": "List all tasks on the board.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as done.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        def _run_list_tasks() -> str:
            from tools import list_tasks_from_disk
            tasks = list_tasks_from_disk()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]" for t in tasks)

        sub_handlers = dict(SUB_HANDLERS)
        sub_handlers.update({
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                 "Sent")[1],
            "submit_plan": lambda plan: submit_plan(plan),
            "list_tasks": lambda: _run_list_tasks(),
            "claim_task": lambda task_id: claim_task(task_id, owner=name),
            "complete_task": lambda task_id: complete_task(task_id),
        })

        # 外层循环：WORK → IDLE 循环
        while True:
            # identity 重注入（上下文被压缩后）
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                                    "content": f"<identity>You are '{name}', "
                                               f"role: {role}. Continue your "
                                               f"work.</identity>"})

            # WORK phase
            should_shutdown = False
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if handle_inbox_message(msg, messages):
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                                         "content": f"<inbox>{json.dumps(non_protocol)}</inbox>"})

                try:
                    response = client.messages.create(
                        model=model, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception:
                    break
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        handler = sub_handlers.get(block.name)
                        if not handler:
                            output = "Unknown"
                        else:
                            try:
                                output = handler(**block.input)
                            except Exception as e:
                                output = f"Error: {type(e).__name__}: {e}"
                        print(f"  \033[90m[teammate:{name}] {block.name}: "
                              f"{str(output)[:80]}\033[0m")
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": str(output)})
                messages.append({"role": "user", "content": results})

            if should_shutdown:
                break

            # IDLE phase
            idle_result = idle_poll(messages)
            if idle_result in ("shutdown", "timeout"):
                break

        # 最终摘要发回 Lead
        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role} (autonomous)\033[0m")
    return f"Teammate '{name}' spawned as {role} (autonomous). Will report back."
