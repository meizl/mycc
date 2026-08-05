"""
Error Recovery — LLM 调用的错误恢复：max_tokens 截断、429/529 退避、prompt_too_long 压缩。

Path 1: max_tokens -> escalate 8K->64K -> continuation (max 3)
Path 2: prompt_too_long -> reactive compact -> retry (once) [TODO]
Path 3: 429/529 -> exponential backoff with jitter (max 10) -> fallback model
"""

import os
import random

# ── Path 1: max_tokens 恢复 ─────────────────────────────────

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 64000
MAX_CONTINUATIONS = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

# ── Path 3: 429/529 退避恢复 ────────────────────────────────

FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_CONSECUTIVE_529 = 3


class RecoveryState:
    """追踪恢复状态."""
    def __init__(self, primary_model: str):
        self.current_model = primary_model
        self.primary_model = primary_model
        self.consecutive_529 = 0


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """指数退避 + 随机抖动."""
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000.0
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def is_rate_limit_error(e: Exception) -> bool:
    """检测 429 限流."""
    name = type(e).__name__.lower()
    msg = str(e).lower()
    return "ratelimit" in name or "429" in msg


def is_overloaded_error(e: Exception) -> bool:
    """检测 529 过载."""
    name = type(e).__name__.lower()
    msg = str(e).lower()
    return "overloaded" in name or "529" in msg or "overloaded" in msg


def is_prompt_too_long_error(e: Exception) -> bool:
    """检测 prompt/context 过长错误."""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)
