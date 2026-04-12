.PHONY: sync run sandbox task eval check fix

sync:
	uv sync

# Full PAC1 benchmark (requires BITGN_API_KEY)
run:
	uv run python main.py

# Sandbox mode (no API key needed)
sandbox:
	BENCH_ID=bitgn/sandbox uv run python main.py

# Run specific tasks: `make task TASKS='t01 t03'`
task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	uv run python main.py $(TASKS)

# Full PAC1 eval, output logged
eval:
	uv run python main.py 2>&1 | tee run_$$(date +%Y%m%d_%H%M).log

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fix:
	uv run ruff check --fix .
	uv run ruff format .
