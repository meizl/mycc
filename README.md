# mycc

mycc（My Coding Companion）是一个从零手写的极简 AI 编程智能体，用作学习项目。它逐章复刻 Claude Code 的架构（对应参考项目 `learn_claude_code` 的 s01→s19）：流式 agent 循环、多工具调用、上下文压缩、持久化记忆、子 agent、后台任务、cron 定时、agent 团队、自治 worker，以及 MCP 外接工具。

基于 Anthropic API，流式输出。

## 快速开始

```bash
pip install -r requirements.txt

# 在 .env 中配置：
#   MODEL_ID           模型 ID
#   ANTHROPIC_BASE_URL API 地址
#   ANTHROPIC_API_KEY  API 密钥
cd mycc && python3 -m app.agent_loop
```

启动后输入问题回车发送，输入 `q` 退出。

本项目没有测试。验证方式为手动运行 `python3 -m app.agent_loop`，或用 `python3 -c "import <模块>"` 做导入/语法检查。

## 架构总览

所有模块平铺在 `mycc/app/` 下。入口 `agent_loop.py` 是编排者，其余文件是它导入的能力模块。

| 模块 | 职责 |
|------|------|
| `agent_loop.py` | 流式 agent 循环（编排者）：事件驱动入口、动态工具池、handler 注入 |
| `tools.py` | 全部工具定义 + 实现：文件/执行/网络 + 磁盘任务系统 + 后台任务 + 协议工具 |
| `compact.py` | 4 层上下文压缩管道（L1-L4）+ AutoCompact |
| `subagent.py` | 隔离子 agent（全新上下文、有限工具、只返回摘要） |
| `skill_loader.py` | 技能系统：两级注入 + 自进化（创建/改进技能） |
| `memory.py` | 跨会话持久化记忆（.memory/，Ebbinghaus 遗忘衰减） |
| `hooks.py` | 4 个生命周期事件的发布/订阅钩子 |
| `error_recovery.py` | LLM 调用失败恢复：max_tokens 升级、429/529 退避、模型切换 |
| `cron.py` | 4 层定时任务管道，子 agent 隔离执行 |
| `message_bus.py` | 文件邮箱系统，agent 间通信（消费性读取） |
| `protocol.py` | 请求/响应协议状态机，request_id 关联 |
| `teammate.py` | 自治 worker（WORK/IDLE 生命周期、自动认领任务） |
| `mcp.py` | MCP 外接工具：连接、发现、组装工具池 |

### 核心循环

```
用户输入 → LLM（流式 + 工具）→ 执行工具调用 → 重复
```

关键行为：

- **事件驱动入口**：`queue.Queue` 统一两个来源 —— 用户输入（`input_reader` 线程）和异步事件（`inbox_poller` 线程，teammate 消息 / 后台任务完成时唤醒）
- **CLAUDE.md 自动加载**：从 WORKDIR 向上查找 CLAUDE.md 注入 SYSTEM（截断 8000 字符）
- **Nag 提醒**：3 轮未更新 todo 则注入提醒
- **动态工具池**：`assemble_tool_pool()` 组装内置 + MCP 工具，`connect_mcp` 后重建
- **handler 注入**：`task` 和 `spawn_teammate` 的 handler 在这里注入（因为需要 `client` 和 `MODEL`）

### 任务系统（两类，易混淆）

- **`todo_write`**：会话级、内存中的步骤规划（对应 Claude Code 的 todo）
- **`task_create`/`task_list`/`task_update`/`get_task`**：磁盘持久化（`.tasks/task_*.json`），带 `status`（pending/in_progress/done/cancelled）、`owner`、`blockedBy` 依赖图。`claim_task`/`complete_task`/`scan_unclaimed_tasks` 支持自治 worker 认领。

### 自治 worker（teammate）

WORK/IDLE 生命周期：

```
WORK: 收件箱 → LLM → 工具 → (还有工具调用? 继续) → (干完? → IDLE)
IDLE: 每 5s 轮询 → 收件箱有消息? → WORK
                  → 有无人认领任务? → 自动认领 → WORK
                  → 60s 超时? → 退出
```

Leader 通过协议（`request_shutdown`、`request_plan`/`review_plan`）与 worker 通信，用 `request_id` 关联请求与响应。

### MCP 外接工具

```
connect_mcp("docs") → MCPClient 发现工具 →
assemble_tool_pool() → [内置..., mcp__docs__search, mcp__docs__get_version]
```

MCP 工具用 `mcp__{server}__{tool}` 前缀避免命名冲突。教学版用 mock handler 模拟外部 server（docs/deploy），真实版通过 stdio JSON-RPC 子进程通信。MCP 工具仅 Lead 可用，teammate 用固定子集。

## 关键设计决策

1. **流式优先**：token 逐字输出，工具输入通过 `input_json_delta` 累积
2. **上下文压缩分层**：L1-L3 原地修改消息（廉价规则），L4 返回只读投影（从不修改），AutoCompact 原地重写（昂贵，最后手段）
3. **记忆异步**：提取和合并跑在后台 daemon 线程，不阻塞用户
4. **子 agent 隔离**：全新上下文、禁止递归、有限工具集
5. **技能懒加载 + 自进化**：名字常驻（~50-100 tokens），全文按需加载；agent 自主创建/修补技能
6. **动态工具池**：MCP 工具运行时增删，所以 agent_loop 重建 tools/handlers 而非缓存
7. **agent 通信 = 文件 + 协议关联**：mailbox 传输，request_id 匹配请求响应
8. **自治 worker 闲时不退出**：轮询接单（自动认领无人任务），仅收到关闭请求或空闲超时才退出

## 约定

- 所有路径相对 `WORKDIR`（`Path.cwd()`）
- 工具用 snake_case；Python 3.9+ 兼容（用 `Optional[str]`，不用 `str | None`）
- block 格式双兼容：`dict` 和 SDK 对象两种访问都支持（`block["name"]` / `block.name`）
- **循环导入用懒导入打破**：`mcp.py` 在函数内 import `tools`，`cron.py` 在函数内 import `subagent`
- 打印用 ANSI 颜色：cyan 输入提示、yellow 工具名、blue 压缩、gray hooks/meta、magenta/purple cron/subagent/teammate、red 错误/协议拒绝
- `.tasks/` `.mailboxes/` `.memory/` `.task_outputs/` `.scheduled_tasks.json` 是运行时状态，已 gitignore
