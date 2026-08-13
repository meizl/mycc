"""
Protocol — 请求/响应协议状态机，用于 Leader 和 teammate 之间的结构化通信。

通过 request_id 关联原始请求和响应。典型流程：
  Leader.request_shutdown → BUS.send(shutdown_request, metadata={request_id})
  Teammate.handle_inbox_message → BUS.send(shutdown_response, metadata={request_id})
  Leader.match_response → 校验类型 + 更新 pending_requests 状态
"""

import random
import time
from dataclasses import dataclass, field


@dataclass
class ProtocolState:
    request_id: str
    type: str            # "shutdown" | "plan_approval"
    sender: str
    target: str
    status: str          # "pending" | "approved" | "rejected"
    payload: str
    created_at: float = field(default_factory=time.time)


# 未完成的协议请求
pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type: str, request_id: str, approve: bool):
    """用 request_id 关联原始请求，校验响应类型匹配，更新状态。"""
    state = pending_requests.get(request_id)
    if not state:
        print(f"  \033[31m[protocol] unknown request_id: {request_id}\033[0m")
        return
    if state.type == "shutdown" and response_type != "shutdown_response":
        print(f"  \033[31m[protocol] type mismatch: expected shutdown_response, "
              f"got {response_type}\033[0m")
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        print(f"  \033[31m[protocol] type mismatch: expected plan_approval_response, "
              f"got {response_type}\033[0m")
        return
    state.status = "approved" if approve else "rejected"
    icon = "✓" if approve else "✗"
    color = "32" if approve else "31"
    print(f"  \033[{color}m[protocol] {state.type} {icon} "
          f"({request_id}: {state.status})\033[0m")
