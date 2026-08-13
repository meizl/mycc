"""
MessageBus — 文件级邮箱系统，用于 agent 间通信。

每个 agent 一个 .jsonl 文件存在 .mailboxes/ 下。
读是消费性的：read_text + unlink，每条消息只投递一次。
"""

import json
import time
from pathlib import Path

WORKDIR = Path.cwd()
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """File-based message bus. Each agent has a .jsonl inbox.
    Read is destructive: read_text + unlink (consumes messages)."""

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
              f"{content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        """Read and consume inbox for agent. Returns list of messages."""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()  # consume: read + delete
        return msgs

    def peek(self, agent: str) -> bool:
        """Non-destructive: True if agent has unread inbox messages."""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        return inbox.exists() and inbox.stat().st_size > 0


# 全局单例
BUS = MessageBus()
