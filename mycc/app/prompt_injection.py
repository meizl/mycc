"""
Prompt Injection Defense — 在真正调用 LLM 前净化最后一条用户消息。

管道（对最后一条 user 消息）：
  1. 正则匹配 43 个黑名单标签
  2. 命中则把 < > 转义成 &lt; &gt;，让标签失效
  3. 用 --- BEGIN/END USER INPUT --- 边界标记包裹
  4. 交给 handler（真正的 LLM 调用）

设计约束：
  - 不改原始数据：转义只发生在净化后的深拷贝副本上，绝不写回源 messages
  - 原文通过 ORIGINAL_USER_CONTENT_KEY 保留，供下游中间件取用
  - 出错放行（fail-open）：任何异常都透传原始消息，宁可不防御也不让请求崩溃
"""

import copy
import re

# ── 43 个黑名单标签 ─────────────────────────────────────────
# 三类来源：本 harness 内部注入标签 + Anthropic/Claude Code 内部标签 + 常见注入家族
BLACKLIST_TAGS = [
    # 本 harness 注入标签（agent_loop / teammate / cron 用）
    "reminder", "project-context", "inbox", "task_notification",
    "cron_notification", "identity", "auto-claimed",
    "plan_approval_request", "plan_approval_response",
    "shutdown_request", "shutdown_response",
    # Anthropic / Claude Code 内部标签
    "system_reminder", "user_reminder", "system", "user", "assistant",
    "function_calls", "function_results", "tool_use", "tool_result",
    "thinking", "system_prompt", "user_prompt", "context", "rules",
    "output_format",
    # antml 越狱家族（常见注入前缀）
    "antml:thinking", "antml:function_calls", "antml:function_results",
    "antml:invoke", "antml:parameter", "antml:system", "antml:user",
    "antml:assistant", "antml:tool_use", "antml:tool_result",
    # 其他注入指令标签
    "instructions", "directive", "override", "ignore",
    "memory", "knowledge", "constraints",
]

# 下游中间件取原文用的键
ORIGINAL_USER_CONTENT_KEY = "__original_user_content__"

BEGIN_MARKER = "--- BEGIN USER INPUT ---"
END_MARKER = "--- END USER INPUT ---"


def _build_tag_re(tags):
    """编译黑名单标签正则：匹配 <tag>、</tag>、<tag attr="..."> 等。"""
    alts = [rf"</?{re.escape(t)}\b" for t in tags]
    return re.compile("|".join(alts), re.IGNORECASE)


_TAG_RE = _build_tag_re(BLACKLIST_TAGS)


def sanitize_user_content(content: str):
    """净化一段文本。返回 (净化后文本, 命中的标签列表)。

    无黑名单标签命中则原样返回（避免误伤正常输入里的尖括号）。
    """
    if not isinstance(content, str):
        return content, []
    hits = _TAG_RE.findall(content)
    if not hits:
        return content, hits
    escaped = content.replace("<", "&lt;").replace(">", "&gt;")
    return f"{BEGIN_MARKER}\n{escaped}\n{END_MARKER}", hits


def sanitize_last_user_message(messages):
    """深拷贝 messages 并净化最后一条 user 消息。

    - 源 messages 不被修改（转义只作用于副本）
    - 原文存在副本消息的 ORIGINAL_USER_CONTENT_KEY 字段下
    - 兼容 string 内容（用户输入）和 list 内容（工具结果块）
    """
    if not isinstance(messages, list):
        return messages
    sanitized = copy.deepcopy(messages)
    for msg in reversed(sanitized):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            new_content, hits = sanitize_user_content(content)
            if hits:
                msg[ORIGINAL_USER_CONTENT_KEY] = content
                msg["content"] = new_content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    new_text, hits = sanitize_user_content(text)
                    if hits:
                        block[ORIGINAL_USER_CONTENT_KEY] = text
                        block["text"] = new_text
        break  # 只处理最后一条 user 消息
    return sanitized


def _strip_original_key(messages):
    """调用 LLM 前剥离 ORIGINAL_USER_CONTENT_KEY，避免污染 API 请求。"""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        msg.pop(ORIGINAL_USER_CONTENT_KEY, None)
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict):
                    block.pop(ORIGINAL_USER_CONTENT_KEY, None)
    return messages


def wrap_model_call(handler):
    """包装 model call：净化最后一条用户消息后交给真正的 handler。

    出错放行：sanitize 抛异常时透传原始 messages，不因防御而崩溃。
    """
    def wrapped(*args, **kwargs):
        messages = kwargs.get("messages")
        if messages is None:
            return handler(*args, **kwargs)  # 没有 messages 参数，直接放行
        try:
            sanitized = sanitize_last_user_message(messages)
            kwargs["messages"] = _strip_original_key(sanitized)
        except Exception:
            pass  # fail-open：宁可不防御
        return handler(*args, **kwargs)
    return wrapped
