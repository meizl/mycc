"""
Compact — 上下文压缩管道。

  messages[]
    ↓
  L3: tool_result_budget  — 本轮大结果存盘，替换为占位符（路径+预览）
  L1: snip_compact        — messages 超过 50 条时裁剪中间
  L2: micro_compact       — 旧 tool_results 替换为短占位符
    ↓
  L4: prepare_context_for_api — 读时投影（不改原始消息）
    ↓
  LLM call

L1/L2/L3 便宜先跑（规则），L4 按需投影（可能调模型）。
"""

import time
from pathlib import Path

WORKDIR = Path.cwd()
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

# ── 阈值 ──
MAX_MESSAGES = 50             # L1: 超过 50 条消息触发裁剪
KEEP_RECENT = 3               # L2: 可重跑工具保留最近 3 个 tool_result
MICRO_COMPACT_COOLDOWN = 3600 # L2: 冷却时间（秒），1 小时内不重复触发
PERSIST_THRESHOLD = 10_000    # L3: 单个结果 > 10k 字符才存盘
MAX_TOOL_RESULT_BYTES = 200_000  # L3: 本轮 tool_results 总大小上限

_last_micro_compact_time: float = 0.0  # L2: 上次 micro_compact 执行时间戳

# L2: 可重新获取结果的工具（幂等读取类）— 可以安全压缩
REOBTAINABLE_TOOLS = {
    "read_file", "glob", "grep", "ls", "bash", "diff",
    "web_fetch", "web_search",
}

# ── L4 阈值 ──
CONTEXT_WINDOW_TOKENS = 200_000     # 上下文窗口总大小
TOKEN_OVERHEAD = 25_000             # system + tools 固定开销 (~21k)
LIGHT_FOLD_PCT = 0.90               # 轻度折叠：旧 tool_result → 占位符
HEAVY_FOLD_PCT = 0.95               # 重度折叠：前 N 轮 → LLM 摘要
LIGHT_FOLD_KEEP = 8                 # 轻度折叠保留最近可重跑结果数
HEAVY_FOLD_RATIO = 0.6              # 重度折叠：折叠前 60% 的消息

# ── AutoCompact 阈值 ──
AUTOCOMPACT_THRESHOLD = 160_000   # L4 投影后 token 超这个绝对值 → 硬压缩
AUTOCOMPACT_KEEP_RECENT = 6       # 硬压缩保留最近 N 条消息
AUTOCOMPACT_MAX_TOKENS = 8000     # 子 agent 输出的摘要 token 上限
AUTOCOMPACT_MAX_RUNS = 3          # 最多触发 3 次，超过熔断

_autocompact_run_count = 0        # 当前会话已运行次数

# Collapse 记录持久化（跨 turn 保留，重启后重建）
_collapse_records: list = []   # [{type, range, summary}]


# ═══════════════════════════════════════════════════════════
#  辅助
# ═══════════════════════════════════════════════════════════

def estimate_size(msgs) -> int:
    """粗略估算 messages 的序列化大小。"""
    return len(str(msgs))


def _block_type(block):
    """兼容 dict 和 SDK 对象两种 block 格式。"""
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _message_has_tool_use(msg: dict) -> bool:
    """判断 assistant 消息是否包含 tool_use block。"""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(_block_type(b) == "tool_use" for b in content)


def _is_tool_result_message(msg: dict) -> bool:
    """判断 user 消息是否由 tool_result block 组成。"""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


# ═══════════════════════════════════════════════════════════
#  L3: tool_result_budget — 大结果存盘
# ═══════════════════════════════════════════════════════════

def persist_large_output(tool_use_id: str, output: str) -> str:
    """把大输出写磁盘，返回占位符文本。"""
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output)

    preview = output[:2000]
    return (
        f'<persisted-output path="{path}">\n'
        f"{preview}\n"
        f"</persisted-output>"
    )


def tool_result_budget(messages: list, max_bytes: int = MAX_TOOL_RESULT_BYTES) -> list:
    """原地压缩 messages 最后一条里的 tool_result block。

    只看最后一轮 tool_results，总大小超 max_bytes 时从最大的开始存盘。
    """
    if not messages:
        return messages

    last = messages[-1]
    if last.get("role") != "user" or not isinstance(last.get("content"), list):
        return messages

    blocks = [
        (i, b) for i, b in enumerate(last["content"])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    if not blocks:
        return messages

    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages

    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for idx, block in ranked:
        if total <= max_bytes:
            break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tool_use_id = block.get("tool_use_id", f"unknown_{idx}")
        block["content"] = persist_large_output(tool_use_id, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)

    return messages


# ═══════════════════════════════════════════════════════════
#  L1: snip_compact — 裁剪中间消息
# ═══════════════════════════════════════════════════════════

def snip_compact(messages: list, max_messages: int = MAX_MESSAGES) -> list:
    """消息数超过 max_messages 时，保留头尾、裁剪中间。

    保证裁剪边界不切断 tool_use / tool_result 配对。
    """
    if len(messages) <= max_messages:
        return messages

    keep_head, keep_tail = 3, max_messages - 3
    head_end = keep_head
    tail_start = len(messages) - keep_tail

    # 头部扩展：最后一条是 tool_use → 把后续的 tool_result 也保留
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    # 尾部扩展：第一条是 tool_result 且前一条是 tool_use → 把 tool_use 也拉进来
    if (tail_start > 0 and tail_start < len(messages)
            and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1

    if head_end >= tail_start:
        return messages

    snipped = tail_start - head_end
    return (
        messages[:head_end]
        + [{"role": "user", "content": f"[snipped {snipped} messages from conversation middle]"}]
        + messages[tail_start:]
    )


# ═══════════════════════════════════════════════════════════
#  L2: micro_compact — 旧 tool_result 替换为占位符
# ═══════════════════════════════════════════════════════════

def _build_tool_name_map(messages: list) -> dict:
    """扫描所有 assistant 消息，建立 tool_use_id → tool_name 映射。"""
    name_map = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _block_type(block) == "tool_use":
                tid = block.get("id") if isinstance(block, dict) else getattr(block, "id", None)
                tname = block.get("name") if isinstance(block, dict) else getattr(block, "name", None)
                if tid and tname:
                    name_map[tid] = tname
    return name_map


def collect_tool_results(messages: list):
    """遍历所有消息，找到所有 tool_result block，返回 (消息索引, block索引, block引用)。"""
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks


def micro_compact(messages: list) -> list:
    """可重跑工具只保留最近 KEEP_RECENT 个结果，旧的替换为短占位符。

    不可复现的工具（write_file, ask_user, task 等）— 永不压缩。
    不删除 block（保持 tool_use/tool_result 配对完整），只原地修改 content。

    冷却机制：两次触发间隔至少 MICRO_COMPACT_COOLDOWN 秒（默认 1 小时），
    避免频繁压缩。
    """
    global _last_micro_compact_time

    now = time.time()
    if now - _last_micro_compact_time < MICRO_COMPACT_COOLDOWN:
        return messages

    tool_results = collect_tool_results(messages)

    # 建立 tool_use_id → tool_name 映射，区分可重跑 vs 不可复现
    name_map = _build_tool_name_map(messages)

    reobtainable = [
        (mi, bi, block) for mi, bi, block in tool_results
        if name_map.get(block.get("tool_use_id", ""), "") in REOBTAINABLE_TOOLS
    ]

    if len(reobtainable) <= KEEP_RECENT:
        return messages

    for _, _, block in reobtainable[:-KEEP_RECENT]:
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"

    _last_micro_compact_time = now
    return messages


# ═══════════════════════════════════════════════════════════
#  L4: prepare_context_for_api — 读时投影，不修改原始消息
# ═══════════════════════════════════════════════════════════

def estimate_tokens(messages) -> int:
    """粗略 token 估算（~4 chars/token）。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("text", "")))
                    total += len(str(block.get("content", "")))
                    total += len(str(block.get("input", "")))
                elif hasattr(block, "text"):
                    total += len(str(block.text))
        else:
            total += len(str(content))
        total += len(str(msg.get("role", "")))
    return total // 4


def _context_usage(messages) -> float:
    """当前上下文使用率 (0.0 ~ 1.0+)。"""
    effective = CONTEXT_WINDOW_TOKENS - TOKEN_OVERHEAD
    return estimate_tokens(messages) / effective


def _light_fold(messages: list) -> list:
    """90% 轻度折叠：旧的 tool_result 替换为简短占位符。

    可重跑工具（read_file, bash 等）→ 保留最近 LIGHT_FOLD_KEEP 个。
    不可复现工具（write_file, ask_user, task 等）→ 全部保留。
    返回全新列表，不修改输入。
    """
    # 建立 tool_use_id → tool_name 映射
    name_map = _build_tool_name_map(messages)

    # 收集所有可重跑 tool_result 的位置
    reobtainable_positions = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                if name_map.get(tid, "") in REOBTAINABLE_TOOLS:
                    reobtainable_positions.append((mi, bi))

    keep_positions = set(reobtainable_positions[-LIGHT_FOLD_KEEP:]) if len(
        reobtainable_positions) > LIGHT_FOLD_KEEP else set(reobtainable_positions)

    # 构建投影视图
    result = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            result.append(msg)
            continue

        new_content = []
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                tool_name = name_map.get(tid, "")

                if tool_name in REOBTAINABLE_TOOLS and (mi, bi) not in keep_positions:
                    orig_len = len(str(block.get("content", "")))
                    if orig_len > 120:
                        new_block = dict(block)
                        new_block["content"] = (
                            f"[Collapsed tool result. "
                            f"Original: {orig_len} chars. Re-run if needed.]"
                        )
                        new_content.append(new_block)
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        result.append({**msg, "content": new_content})

    return result


def _heavy_fold(messages: list, client, model: str) -> list:
    """95% 重度折叠：调 LLM 将前 N 轮对话压缩为摘要。

    返回全新列表，不修改输入。摘要记录存入 _collapse_records。
    如果 client 不可用或调用失败，回退到 _light_fold。
    """
    if client is None:
        return _light_fold(messages)

    fold_count = int(len(messages) * HEAVY_FOLD_RATIO)
    if fold_count < 4:
        return list(messages)

    fold_range = messages[:fold_count]
    rest = messages[fold_count:]

    # 构建摘要 prompt
    lines = [
        "Summarize the following conversation between an AI coding agent and a user.",
        "Focus on: (1) key decisions made, (2) files modified and why,",
        "(3) current task state and pending work.",
        "Be concise but complete — this summary replaces the original messages",
        "in context, so retain anything the agent needs to continue working.",
        "",
        "Conversation:",
    ]
    for msg in fold_range:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(str(b.get("text", ""))[:400])
                    elif b.get("type") == "tool_use":
                        parts.append(f"[tool_use: {b.get('name', '?')}]")
                    elif b.get("type") == "tool_result":
                        preview = str(b.get("content", ""))[:150]
                        parts.append(f"[tool_result: {preview}]")
                elif hasattr(b, "type"):
                    if b.type == "text":
                        parts.append(str(b.text)[:400])
                    elif b.type == "tool_use":
                        parts.append(f"[tool_use: {b.name}]")
            content = " ".join(parts)
        lines.append(f"[{role}]: {str(content)[:800]}")

    lines.append("")
    lines.append("Provide a structured summary:")

    try:
        response = client.messages.create(
            model=model,
            system="You are a precise summarizer. Extract only factual information.",
            messages=[{"role": "user", "content": "\n".join(lines)}],
            max_tokens=2000,
        )
        summary_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                summary_text += block.text

        if not summary_text:
            raise ValueError("Empty summary from model")

        # 存入 collapse 记录
        _collapse_records.append({
            "type": "conversation_fold",
            "range": [0, fold_count - 1],
            "summary": summary_text,
        })

        marker = {
            "role": "user",
            "content": (
                f"[CONTEXT COLLAPSED: {fold_count} messages summarized below]\n\n"
                f"{summary_text}\n\n"
                f"[End of collapsed context. Continue from here.]"
            ),
        }
        return [marker] + rest

    except Exception as e:
        print(f"\n\033[93m[L4 heavy fold failed: {e}, falling back to light fold]\033[0m")
        return _light_fold(messages)


def prepare_context_for_api(messages: list, client=None, model=None) -> list:
    """动态投影压缩视图，不修改原始 messages。

    根据上下文使用率自动选择策略：
    - < 90%: 直接返回浅拷贝
    - 90-95%: 轻度折叠（旧 tool_result → 占位符）
    - >= 95%: 重度折叠（前 N 轮 → LLM 摘要）

    System prompt 和工具定义不变，最大化 prompt cache 命中。
    """
    usage = _context_usage(messages)

    if usage >= HEAVY_FOLD_PCT:
        print(f"\n\033[91m[Context: {usage:.0%} — heavy fold (LLM summary)]\033[0m")
        return _heavy_fold(messages, client, model)
    elif usage >= LIGHT_FOLD_PCT:
        print(f"\n\033[93m[Context: {usage:.0%} — light fold (tool outputs)]\033[0m")
        return _light_fold(messages)
    else:
        return list(messages)  # 浅拷贝


def get_collapse_records() -> list:
    """返回 collapse 记录列表，用于测试和调试。"""
    return list(_collapse_records)


# ═══════════════════════════════════════════════════════════
#  AutoCompact — 子 agent 硬压缩，重写 messages
# ═══════════════════════════════════════════════════════════

def _build_compact_prompt(messages: list) -> str:
    """将消息历史格式化为子 agent 的摘要 prompt。

    包含禁止工具调用、XML 输出格式要求。
    """
    formatted = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(str(b.get("text", ""))[:500])
                    elif b.get("type") == "tool_use":
                        args_preview = str(b.get("input", ""))[:200]
                        parts.append(f"[tool_use: {b.get('name', '?')}({args_preview})]")
                    elif b.get("type") == "tool_result":
                        preview = str(b.get("content", ""))[:300]
                        parts.append(f"[tool_result: {preview}]")
                elif hasattr(b, "type"):
                    if b.type == "text":
                        parts.append(str(b.text)[:500])
                    elif b.type == "tool_use":
                        parts.append(f"[tool_use: {b.name}]")
            content = " ".join(parts)
        formatted.append(f"<msg id=\"{i}\" role=\"{role}\">{str(content)[:800]}</msg>")

    return f"""You are summarizing a conversation between an AI coding agent and a user.

CRITICAL: Do NOT call any tools. Do NOT use tool_use. Just output XML text.

Your task: Read ALL messages below and produce a structured XML summary.
The summary will REPLACE these messages in the agent's context.
Include ALL important details — anything the agent might need later.

CONVERSATION TO SUMMARIZE:
<conversation>
{chr(10).join(formatted)}
</conversation>

OUTPUT THIS EXACT XML STRUCTURE (replace the placeholder text with your analysis):

<summary>
<primary-request>
What the user originally asked for. What is the main goal of this session? Be specific.
</primary-request>

<key-concepts>
Technical concepts, libraries, patterns, or technologies discussed. Include version numbers if mentioned.
</key-concepts>

<files>
Each file that was read, modified, or created. Include file paths and describe what changed or what was found.
</files>

<errors>
Every error encountered and how it was fixed. Include exact error messages if available.
</errors>

<problem-solving>
Problems that were analyzed and solved. Include the reasoning process and final solution.
</problem-solving>

<user-messages>
List all messages the user sent. Preserve their exact meaning.
</user-messages>

<pending-tasks>
Tasks mentioned but not yet completed. Include todo items if any.
</pending-tasks>

<current-work>
What was being worked on right before this compaction? Describe the exact state.
</current-work>

<next-step>
The single most important next action to continue the work.
</next-step>
</summary>

Remember: NO tool calls. Output ONLY the XML. Be thorough — these XML tags are the ONLY thing the agent will see from the compacted history."""


def _run_compact_subagent(client, model: str, prompt: str) -> str:
    """运行压缩专用子 agent：无工具、大 token、纯 XML 输出。

    与 spawn_subagent 不同：
    - 不带任何工具（禁止子 agent 做工具调用）
    - max_tokens 更大，给摘要足够空间
    - 专门用于上下文压缩场景
    """
    system = (
        "You are a context-compaction specialist. "
        "Your ONLY job: read conversation history and output a structured XML summary. "
        "You have NO tools. Just think, then output the XML. "
        "Be thorough — include ALL details the agent might need later. "
        "Output ONLY the XML tags, no preamble or postscript."
    )

    print("  \033[94m[Compact subagent] Analyzing history...\033[0m")

    response = client.messages.create(
        model=model,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=AUTOCOMPACT_MAX_TOKENS,
    )

    result = ""
    for block in response.content:
        if hasattr(block, "text"):
            result += block.text

    print(f"  \033[94m[Compact subagent] Summary: {len(result)} chars\033[0m")
    return result.strip()


def auto_compact(messages: list, client, model: str,
                 keep_recent: int = AUTOCOMPACT_KEEP_RECENT) -> list:
    """AutoCompact：子 agent 硬压缩历史为 XML 摘要，重写 messages。

    流程：
    1. 保留最近 keep_recent 条消息不动
    2. 旧消息发给压缩子 agent → XML 摘要
    3. messages 重写为: [边界标记, XML摘要, 最近消息]

    这是不可逆操作 — 原始消息被替换。
    与 L4 projectView 不同，auto_compact 直接修改 messages 本身。
    """
    global _autocompact_run_count

    total = len(messages)
    if total <= keep_recent:
        return messages

    # client 不可用时跳过（测试 / 调试场景，不消耗配额）
    if client is None:
        print("  \033[93m[AutoCompact] client=None，跳过压缩\033[0m")
        return messages

    # 熔断：超过最大次数不再压缩
    if _autocompact_run_count >= AUTOCOMPACT_MAX_RUNS:
        print(f"\n\033[91m[AutoCompact] 已触发 {_autocompact_run_count} 次，达到上限，熔断！\033[0m")
        print(f"\033[91m[AutoCompact] 不再压缩，继续运行（上下文可能溢出）\033[0m")
        return messages

    _autocompact_run_count += 1
    old = messages[:-keep_recent]
    recent = messages[-keep_recent:]

    print(f"\n\033[94m{'='*50}\033[0m")
    print(f"\033[94m[AutoCompact] 压缩上下文中... (第 {_autocompact_run_count}/{AUTOCOMPACT_MAX_RUNS} 次)\033[0m")
    print(f"\033[94m[AutoCompact] 压缩 {len(old)} 条旧消息 → 保留 {len(recent)} 条最近消息\033[0m")

    # 构建 prompt + 调用压缩子 agent
    prompt = _build_compact_prompt(old)
    summary = _run_compact_subagent(client, model, prompt)

    if not summary:
        print("  \033[93m[AutoCompact] 摘要为空，跳过压缩\033[0m")
        return messages

    # buildPostCompactMessages 等价逻辑:
    # [boundaryMarker, summaryMessages, ...recent]
    boundary = {
        "role": "user",
        "content": (
            "[CONTEXT COMPACTED]\n"
            "The conversation history before this point has been summarized below.\n"
            "The XML contains all key details from the compacted messages.\n"
            "Continue working from where you left off."
        ),
    }

    summary_msg = {
        "role": "user",
        "content": summary,
    }

    messages[:] = [boundary, summary_msg] + recent

    new_total = len(messages)
    print(f"\033[94m[AutoCompact] 完成: {total} 条 → {new_total} 条\033[0m")
    print(f"\033[94m{'='*50}\033[0m\n")

    return messages


# ═══════════════════════════════════════════════════════════
#  Pipeline — 统一入口，主循环只调这一个函数
# ═══════════════════════════════════════════════════════════

def compact_pipeline(messages: list, client, model: str) -> list:
    """上下文压缩管道：全部四层 + AutoCompact。

    原地修改 messages（L1/L2/L3），返回 API 就绪的投影视图。
    主循环只需调用这一行即可。
    """
    # 规则层：便宜，原地修改
    messages[:] = tool_result_budget(messages)   # L3: 大结果存盘
    messages[:] = snip_compact(messages)         # L1: 裁剪中间
    messages[:] = micro_compact(messages)        # L2: 旧结果占位

    # 投影层：不改原始，返回视图
    api_messages = prepare_context_for_api(messages, client, model)

    # 硬压缩：绝对阈值触发，子 agent 摘要
    if estimate_tokens(api_messages) >= AUTOCOMPACT_THRESHOLD:
        auto_compact(messages, client, model)
        api_messages = prepare_context_for_api(messages, client, model)

    return api_messages
