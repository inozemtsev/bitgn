# BitGN Agent

An autonomous agent for the [BitGN](https://bitgn.com) benchmarking platform — a global competition where agents solve tasks inside vault-style workspaces.

Built for the **"Personal & Trustworthy" (April 2026)** challenge using **Codex CLI 0.107.0** and **gpt-5.4** (reasoning effort: high). Scored **87/104 tasks**.

## Architecture

```
main.py ──► codex_agent.py ──► codex exec ──► vault_mcp_server.py (stdio MCP) ──► BitGN VM (gRPC)
```

- [main.py](main.py) — Entry point. Connects to BitGN, iterates tasks, dispatches each to the agent.
- [codex_agent.py](codex_agent.py) — Runs a single `codex exec --full-auto` per task with a custom MCP server attached. Handles auto-discovery, prompt assembly, subprocess execution, and answer submission.
- [vault_mcp_server.py](vault_mcp_server.py) — Stdio MCP server exposing vault tools (`vault_read`, `vault_write`, `vault_delete`, `vault_search`, `vault_tree`, …). Runs outside the Codex sandbox and makes gRPC calls to the BitGN VM.
- [vault_utils.py](vault_utils.py) — Shared helpers: file metadata inference, tree formatting, ANSI colors, thread-safe printing.
- [config.py](config.py) — Module-level constants (timeouts, retry counts, default model).
- [prompts/](prompts/) — Agent instructions, the optional multi-step protocol, and the Codex preamble, each as a separate Markdown file.

## Features

1. **Custom MCP server** — bridges Codex CLI to BitGN vault tools via gRPC. Codex gets clean tool interfaces; the server handles protocol translation.
2. **Vault metadata tags** — wraps file content in `<vault-file>` XML with `type`, `trust`, and `format` attributes. Root policy files are trusted; everything else is untrusted.
3. **Multi-step protocol** — optional (`CODEX_MULTI_STEP=on`) Plan → Draft → Verify → Commit phases. Files are drafted to Codex's local sandbox first, verified, then committed to the vault.
4. **Custom compaction prompt** — [compact_prompt.md](compact_prompt.md) preserves vault state, trust boundaries, security observations, and plan progress across context window compaction.
5. **Security-aware instructions** — explicit threat detection for prompt injection, credential exfiltration, system file tampering, and social engineering. Suspicious tasks get `OUTCOME_DENIED_SECURITY` instead of blind execution.
6. **Auto-discovery** — before the Codex loop starts, the agent reads the vault tree, `AGENTS.md`, and task context so Codex starts with a full picture.
7. **Deterministic grounding refs** — the MCP server tracks every `vault_read` / `vault_search` call and writes the set to a refs file. The agent reads the file after Codex returns, so refs reflect what was actually read (not what the model claimed).
8. **Reasoning effort tuning** — via `CODEX_REASONING_EFFORT` (`low` / `medium` / `high` / `xhigh`).
9. **Structured output** — `TaskResult` with outcome codes: `OK`, `NONE_CLARIFICATION`, `DENIED_SECURITY`, `NONE_UNSUPPORTED`, `ERR_INTERNAL`.
10. **Observability** — [Logfire](https://logfire.pydantic.dev) integration for tracing agent runs, tool calls, and token usage.

No rigorous ablation study was conducted — I just kept adding things that seemed like good ideas.

## Setup

```bash
make sync               # install dependencies
cp .env.example .env    # fill in API keys
```

### Codex MCP config

Codex CLI reads `~/.codex/config.toml` on startup and spawns the `bitgn-vault` MCP server from the path registered there. The `[mcp_servers.bitgn-vault]` block must point at **this** checkout's `vault_mcp_server.py` — otherwise changes made in this repo (validator, tools, logging) are ignored silently. A sample config is included for reference at [`codex_config.example.toml`](codex_config.example.toml); add an equivalent block to `~/.codex/config.toml` with absolute paths for your machine. `VAULT_HARNESS_URL` in the config is a placeholder — `codex_agent.py` overrides it per task.

This setup will be dockerized in a future iteration so the MCP server path, Python env, and `yq` dependency are all pinned inside the container and the host `~/.codex/config.toml` edit is no longer required.

### Environment Variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | Required (Codex CLI uses OpenAI under the hood) |
| `BITGN_API_KEY` | Required for PAC1 leaderboard submission |
| `BENCH_ID` | `bitgn/sandbox` or `bitgn/pac1-dev` (default: `bitgn/pac1-dev`) |
| `MODEL_ID` | Codex model (default: `gpt-5.3-codex`) |
| `WORKERS` | Parallel task workers (default: `5`) |
| `RUN_NAME` | Experiment name shown in BitGN + Logfire |
| `CODEX_MULTI_STEP` | `on` to enable Plan → Draft → Verify → Commit |
| `CODEX_REASONING_EFFORT` | `low` / `medium` / `high` / `xhigh` (default: `high`) |
| `LOGFIRE_TOKEN` | Logfire write token for observability |

## Usage

```bash
make sandbox                       # sandbox mode (no API key)
make run                           # full PAC1 benchmark
make task TASKS='t01 t03 t05'      # specific tasks
make eval                          # full PAC1 eval, output tee'd to a log file
```

### Competition Commands

The actual commands used during the competition (see [run.sh](run.sh)):

```bash
WORKERS=6 MODEL_ID=gpt-5.4       RUN_NAME=codex-on-rails uv run python main.py
WORKERS=6 MODEL_ID=gpt-5.3-codex RUN_NAME=codex-on-rails uv run python main.py
```

## Development

```bash
make check   # ruff + mypy
make fix     # auto-fix and format
```

Synthetic tasks can be run against the sandbox for quick iteration:

```bash
uv run python -m tests.run_synthetic              # run all
uv run python -m tests.run_synthetic inject-01    # run one
uv run python -m tests.run_synthetic --list       # list all
```
