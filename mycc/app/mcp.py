"""
MCP — 外接工具，标准协议（s19）。

核心思想：外部服务只要实现 MCP 协议（tools/list + tools/call），
Agent 就能直接调用，不管服务用什么语言写。

教学版用 mock handler 模拟外部 server（不依赖真实服务就能跑通流程）。
真实版会启动子进程，通过 stdin/stdout 发 JSON-RPC。

流程：
  connect_mcp("docs") → MCPClient 发现工具 →
  assemble_tool_pool() → [builtin..., mcp__docs__search, mcp__docs__get_version]
  agent_loop 用组装后的工具池
"""

import re
from typing import Callable


_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def normalize_mcp_name(name: str) -> str:
    """把所有非 [a-zA-Z0-9_-] 字符替换成 _，防止命名冲突或注入。"""
    return _DISALLOWED_CHARS.sub('_', name)


class MCPClient:
    """发现 + 调用外部 server 的工具。

    模拟 MCP 协议的两个核心操作：
      register()   → tools/list（发现有哪些工具）
      call_tool()  → tools/call（调用某个工具）
    """

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, Callable] = {}

    def register(self, tool_defs: list[dict],
                 handlers: dict[str, Callable]):
        """模拟 tools/list 发现。"""
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        """模拟 tools/call 调用。"""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP error: {e}"


# 已连接的 server：name → MCPClient
mcp_clients: dict[str, MCPClient] = {}


# ── mock servers（教学版模拟，真实版是 stdio JSON-RPC 子进程）──

def _mock_server_docs():
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search documentation. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version", "description": "Get API version. (readOnly)",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy():
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment. (destructive — requires approval in real CC)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
            {"name": "status", "description": "Check deployment status. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def connect_mcp(name: str) -> str:
    """连接一个 MCP server，发现其工具并加入工具池。"""
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        available = ", ".join(MOCK_SERVERS.keys())
        return f"Unknown server '{name}'. Available: {available}"
    mcp_client = factory()
    mcp_clients[name] = mcp_client
    tool_names = [t["name"] for t in mcp_client.tools]
    print(f"  \033[31m[mcp] connected: {name} → {tool_names}\033[0m")
    return (f"Connected to MCP server '{name}'. "
            f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")


def assemble_tool_pool() -> tuple[list[dict], dict]:
    """组装内置工具 + 所有已连接 MCP 工具为一个工具池。

    MCP 工具用 mcp__{server}__{tool} 前缀避免不同 server 工具名冲突。
    返回 (tools, handlers)。
    """
    from tools import TOOLS, TOOL_HANDLERS  # lazy import 避免循环依赖
    tools = list(TOOLS)
    handlers = dict(TOOL_HANDLERS)
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("inputSchema", {}),
            })
            # 默认参数绑定（c=mcp_client, t=tool_def["name"]），
            # 避免 lambda 闭包晚绑定导致所有工具都调用最后一个 server 的问题
            handlers[prefixed] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw:
                    c.call_tool(t, kw))
    return tools, handlers
