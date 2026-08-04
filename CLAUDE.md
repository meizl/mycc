# CLAUDE.md — mycc Project

## Overview
mycc (My Coding Companion) is a minimal AI coding agent built from scratch as a learning project. It uses the Anthropic API with streaming, multi-tool support, and context compaction.

## Architecture
- `mycc/app/agent_loop.py` — Main agent loop with streaming LLM calls
- `mycc/app/tools.py` — Tool definitions and implementations (file ops, bash, web, task management)
- `mycc/app/compact.py` — 4-layer context compaction pipeline (L1 snip, L2 micro, L3 budget, L4 projection + AutoCompact)
- `mycc/app/hooks.py` — Hook system for extensible permission checks
- `mycc/app/subagent.py` — Isolated subagent with fresh context (no recursion)
- `mycc/app/skill_loader.py` — Skill system: frontmatter parsing, registry, layered loading

## Key Design Decisions
1. **Context compaction is layered**: L1-L3 modify in-place, L4 projects a read-time view, AutoCompact uses a subagent for hard compaction
2. **Streaming-first**: LLM responses stream token-by-token to the terminal
3. **Tool result budget**: Large tool outputs (>50KB) are persisted to disk files to save context
4. **Nag mechanism**: If 3+ rounds pass without todo_write, a reminder is injected

## Conventions
- All paths are relative to `WORKDIR` (current working directory)
- Tools use snake_case naming
- Python 3.9+ compatible (no `str | None` syntax)
- Print statements use ANSI color codes for terminal output

## Running
```bash
cd mycc && python3 -m app.agent_loop
```
