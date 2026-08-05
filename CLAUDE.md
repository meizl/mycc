# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
mycc (My Coding Companion) is a minimal AI coding agent built from scratch as a learning project. It uses the Anthropic API with streaming, multi-tool support, and context compaction.

## Setup & Running
```bash
pip install -r requirements.txt
# Configure .env with MODEL_ID, ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY
cd mycc && python3 -m app.agent_loop
```

## Architecture

### Core Loop (`agent_loop.py`)
Streaming agent loop: reads user input → calls LLM with tools → executes tool calls → repeats. Key behaviors:
- **CLAUDE.md auto-loading**: Walks up from WORKDIR to find CLAUDE.md, injects into SYSTEM prompt (truncated at 8000 chars)
- **Nag mechanism**: If 3+ rounds pass without todo_write, injects a reminder
- **Hook system**: PreToolUse (permission), PostToolUse (warnings), Stop (cleanup + memory extraction) hooks fire at defined lifecycle points

### Tools (`tools.py`)
All tool definitions (Anthropic-compatible JSON schemas) and implementations live in one file. Tool output >50KB is persisted to `.task_outputs/` to save context. bash commands have a deny list (`rm -rf /`, `sudo`, etc.) and user-confirmation for destructive operations.

### Context Compaction (`compact.py`)
4-layer pipeline, run every turn before the LLM call:
- **L1 snip**: If messages >50, crop the middle while preserving tool_use/tool_result pairs
- **L2 micro**: Replace old reobtainable tool results (>1h cooldown) with short placeholders — non-idempotent tools (write_file, ask_user) are never compressed
- **L3 budget**: Persist large tool results (>10KB each, total >200KB) to disk files
- **L4 projection**: Read-time view — light fold (>90% context) replaces old tool results with placeholders; heavy fold (>95%) sends history to an LLM summarizer

**AutoCompact**: When L4-projected tokens still exceed 160K, spawns a tool-less subagent to produce a structured XML summary, rewriting messages in-place. Max 3 runs per session (circuit breaker).

### Subagent (`subagent.py`)
Isolated agent with fresh `messages=[]`, limited tools (no `task` — prevents recursion, no `todo_write`), max 30 turns. Returns a structured summary extracted by an extra LLM call on the last 20 turns.

### Skill System (`skill_loader.py`)
Two-layer knowledge injection:
- **Layer 1 (cheap)**: Skill names + one-line descriptions injected into SYSTEM prompt at startup
- **Layer 2 (on-demand)**: Agent calls `load_skill("name")` → full SKILL.md returned

Skills live in `mycc/app/skills/<name>/SKILL.md` with YAML-like frontmatter.

### Memory (`memory.py`)
Persistent cross-session knowledge stored in `.memory/` as individual markdown files with a `MEMORY.md` index. Per-turn lifecycle:
1. **build_memory_system()** → user-profile memories injected into SYSTEM at startup
2. **select_relevant_memories()** → small LLM call picks relevant memories for the current turn (keyword fallback if LLM unavailable)
3. **inject_memories()** → prepends selected memories to the current user message
4. **extract_memories()** → background daemon thread extracts new facts from dialogue at turn end
5. **consolidate_memories()** → when ≥10 memories, LLM deduplicates and merges down to ≤30
6. **forget_stale_memories()** → Ebbinghaus decay scoring (half-life 7 days); scores below 0.5 are deleted

### Hook System (`hooks.py`)
Four lifecycle events with a publish/subscribe registry:
- `UserPromptSubmit` — fired after user input
- `PreToolUse` — permission gates (deny list, workspace boundary check, destructive command confirmation)
- `PostToolUse` — large output warnings
- `Stop` — session statistics + triggers background memory extraction

## Key Design Decisions
1. **Streaming-first**: LLM responses stream token-by-token to the terminal; tool input accumulates via `input_json_delta`
2. **Context compaction is layered**: L1-L3 modify messages in-place (cheap rules), L4 returns a read-time projection (never mutates), AutoCompact rewrites in-place (expensive, last resort)
3. **Memory is async**: Extraction and consolidation run in a background daemon thread to avoid blocking the user
4. **Subagents are isolated**: Fresh context, no recursion, limited tool set — designed for focused subtasks
5. **Skills are lazy-loaded**: Names are cheap (~50-100 tokens) and always visible; full content is loaded on demand via tool call

## Conventions
- All paths are relative to `WORKDIR` (`Path.cwd()`)
- Tools use snake_case naming, Python 3.9+ compatible (no `str | None` syntax)
- Block content format is dual-compatible: both `dict` and SDK object access patterns are supported throughout
- Print statements use ANSI color codes: cyan for input prompts, yellow for tool names, blue for compaction, gray for hooks/meta
