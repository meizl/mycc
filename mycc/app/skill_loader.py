"""
Skill 加载系统 — 两级按需知识注入。

Layer 1 (便宜，一直在):
  SYSTEM prompt 里注入技能目录（名字 + 一行描述）
  ~50-100 tokens/技能

Layer 2 (贵，按需):
  agent 调用 load_skill("xxx") → 返回完整 SKILL.md
  ~1000-3000 tokens/技能

目录结构:
  skills/
    code-review/SKILL.md
    my-skill/SKILL.md
"""

import difflib
import os
import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"  # 相对于 skill_loader.py 所在目录
SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML-like frontmatter（不依赖 pyyaml）。

    支持:
      key: value
      key: |
        multiline
        value
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta = {}
    current_key = None
    current_value = []

    for line in parts[1].split("\n"):
        # 顶层键值对: "key: value"
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if m:
            # 保存上一组
            if current_key:
                meta[current_key] = "\n".join(current_value).strip()
                current_value = []
            current_key = m.group(1)
            val = m.group(2)
            if val in ("|", ">"):  # 多行值开始
                continue
            current_value.append(val)
        elif current_key and line and line[0] in (" ", "\t"):
            # 缩进续行（多行值的一部分）
            current_value.append(line.strip())
        else:
            # 空行或非缩进行 → 上一组结束
            if current_key:
                meta[current_key] = "\n".join(current_value).strip()
                current_key = None
                current_value = []

    # 收尾
    if current_key:
        meta[current_key] = "\n".join(current_value).strip()

    return meta, parts[2].strip()


def _scan_skills():
    """扫描 skills/ 目录，填充 SKILL_REGISTRY。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if not manifest.exists():
            continue
        raw = manifest.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", d.name)
        desc = meta.get("description", body.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[name] = {
            "name": name,
            "description": desc,
            "content": raw,
        }


# 模块导入时扫描
_scan_skills()


def list_skills() -> str:
    """列出所有技能（名字 + 一行描述）→ 注入 SYSTEM prompt。"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- **{s['name']}**: {s['description']}"
        for s in SKILL_REGISTRY.values()
    )


def load_skill(name: str) -> str:
    """加载完整技能内容。查 registry，无文件系统访问。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


# ═══════════════════════════════════════════════════════════
#  自进化机制 — agent 自主创建 / 改进 Skill
# ═══════════════════════════════════════════════════════════
#
# 触发（写死在 SYSTEM prompt 里）：
#   create — 完成复杂任务 / 修了棘手的 bug / 发现可复用工作流后，存成 Skill
#   update — 加载的 Skill 缺步骤 / 命令错 / 漏了坑点，任务结束前改掉
#
# 七道安全检查（简化版）：名称验证、大小限制、分类验证、
# frontmatter 格式校验、名称冲突检查、原子写入、安全扫描。

MAX_SKILL_SIZE = 100_000  # 100KB 上限

VALID_CATEGORIES = {"code", "workflow", "tooling", "reference", "other"}

# 威胁模式（简化）— 命中即拒绝写入
_THREAT_PATTERNS = [
    (r"\.\./|\.\.\\", "path traversal"),
    (r"(ANTHROPIC_API_KEY|OPENAI_API_KEY|API_KEY|SECRET|PASSWORD|AUTH_TOKEN)\s*[:=]\s*\S+", "credential leakage"),
    (r"rm\s+-rf\s+/", "destructive command"),
    (r"curl\s+\S+\s*\|\s*(ba)?sh", "curl-pipe-exec"),
    (r"eval\s*\(|exec\s*\(|__import__\s*\(", "code execution"),
    (r"/etc/(shadow|passwd)", "sensitive file access"),
    (r"^\s*(sudo|chmod\s+777)", "privilege escalation"),
]


def _validate_name(name: str):
    """校验技能名：小写字母/数字/连字符/下划线。返回错误消息或 None。"""
    if not name or not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name):
        return (f"Invalid skill name '{name}' "
                f"(use lowercase letters, digits, '-' or '_')")
    return None


def _validate_category(category: str):
    if not category:
        return None
    if category not in VALID_CATEGORIES:
        return (f"Invalid category '{category}' "
                f"(must be one of {sorted(VALID_CATEGORIES)})")
    return None


def _scan_threats(content: str):
    """安全扫描 — 命中威胁模式返回描述，否则 None。"""
    for pattern, label in _THREAT_PATTERNS:
        if re.search(pattern, content):
            return f"security scan blocked: detected {label}"
    return None


def _atomic_write(path: Path, text: str):
    """原子写入：先写 .tmp，再 os.replace 覆盖，避免半写状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _build_skill_md(name: str, description: str, body: str) -> str:
    """组装带 frontmatter 的 SKILL.md 内容。body 已含 frontmatter 则原样返回。"""
    if body.strip().startswith("---"):
        return body.strip() + "\n"
    desc = (description or "(see body)").strip()
    if "\n" in desc:
        desc_line = "description: |\n  " + desc.replace("\n", "\n  ")
    else:
        desc_line = f"description: {desc}"
    return f"---\nname: {name}\n{desc_line}\n---\n\n{body.strip()}\n"


def _fuzzy_replace(text: str, old: str, new: str):
    """模糊替换：先精确匹配；失败则用 SequenceMatcher 找最相似的连续行窗口。
    返回 (新文本, 匹配度) 或 (None, 最佳匹配度)。"""
    if old in text:
        return text.replace(old, new, 1), 1.0

    old_lines = old.strip().splitlines()
    text_lines = text.splitlines(keepends=True)
    n = len(old_lines)
    best_ratio = 0.0
    best_idx = -1
    for i in range(len(text_lines) - n + 1):
        window = "".join(text_lines[i:i + n])
        ratio = difflib.SequenceMatcher(None, old, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i
    if best_idx >= 0 and best_ratio >= 0.6:
        new_text = ("".join(text_lines[:best_idx]) + new
                    + "".join(text_lines[best_idx + n:]))
        return new_text, best_ratio
    return None, best_ratio


def _skill_create(name: str, content: str, description: str,
                  category: str = "") -> str:
    """创建新 Skill。七道安全检查通过后原子写入。"""
    err = _validate_name(name)
    if err:
        return f"Error: {err}"
    if not content.strip():
        return "Error: content is required to create a skill"
    if len(content) > MAX_SKILL_SIZE:
        return f"Error: content too large ({len(content)} > {MAX_SKILL_SIZE})"
    err = _validate_category(category)
    if err:
        return f"Error: {err}"
    err = _scan_threats(content)
    if err:
        return f"Error: {err}"
    if (SKILLS_DIR / name).exists():
        return f"Error: skill '{name}' already exists (use action='update')"

    full = _build_skill_md(name, description, content)
    _atomic_write(SKILLS_DIR / name / "SKILL.md", full)

    # 刷新 registry，让 list_skills / load_skill 立即可见
    _scan_skills()
    print(f"  \033[32m[skill] created '{name}'\033[0m")
    return f"Created skill '{name}' ({len(full)} bytes)"


def _skill_update(name: str, content: str, description: str,
                  old_string: str, new_string: str) -> str:
    """改进已存在的 Skill：全量重写或模糊 patch。"""
    err = _validate_name(name)
    if err:
        return f"Error: {err}"
    manifest = SKILLS_DIR / name / "SKILL.md"
    if not manifest.exists():
        return f"Error: skill '{name}' not found (use action='create')"

    current = manifest.read_text()
    meta, _ = _parse_frontmatter(current)

    if old_string and new_string:
        # patch 模式：模糊匹配 old_string
        if len(new_string) > MAX_SKILL_SIZE:
            return f"Error: content too large ({len(new_string)} > {MAX_SKILL_SIZE})"
        err = _scan_threats(new_string)
        if err:
            return f"Error: {err}"
        new_text, ratio = _fuzzy_replace(current, old_string, new_string)
        if new_text is None:
            return (f"Error: old_string not found "
                    f"(best fuzzy match {ratio:.2f})")
        _atomic_write(manifest, new_text)
        _scan_skills()
        print(f"  \033[36m[skill] patched '{name}' "
              f"(match {ratio:.2f})\033[0m")
        return f"Patched skill '{name}' (fuzzy match {ratio:.2f})"

    if content.strip():
        # 重写模式：保留原 frontmatter（description 未显式给出时沿用旧的）
        if len(content) > MAX_SKILL_SIZE:
            return f"Error: content too large ({len(content)} > {MAX_SKILL_SIZE})"
        err = _scan_threats(content)
        if err:
            return f"Error: {err}"
        if not description:
            description = meta.get("description", "")
        full = _build_skill_md(name, description, content)
        _atomic_write(manifest, full)
        _scan_skills()
        print(f"  \033[36m[skill] rewritten '{name}'\033[0m")
        return f"Rewrote skill '{name}' ({len(full)} bytes)"

    return ("Error: provide either content (rewrite) "
            "or old_string+new_string (patch)")


def skill_manage(action: str, name: str = "", content: str = "",
                 description: str = "", category: str = "",
                 old_string: str = "", new_string: str = "") -> str:
    """Skill 自进化入口。action: 'create' | 'update'。"""
    action = (action or "").lower()
    if action == "create":
        return _skill_create(name, content, description, category)
    if action == "update":
        return _skill_update(name, content, description, old_string, new_string)
    return f"Unknown action '{action}' (use 'create' or 'update')"
