"""
Memory — persistent cross-session knowledge for the coding agent.

Storage:
    .memory/
      MEMORY.md          ← index (one line per memory)
      *.md               ← individual memory files (YAML frontmatter + Markdown)

Each turn in agent_loop:
    1. build_memory_system()       → appended to SYSTEM prompt
    2. load_memories(client, ...)  → select relevant + return content string
    3. inject_memories(api, ...)   → inject into API view (copy, never original)
    4. After turn: extract_memories(client, ...)   → new memories from dialogue
    5. Periodically: consolidate_memories(client, ...) → merge when ≥ threshold
    6. Periodically: forget_stale_memories() → ECS decay scoring, delete old
"""

import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"       # 记忆文件目录
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md" # 索引文件路径

CONSOLIDATE_THRESHOLD = 10  # 超过 10 条记忆触发合并

# 艾宾浩斯遗忘参数
FORGET_HALF_LIFE_DAYS = 7    # 半衰期：7 天无访问，记忆权重衰减到一半
FORGET_SCORE_THRESHOLD = 0.5  # 综合评分低于此值 → 遗忘（删除）
ACCESS_TRACKING_FILE = MEMORY_DIR / ".access.json"

# 读写锁：主线程只读，子线程只写，但写操作（write + rebuild + consolidate全量删）
# 和读操作（list + read）之间存在竞态。锁保护写操作，读侧拿到的要么是旧状态
# 要么是新状态，不会读到写了一半的中间态。
_memory_lock = threading.RLock()  # RLock: consolidate 持有锁时内部 write_memory_file 可重入


# ═══════════════════════════════════════════════════════════
#  遗忘追踪 — 访问时间 + 次数 → 艾宾浩斯衰减评分 → 遗忘
# ═══════════════════════════════════════════════════════════

def _load_access_tracking() -> dict:
    """
    加载访问追踪数据。

    文件格式 (.memory/.access.json):
        {
          "user-pref-language.md": {
            "access_count": 5,
            "created_at": 1722800000,
            "last_accessed_at": 1722960000
          },
          ...
        }

    文件不存在或损坏时返回空 dict。
    """
    if not ACCESS_TRACKING_FILE.exists():
        return {}
    try:
        return json.loads(ACCESS_TRACKING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_access_tracking(data: dict):
    """保存访问追踪数据到磁盘（原子写：先写临时文件再 rename）。"""
    MEMORY_DIR.mkdir(exist_ok=True)
    tmp = ACCESS_TRACKING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(ACCESS_TRACKING_FILE)  # rename 是原子操作


def _record_access(filename: str):
    """
    记录一次记忆访问：次数 +1，更新最后访问时间。

    调用时机: select_relevant_memories 选中某条记忆后。
    和记忆文件读写用同一把锁保护。
    """
    now = time.time()
    with _memory_lock:
        tracking = _load_access_tracking()
        entry = tracking.get(filename, {})
        entry["access_count"] = entry.get("access_count", 0) + 1
        entry["last_accessed_at"] = now
        if "created_at" not in entry:
            entry["created_at"] = now
        tracking[filename] = entry
        _save_access_tracking(tracking)


def _compute_forget_score(entry: dict, now: float) -> float:
    """
    艾宾浩斯风格衰减评分。

    公式:
        days_since = (now - last_accessed_at) / 86400
        retention = 0.5 ** (days_since / FORGET_HALF_LIFE_DAYS)
        score = access_count * retention

    含义:
        - 刚访问过 → retention ≈ 1.0 → score ≈ access_count
        - 7 天没访问 → retention = 0.5 → score 折半
        - 14 天没访问 → retention = 0.25 → score 再折半
        - 访问次数多 → score 基数高，更抗衰减

    返回: float 评分，越高越不容易被遗忘。
    """
    days_since = max(0, (now - entry.get("last_accessed_at", 0)) / 86400)
    retention = 0.5 ** (days_since / FORGET_HALF_LIFE_DAYS)
    access_count = entry.get("access_count", 0)
    return access_count * retention


def forget_stale_memories():
    """
    遍历所有记忆，评分低于阈值的删除。

    只处理有访问记录的记忆（至少被选中过一次）。
    从未被访问过的记忆（刚提取的新记忆）不参与遗忘评估——
    给它们一个"学习期"。

    评分规则:
        score = access_count × 0.5^(days_since_last_access / 7)
        score < FORGET_SCORE_THRESHOLD → 删除

    调用时机: 合并后或每 N 轮检查一次。
    副作用: 删除记忆文件 + 清理追踪记录 + 重建索引。
    """
    tracking = _load_access_tracking()
    if not tracking:
        return

    files = list_memory_files()
    if not files:
        return

    now = time.time()
    to_delete = []

    for f in files:
        entry = tracking.get(f["filename"])
        if entry is None:
            continue  # 从未被访问过，保留（新记忆保护期）
        score = _compute_forget_score(entry, now)
        if score < FORGET_SCORE_THRESHOLD:
            to_delete.append(f["filename"])

    if not to_delete:
        return

    with _memory_lock:
        for filename in to_delete:
            path = MEMORY_DIR / filename
            if path.exists():
                path.unlink()
            tracking.pop(filename, None)
        _save_access_tracking(tracking)
        _rebuild_index_locked()

    print(f"\n\033[33m[Memory: forgot {len(to_delete)} stale]\033[0m")


# ═══════════════════════════════════════════════════════════
#  File I/O — 磁盘读写，不涉及 LLM
# ═══════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple:
    """
    解析记忆文件的 YAML frontmatter，返回 (meta, body) 两个部分。

    frontmatter 格式:
        ---
        name: xxx
        description: xxx
        type: user
        ---

        正文 markdown...

    调用者: _rebuild_index, list_memory_files
    纯字符串解析，不读写磁盘。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """
    写入一条记忆文件到磁盘，然后自动重建索引。
    ...
    这是唯一的写入入口——没有单独的 update/delete。
    """
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    with _memory_lock:
        MEMORY_DIR.mkdir(exist_ok=True)
        filepath.write_text(
            f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
        )
        _rebuild_index_locked()
    return filepath


def _rebuild_index_locked():
    """
    扫描 .memory/*.md 重建 MEMORY.md。调用方必须持有 _memory_lock。

    锁由 write_memory_file 和 consolidate_memories 的上层获取，
    保证索引和文件一起原子更新，不会让读侧看到不一致状态。
    """
    MEMORY_DIR.mkdir(exist_ok=True)
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")


def read_memory_index() -> str:
    """
    读取 MEMORY.md 索引文件内容。加锁防止读到子线程写到一半的内容。

    调用者: build_memory_system（唯一调用者）
    """
    with _memory_lock:
        if not MEMORY_INDEX.exists():
            return ""
        text = MEMORY_INDEX.read_text().strip()
    return text if text else ""


def read_memory_file(filename: str) -> Optional[str]:
    """
    读取单条记忆文件的完整内容（含 frontmatter + markdown body）。

    参数:
        filename: 文件名，如 "user-pref-language.md"

    返回:
        文件内容字符串，文件不存在返回 None

    调用者: build_memory_system, load_memories
    纯磁盘 I/O，不涉及 LLM。
    """
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()


def list_memory_files() -> list:
    """
    列出所有记忆文件的元信息，返回 list[dict]。加锁防止 consolidate
    删文件时读侧遍历到不存在的文件或读到写了一半的内容。

    调用者: build_memory_system, select_relevant_memories,
            extract_memories, consolidate_memories
    """
    result = []
    with _memory_lock:
        if not MEMORY_DIR.exists():
            return result
        for f in sorted(MEMORY_DIR.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            raw = f.read_text()
            meta, body = _parse_frontmatter(raw)
            result.append({
                "filename": f.name,
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": meta.get("type", "user"),
                "body": body,
            })
    return result


# ═══════════════════════════════════════════════════════════
#  System prompt — 构建 SYSTEM 的记忆部分，启动时调用
# ═══════════════════════════════════════════════════════════

def build_memory_system() -> str:
    """
    构建注入 system prompt 的记忆段落。启动时调用一次。

    只包含 type=user 的全文（用户画像/偏好），无条件注入，每轮必现。
    其他类型（project/feedback/reference）暂不注入 system，
    后续通过 load_memories 按需加载。
    无记忆时返回空字符串。

    调用者: agent_loop.py 模块级初始化（启动时执行一次）
    副作用: 无。纯读磁盘 + 字符串拼接。
    """
    sections = []

    # Part 1: user profile — always inject full content
    all_files = list_memory_files()
    user_files = [f for f in all_files if f["type"] == "user"]
    if user_files:
        profile_parts = []
        for f in user_files:
            profile_parts.append(read_memory_file(f["filename"]) or "")
        profile_text = "\n\n".join(p for p in profile_parts if p)
        if profile_text:
            sections.append(
                "<user_profile>\n"
                "These are persistent user preferences. "
                "Always respect them — they apply to EVERY turn.\n\n"
                f"{profile_text}\n"
                "</user_profile>"
            )

    # Part 2: index of other types — currently not injected
    # (only user profile goes into SYSTEM; other types will be loaded
    #  on-demand via load_memories when that feature is re-enabled)

    return "\n\n".join(sections) if sections else ""


# ═══════════════════════════════════════════════════════════
#  Selection & Loading — 检索 + 注入，每 turn 开始时执行
# ═══════════════════════════════════════════════════════════

def _extract_text(content) -> str:
    """
    从各种格式的 content 中提取纯文本。

    兼容三种格式:
        - str:     直接返回
        - list[dict]: 遍历，取 type=="text" 的 block["text"]
        - list[SDK object]: 遍历，取 type=="text" 的 block.text
        - 其他:    str() 兜底

    调用者: select_relevant_memories, extract_memories
    纯数据转换，不读写磁盘、不调 LLM。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(str(b.get("text", "")))
        elif hasattr(b, "type") and b.type == "text":
            parts.append(str(b.text))
    return " ".join(parts)


def select_relevant_memories(client, model: str, messages: list,
                             max_items: int = 5) -> list:
    """
    从记忆中选出与当前对话相关的文件名列表。load_memories 的子步骤。

    匹配策略（两层，上层优先）:
        1. LLM 匹配: 把最近 3 条用户消息 + 记忆目录（name + description）
           发给一个小 LLM 调用 (max_tokens=200)，让它返回相关索引
           [0, 3, 5] → 映射回文件名列表
        2. 关键词 fallback: LLM 调用失败时，把用户消息拆成长度 >3 的词，
           和每条记忆的 name + description 做子串匹配

    参数:
        client:     Anthropic client（为 None 时跳过 LLM，直接走 fallback）
        model:      模型 ID
        messages:   完整对话历史
        max_items:  最多返回几条

    返回:
        list[str]: 记忆文件名列表，如 ["user-pref-language.md", "project-py39.md"]
                   无匹配返回 []

    调用者: load_memories（唯一调用者）
    LLM 调用: 1 次 max_tokens=200（小模型也能做）
    磁盘读写: 通过 list_memory_files 读一次目录
    """
    files = list_memory_files()
    if not files:
        return []

    # Step 1: 收集最近 3 条用户消息，截取前 2000 字符
    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = _extract_text(msg.get("content", ""))
            if text.strip():
                recent_texts.append(text)
            if len(recent_texts) >= 3:
                break
    recent = " ".join(reversed(recent_texts))[:2000]

    if not recent.strip():
        return []

    # Step 2: 构建记忆目录（编号 + name + description）
    catalog_lines = [
        f"{i}: {f['name']} — {f['description']}"
        for i, f in enumerate(files)
    ]
    catalog = "\n".join(catalog_lines)

    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select ONLY the indices of memories that are DEFINITELY relevant. "
        "Be conservative: prefer fewer selections over wrong ones. "
        "If you are not sure about a memory, skip it. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are clearly relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    # Step 3: LLM 匹配
    if client is not None:
        try:
            response = client.messages.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
            )
            text = _extract_text(response.content).strip()
            match = re.search(r'\[.*?\]', text, re.DOTALL)
            if match:
                indices = json.loads(match.group())
                selected = []
                for idx in indices:
                    if isinstance(idx, int) and 0 <= idx < len(files):
                        selected.append(files[idx]["filename"])
                        if len(selected) >= max_items:
                            break
                for filename in selected:
                    _record_access(filename)
                return selected
        except Exception:
            pass

    # Step 4: 关键词 fallback（LLM 失败或 client 为空时）
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break

    # Step 5: 记录每条被选中记忆的访问
    for filename in selected:
        _record_access(filename)

    return selected


def load_memories(client, model: str, messages: list) -> str:
    """
    选出相关记忆，读出全文，拼成格式化文本，准备注入上下文。

    流程:
        1. select_relevant_memories() → 选出相关记忆的文件名
        2. read_memory_file()         → 逐个读出完整内容
        3. 拼接为:
            <relevant_memories>
            ---\nname: ...\n---\n正文...
            ---\nname: ...\n---\n正文...
            </relevant_memories>

    参数:
        client, model, messages: 透传给 select_relevant_memories

    返回:
        格式化记忆文本，无相关记忆或 client=None 时返回 ""
        注意: user 类型的记忆不在这里返回——它们已经在 build_memory_system()
        里进了 system prompt，这里只处理 project/feedback/reference 类型

    调用者: agent_loop.agent_loop（每 turn 开始时调用一次）
    LLM 调用: 1 次（通过 select_relevant_memories, max_tokens=200）
    磁盘读写: 通过 select_relevant_memories + read_memory_file 读磁盘
    """
    if client is None:
        return ""
    selected_files = select_relevant_memories(client, model, messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def inject_memories(api_messages: list, memories_content: str) -> list:
    """
    把 load_memories 返回的记忆文本注入到 API 消息副本中。

    注入位置: 最后一条 role="user" 且 content 是字符串的消息前面。
    这通常是当前 turn 的用户 query。

    为什么注入到 user 消息而不是 system:
        - system 是静态的（cache 友好）
        - 记忆选择和对话相关，放 user 消息更自然
        - 不会破坏 system prompt 的缓存

    为什么返回副本:
        - 不污染原始 messages（后续压缩管线还会用到原始消息）

    参数:
        api_messages:     compact_pipeline 返回的 API 视图消息列表
        memories_content: load_memories 返回的格式化记忆文本

    返回:
        list: 注入后的新消息列表（shallow copy）
              如果 memories_content 为空或无合适的 user 消息，返回原列表

    调用者: agent_loop.agent_loop（LLM 调用前）
    副作用: 无。纯字符串拼接 + list copy。
    """
    if not memories_content:
        return api_messages

    # 从后往前找——当前 query 通常是最后一条 user 消息
    for i in range(len(api_messages) - 1, -1, -1):
        msg = api_messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            result = list(api_messages)  # shallow copy 外层 list
            result[i] = {
                **msg,
                "content": memories_content + "\n\n" + msg["content"],
            }
            return result

    # 没找到合适的 user 消息（比如当前轮在工具执行中）→ 不注入
    return api_messages


# ═══════════════════════════════════════════════════════════
#  Extraction — 每 turn 结束时从对话提取新记忆
# ═══════════════════════════════════════════════════════════

def extract_memories(client, model: str, messages: list):
    """
    从最近对话中提取新记忆，写入磁盘。每 turn 结束时调用。

    流程:
        1. 取 messages 最后 10 条对话，拼成 "role: text" 的文本
        2. 列出已有记忆的 name + description（发给 LLM 做去重参考）
        3. LLM 调用 (max_tokens=800): 提取新偏好/约束/事实
        4. 解析 LLM 返回的 JSON，逐条写入磁盘

    关键设计: 用压缩前的 messages（pre_compress 快照）调用，
        保证从完整对话中提取，不会因为压缩丢失细节。

    LLM prompt 要求:
        - 已有记忆已覆盖的 → 返回 []
        - 没有新信息 → 返回 []
        - 每条记忆必须有 description 和 body

    参数:
        client:   Anthropic client
        model:    模型 ID
        messages: 对话历史（应该是压缩前的原始快照）

    返回: None（副作用: 写入磁盘）

    调用者: agent_loop.agent_loop（turn 结束时）
    LLM 调用: 1 次 max_tokens=800
    磁盘读写: 通过 list_memory_files 读，write_memory_file 写
    异常处理: 静默吞掉（不影响主流程）
    """
    if client is None:
        return

    # Step 1: 收集最近 10 条对话
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        text = _extract_text(msg.get("content", ""))
        if text.strip():
            dialogue_parts.append(f"{role}: {text}")
    dialogue = "\n".join(dialogue_parts)

    if not dialogue.strip():
        return

    # Step 2: 列出已有记忆，让 LLM 做去重
    existing = list_memory_files()
    existing_desc = (
        "\n".join(f"- {m['name']}: {m['description']}" for m in existing)
        if existing else "(none)"
    )

    # Step 3: LLM 提取
    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.messages.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        text = _extract_text(response.content).strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return

        # Step 4: 逐条写入磁盘
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new]\033[0m")
    except Exception:
        pass  # 提取失败不影响主流程


# ═══════════════════════════════════════════════════════════
#  Consolidation — 记忆 ≥ 阈值时去重合并
# ═══════════════════════════════════════════════════════════

def consolidate_memories(client, model: str):
    """
    当记忆文件数 ≥ CONSOLIDATE_THRESHOLD (10) 时触发合并。

    流程:
        1. 把所有记忆的全文拼成 catalog
        2. LLM 调用 (max_tokens=3000): 合并重复、删过时、压缩至 ≤30 条
        3. 删除 .memory/ 下所有 .md 文件（保留 MEMORY.md）
        4. 写入 LLM 返回的合并后记忆
        5. 重建索引

    合并规则 (在 LLM prompt 里):
        1. 合并重复的 → 合并为一条
        2. 删除过时/矛盾的 → 删除
        3. 总数 ≤ 30 条
        4. 用户偏好（type=user）优先级最高，优先保留

    参数:
        client: Anthropic client
        model:  模型 ID

    返回: None（副作用: 全量重写 .memory/ 目录）

    调用者: agent_loop.agent_loop（每 turn 结束后检查）
    LLM 调用: 1 次 max_tokens=3000（不常触发）
    磁盘读写: 先读全部文件，后全量删了重写
    异常处理: 静默吞掉（合并失败不影响主流程，下次再试）
    """
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return
    if client is None:
        return

    # Step 1: 把所有记忆拼成 catalog
    catalog = "\n\n".join(
        f"## {f['filename']}\n"
        f"name: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )

    # Step 2: LLM 合并
    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.messages.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        text = _extract_text(response.content).strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        # Step 3+4: 全量删旧写新 — 锁保护，读侧不会看到中间态
        with _memory_lock:
            for f in MEMORY_DIR.glob("*.md"):
                if f.name != "MEMORY.md":
                    f.unlink()

            for mem in items:
                name = mem.get("name", f"memory_{int(time.time())}")
                mem_type = mem.get("type", "user")
                desc = mem.get("description", "")
                body = mem.get("body", "")
                if desc and body:
                    # write_memory_file 内部也获取 _memory_lock，RLock 可重入
                    write_memory_file(name, mem_type, desc, body)

        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)}]\033[0m")
    except Exception:
        pass  # 合并失败不影响主流程


# ═══════════════════════════════════════════════════════════
#  Hook — 通过 Stop 钩子异步提取记忆，不阻塞主循环
# ═══════════════════════════════════════════════════════════

def _extract_in_background(client, model: str, messages: list):
    """
    在后台 daemon 线程中执行 extract + consolidate。

    为什么用子线程:
        - extract_memories 和 consolidate_memories 各调一次 LLM
        - 同步等的话用户要卡几百毫秒到几秒才能看到模型回复
        - 丢到后台，主循环立刻返回，用户马上能输入下一条

    为什么 deepcopy:
        - messages 在线程执行期间可能被主循环修改（下一轮压缩等）
        - daemon 线程: 主进程退出时自动终止，不会 hang

    异常处理:
        - 子线程异常静默吞掉，不影响主流程
    """
    snapshot = copy.deepcopy(messages)

    def _run():
        try:
            extract_memories(client, model, snapshot)
            consolidate_memories(client, model)
            forget_stale_memories()
        except Exception:
            pass  # 后台提取失败不影响主流程

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def register_memory_hooks(client, model: str):
    """
    注册记忆相关的 Stop 钩子到 hooks 系统。

    用法: 在 agent_loop 初始化时调用一次
        register_memory_hooks(client, MODEL)

    注册的钩子:
        Stop → 每当 agent turn 结束时（stop_reason != "tool_use"）触发
            在后台 daemon 线程中执行:
            1. deepcopy(messages) 做快照
            2. extract_memories → 从对话提取新记忆
            3. consolidate_memories → 超阈值时合并

    钩子返回 None，不注入消息也不影响其他 Stop 钩子（如 summary 统计）。
    """
    from hooks import register_hook

    def stop_hook(messages: list):
        """Stop hook: agent 结束当前 turn 时触发。"""
        _extract_in_background(client, model, messages)
        return None  # 不注入消息，让其他 Stop 钩子继续执行

    register_hook("Stop", stop_hook)
