# BitGN Agent

An autonomous agent for the [BitGN](https://bitgn.com) benchmarking platform — a global competition where agents solve deterministic enterprise tasks inside vault-style workspaces (think Obsidian for business).

Built for the **"Personal & Trustworthy" (April 2026)** challenge. Only the Codex path was used during the actual competition, with **Codex CLI 0.107.0** and **gpt-5.4** (reasoning effort: high). Scored **87/104 tasks**.

## Architecture

```
main.py → codex_loop.py → vault_mcp_server.py (MCP stdio) → BitGN VM (gRPC)
```

- **main.py** — Entry point. Connects to BitGN API, fetches benchmark tasks, manages trial lifecycle.
- **codex_loop.py** — Runs a single `codex exec --full-auto` per task with a custom MCP server attached.
- **vault_mcp_server.py** — Stdio MCP server exposing vault tools (`vault_read`, `vault_write`, `vault_delete`, `vault_search`, `vault_tree`, etc.). Runs outside Codex sandbox; makes gRPC calls to the BitGN VM.
- **agent.py** — Alternative pydantic-ai agent path with multi-provider support (OpenAI / Anthropic / OpenRouter). Exists but wasn't battle-tested in competition.

## Features

1. **Custom MCP server** — Bridges Codex CLI to BitGN vault tools via gRPC. Codex gets clean tool interfaces; the server handles all protocol translation.

2. **Vault metadata tags** — Wraps file content in `<vault-file>` XML tags with `type`, `trust`, and `format` attributes. Root policy files are marked as trusted; everything else is untrusted. Helps the model reason about trust boundaries.

3. **Multi-step protocol** — When enabled (`CODEX_MULTI_STEP=on`), the agent follows Plan → Draft → Verify → Commit phases. Files are saved to Codex's local sandbox first and verified before committing to the vault.

4. **Custom compaction prompt** — A checkpoint summarization template that preserves vault state, trust boundaries, security observations, and plan progress across context window compaction.

5. **Security-aware instructions** — Explicit threat detection for prompt injection, credential exfiltration, system file tampering, and social engineering. Suspicious tasks get `DENIED_SECURITY` instead of blind execution.

6. **Auto-discovery phase** — Before the agent loop starts, reads the vault tree, `AGENTS.md` policy file, and task context to build a complete picture.

7. **Deterministic grounding refs** — Server-side reference tracking (not model-reported). Every `vault_read` / `vault_search` call is tracked automatically, so grounding references are based on what was actually read.

8. **Reasoning effort tuning** — Configurable via `CODEX_REASONING_EFFORT` (low / medium / high / xhigh).

9. **Structured output** — `TaskResult` with outcome codes: `OK`, `CLARIFICATION`, `DENIED_SECURITY`, `UNSUPPORTED`, `ERR_INTERNAL`.

10. **Observability** — [Logfire](https://logfire.pydantic.dev) integration for tracing and monitoring agent runs.

No rigorous ablation study was conducted — I just kept adding things that seemed like good ideas and hoped for the best.

## Project Structure

```
├── main.py                 # Entry point, benchmark orchestration
├── codex_loop.py           # Codex CLI integration via MCP
├── vault_mcp_server.py     # MCP server exposing vault tools
├── agent.py                # Pydantic-AI agent (alternative path)
├── compact_prompt.md       # Compaction checkpoint template
├── explore_task.py         # Task exploration utility
├── run.sh                  # Multi-model evaluation script
├── Makefile                # Common commands
├── pyproject.toml          # Dependencies (Python 3.14+, uv)
└── tests/                  # Synthetic task tests
```

## Setup

```bash
# Install dependencies
make sync

# Copy and fill in environment variables
cp .env.example .env
```

### Environment Variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | Required for Codex / OpenAI models |
| `ANTHROPIC_API_KEY` | Required for Claude models |
| `BITGN_API_KEY` | Required for PAC1 leaderboard |
| `LLM_PROVIDER` | `codex`, `openai`, `anthropic`, or `openrouter` |
| `BENCH_ID` | `bitgn/sandbox` or `bitgn/pac1-dev` |
| `VAULT_TAGS` | `1` to enable metadata tags (always on for Codex MCP) |
| `CODEX_MULTI_STEP` | `on` to enable Plan → Draft → Verify → Commit |
| `CODEX_REASONING_EFFORT` | `low` / `medium` / `high` / `xhigh` |
| `LOGFIRE_TOKEN` | Pydantic Logfire write token for observability |

## Usage

```bash
# Sandbox mode (no API key needed)
make sandbox

# Run with Codex
make codex

# Codex + sandbox
make codex-sandbox

# Run specific tasks
make task TASKS='t01 t03 t05'

# Full PAC1 eval with logging
make eval
```

### Competition Commands

The actual commands used during competition runs (`run.sh`):

```bash
# Run 1: gpt-5.4
WORKERS=6 MODEL_ID=gpt-5.4 RUN_NAME=codex-on-rails uv run python main.py

# Run 2: gpt-5.3-codex
WORKERS=6 MODEL_ID=gpt-5.3-codex RUN_NAME=codex-on-rails uv run python main.py
```
