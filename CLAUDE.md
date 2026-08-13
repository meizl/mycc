# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
mycc (My Coding Companion) is a minimal AI coding agent built from scratch as a learning project. It mirrors Claude Code's architecture chapter-by-chapter (s01→s19 of the `learn_claude_code` reference): streaming agent loop, multi-tool use, context compaction, persistent memory, subagents, background tasks, cron, agent teams, autonomous workers, and MCP. Uses the Anthropic API with streaming.

## Setup & Running
```bash
pip install -r requirements.txt
# Configure .env with MODEL_ID, ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY
cd mycc && python3 -m app.agent_loop
```
There are no tests. Verification is manual: `python3 -m app.agent_loop`, or `python3 -c "import <module>"` for import/syntax checks.

## Architecture

All modules live flat in `mycc/app/`. The entry point `agent_loop.py` is the orchestrator; the other files are capability modules it imports.

### Core Loop (`agent_loop.py`)
Streaming agent loop: reads user input → calls LLM with tools → executes tool calls → repeats. Key behaviors:
- **Event-driven entry**: a `queue.Queue` unifies two sources — user input (`input_reader` thread) and async events (`inbox_poller` thread, wakes on teammate messages / completed background tasks). Events are `("user", text)` or `("wake", None)`.
- **CLAUDE.md auto-loading**: Walks up from WORKDIR to find CLAUDE.md, injects into SYSTEM prompt (truncated at 8000 chars)
- **Nag mechanism**: If 3+ rounds pass without todo_write, injects a reminder
- **Dynamic tool pool**: `tools, handlers = assemble_tool_pool()` (builtin + MCP tools). Rebuilt after `connect_mcp` because the pool changes at runtime.
- **Handler injection**: `TOOL_HANDLERS["task"]` and `TOOL_HANDLERS["spawn_teammate"]` are injected here (not in tools.py) because they need `client` and `MODEL`.

### Tools (`tools.py`)
All tool definitions (Anthropic-compatible JSON schemas) and implementations live in one file, dispatched via a single `TOOL_HANDLERS` dict. Beyond basic filesystem/execution/network tools, it holds three subsystems:
- **Task system (disk-persistent)**: `task_create`/`task_list`/`task_update`/`get_task` write to `.tasks/task_*.json`. Tasks have `status` (`pending`/`in_progress`/`done`/`cancelled`), `owner`, and `blockedBy` dependencies. `can_start()` gates on dependencies; `claim_task`/`complete_task`/`scan_unclaimed_tasks` support autonomous workers. Distinct from `todo_write` (in-memory, session-level only).
- **Background tasks**: bash accepts `run_in_background=true`. `start_background_task()` runs in a daemon thread and returns a placeholder immediately; `collect_background_results()` injects `<task_notification>` blocks when done. `should_run_background()` uses explicit flag first, keyword heuristic (`install`/`build`/`test`...) as fallback.
- **Protocol tools**: `request_shutdown`/`request_plan`/`review_plan` and `consume_lead_inbox(route_protocol=True)` route `*_response` messages through `match_response`.

Tool output >50KB is persisted to `.task_outputs/` to save context. bash commands have a deny list (`rm -rf /`, `sudo`, etc.) and user-confirmation for destructive operations.

### Context Compaction (`compact.py`)
4-layer pipeline, run every turn before the LLM call:
- **L1 snip**: If messages >50, crop the middle while preserving tool_use/tool_result pairs
- **L2 micro**: Replace old reobtainable tool results (>1h cooldown) with short placeholders — non-idempotent tools (write_file, ask_user) are never compressed
- **L3 budget**: Persist large tool results (>10KB each, total >200KB) to disk files
- **L4 projection**: Read-time view — light fold (>90% context) replaces old tool results with placeholders; heavy fold (>95%) sends history to an LLM summarizer

**AutoCompact**: When L4-projected tokens still exceed 160K, spawns a tool-less subagent to produce a structured XML summary, rewriting messages in-place. Max 3 runs per session (circuit breaker).

### Subagent (`subagent.py`)
Isolated agent with fresh `messages=[]`, limited tools (`SUB_TOOLS`/`SUB_HANDLERS`: no `task` — prevents recursion, no `todo_write`), max 30 turns. Returns a structured summary extracted by an extra LLM call on the last 20 turns. Exports `SUB_TOOLS`/`SUB_HANDLERS` for reuse by teammates.

### Skill System (`skill_loader.py`)
Two-layer knowledge injection plus self-evolution:
- **Layer 1 (cheap)**: Skill names + one-line descriptions injected into SYSTEM prompt at startup
- **Layer 2 (on-demand)**: Agent calls `load_skill("name")` → full SKILL.md returned
- **Self-evolution**: `skill_manage(action="create"|"update")` lets the agent autonomously create or fix skills. Create/update run seven checks (name validation, size ≤100KB, category, frontmatter format, name conflict, atomic write, threat scan); update supports fuzzy patch via `difflib.SequenceMatcher` for when the LLM recalls skill content imperfectly.

Skills live in `mycc/app/skills/<name>/SKILL.md` with YAML-like frontmatter (name + description).

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

### Error Recovery (`error_recovery.py`)
Constants + helpers for LLM-call failure paths, consumed by agent_loop:
- **max_tokens**: escalate 8K→64K, then up to 3 continuation prompts
- **429/529**: exponential backoff with jitter (max 10 retries); 529 x3 switches to `FALLBACK_MODEL_ID`
- `is_*_error()` classifiers match on exception name/message substrings.

### Cron Scheduler (`cron.py`)
4-layer pipeline, all daemon threads: `cron_scheduler_loop` (1s poll, matches 5-field cron) → `cron_queue` → `cron_queue_processor` (0.2s poll) → `spawn_subagent` (clean context, no main-session pollution). Durable jobs persist to `.scheduled_tasks.json`; `init_cron(client, MODEL)` starts the threads from agent_loop. Results are stored and drained into the main conversation via `drain_cron_notifications()`.

### Agent Teams (`message_bus.py`, `protocol.py`, `teammate.py`)
- **`message_bus.py`**: file-based mailboxes (`.mailboxes/<agent>.jsonl`). `read_inbox` is destructive (read + unlink); `peek` is non-destructive. `send()` carries `metadata` for protocol correlation.
- **`protocol.py`**: `ProtocolState` dataclass + `pending_requests` keyed by `request_id`. `match_response()` validates response type matches the original request and updates status. Used for shutdown_request/response and plan_approval_request/response.
- **`teammate.py`**: autonomous worker with WORK/IDLE lifecycle. Spawned via `spawn_teammate_async` in a daemon thread. WORK phase (≤10 rounds) runs inbox→LLM→tools; IDLE phase polls every 5s for inbox messages or unclaimed tasks (auto-claims them), timing out after 60s. Teammates reuse `SUB_TOOLS`/`SUB_HANDLERS` plus team tools (send_message, submit_plan, list_tasks, claim_task, complete_task). Re-injects an `<identity>` message when context is compressed (`len(messages) <= 3`).

### MCP (`mcp.py`)
Standard protocol for external tools. `MCPClient` simulates `tools/list` + `tools/call` (mock handlers stand in for real stdio JSON-RPC). `connect_mcp(name)` connects a mock server (docs/deploy); `assemble_tool_pool()` merges builtin + MCP tools, prefixing MCP tools as `mcp__{server}__{tool}` with `normalize_mcp_name` (non `[a-zA-Z0-9_-]` → `_`). MCP tools are only available to the Lead agent; teammates use their fixed subset.

## Key Design Decisions
1. **Streaming-first**: LLM responses stream token-by-token to the terminal; tool input accumulates via `input_json_delta`
2. **Context compaction is layered**: L1-L3 modify messages in-place (cheap rules), L4 returns a read-time projection (never mutates), AutoCompact rewrites in-place (expensive, last resort)
3. **Memory is async**: Extraction and consolidation run in a background daemon thread to avoid blocking the user
4. **Subagents are isolated**: Fresh context, no recursion, limited tool set — designed for focused subtasks
5. **Skills are lazy-loaded + self-evolving**: Names are cheap (~50-100 tokens) and always visible; full content loads on demand; the agent creates/patches skills autonomously
6. **Dynamic tool pool**: MCP tools appear/disappear at runtime, so `agent_loop` rebuilds `tools`/`handlers` rather than caching them (prompt cache would go stale after `connect_mcp`)
7. **Agent communication is file-based + protocol-correlated**: mailboxes for transport, `request_id` for matching responses to requests
8. **Autonomous workers idle rather than exit**: teammates poll for work (auto-claim unclaimed tasks) and only shut down on request or idle timeout

## Conventions
- All paths are relative to `WORKDIR` (`Path.cwd()`)
- Tools use snake_case naming; Python 3.9+ compatible — use `owner: object` / `Optional[str]`, NOT `str | None` (note `error_recovery.py` and `subagent.py` use `str | None` already; new code avoids it per project convention)
- Block content format is dual-compatible: both `dict` and SDK object access patterns are supported throughout (`block["name"]` vs `block.name`)
- **Circular imports are broken with lazy imports**: `mcp.py` imports `tools` inside `assemble_tool_pool()`; `cron.py` imports `subagent` inside `_run_cron_subagent()`. When a module needs another that transitively imports it, import inside the function, not at module top.
- Print statements use ANSI color codes: cyan for input prompts, yellow for tool names, blue for compaction, gray for hooks/meta, magenta/purple for cron/subagents/teammates, red for errors/protocol-reject
- `.tasks/`, `.mailboxes/`, `.memory/`, `.task_outputs/`, `.scheduled_tasks.json` are runtime state, gitignored
