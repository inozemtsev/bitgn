.PHONY: sync run task sandbox codex codex-sandbox

sync:
	uv sync

run:
	uv run python main.py

task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	uv run python main.py $(TASKS)

# Quick sandbox mode (no API key needed)
sandbox:
	BENCH_ID=bitgn/sandbox uv run python main.py

# Run with Anthropic Claude instead of OpenAI
claude:
	LLM_PROVIDER=anthropic uv run python main.py

# Run with Codex CLI — native MCP integration (single codex exec per task)
# CODEX_SANDBOX: read-only (default, no shell — lower tokens) | workspace-write | danger-full-access
codex:
	LLM_PROVIDER=codex uv run python main.py

# Codex + sandbox (native MCP)
codex-sandbox:
	LLM_PROVIDER=codex BENCH_ID=bitgn/sandbox uv run python main.py

# Full PAC1 eval with Codex, output logged
eval:
	LLM_PROVIDER=codex uv run python main.py 2>&1 | tee run_$$(date +%Y%m%d_%H%M).log
