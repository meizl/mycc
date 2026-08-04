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
