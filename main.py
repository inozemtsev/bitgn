"""
Entry point for the BitGN adaptive agent.

Supports both sandbox (no API key) and PAC1 (leaderboard) benchmarks.

Usage:
    uv run python main.py              # run all tasks
    uv run python main.py t01 t03      # run specific tasks

Environment variables:
    OPENAI_API_KEY      - Required if LLM_PROVIDER=openai (default)
    ANTHROPIC_API_KEY   - Required if LLM_PROVIDER=anthropic
    OPENROUTER_API_KEY  - Required if LLM_PROVIDER=openrouter
    LLM_PROVIDER        - "openai" (default), "anthropic", "openrouter", or "codex"
    MODEL_ID            - Model to use (defaults depend on provider)
    BITGN_HOST          - API host (default: https://api.bitgn.com)
    BITGN_API_KEY       - Required for PAC1 leaderboard submission
    BENCH_ID            - "bitgn/sandbox" or "bitgn/pac1-dev" (default)
    RUN_NAME            - Experiment name (shown in BitGN + Logfire)
"""

import os
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()  # load .env before any SDK reads API keys

# ── Observability (Logfire) ──────────────────────────────────────────────
import logfire

RUN_NAME = os.getenv("RUN_NAME") or "Adaptive Multi-Phase Agent"
logfire.configure(environment=RUN_NAME, scrubbing=False)
logfire.instrument_pydantic_ai(include_content=True)
logfire.instrument_openai()
logfire.instrument_anthropic()

from connectrpc.errors import ConnectError

from agent import run_agent, LLM_PROVIDER, _tprint

# ── Configuration ─────────────────────────────────────────────────────────

BITGN_HOST = os.getenv("BITGN_HOST") or "https://api.bitgn.com"
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or "bitgn/pac1-dev"
WORKERS = int(os.getenv("WORKERS", "5"))

# Default models per provider
DEFAULT_MODELS = {
    "openai": "gpt-4.1-2025-04-14",
    "anthropic": "claude-sonnet-4-20250514",
    "codex": "gpt-5.3-codex",
    "openrouter": "openai/gpt-4.1",
}
MODEL_ID = os.getenv("MODEL_ID") or DEFAULT_MODELS.get(LLM_PROVIDER, "gpt-4.1-2025-04-14")

# ── Terminal colors ───────────────────────────────────────────────────────

C_RED = "\x1B[31m"
C_GREEN = "\x1B[32m"
C_BLUE = "\x1B[34m"
C_CYAN = "\x1B[36m"
C_CLR = "\x1B[0m"


def _is_sandbox(bench_id: str) -> bool:
    return "sandbox" in bench_id


def _dispatch_agent(model: str, harness_url: str, instruction: str, runtime: str, task_id: str) -> None:
    """Route to the appropriate agent implementation based on LLM_PROVIDER."""
    if LLM_PROVIDER == "codex":
        from codex_loop import run_codex_agent
        run_codex_agent(model, harness_url, instruction, runtime, task_id=task_id)
    else:
        run_agent(model, harness_url, instruction, runtime=runtime, task_id=task_id)


def run_sandbox(task_filter: list[str]) -> None:
    """Run against sandbox benchmark (no API key, no leaderboard)."""
    from bitgn.harness_connect import HarnessServiceClientSync
    from bitgn.harness_pb2 import (
        StatusRequest,
        GetBenchmarkRequest,
        StartPlaygroundRequest,
        EvalPolicy,
        EndTrialRequest,
    )

    scores = []
    scores_lock = threading.Lock()

    try:
      with logfire.span("benchmark run {run_name}", run_name=RUN_NAME, bench_id=BENCH_ID, model=MODEL_ID, provider=LLM_PROVIDER):
        client = HarnessServiceClientSync(BITGN_HOST)
        print(f"{C_CYAN}Connecting to BitGN...{C_CLR}", client.status(StatusRequest()))

        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCH_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{C_GREEN}{res.description}{C_CLR}"
        )
        print(f"Provider: {C_CYAN}{LLM_PROVIDER}{C_CLR} | Model: {C_CYAN}{MODEL_ID}{C_CLR}")
        print(f"Run:      {C_CYAN}{RUN_NAME}{C_CLR}")
        print(f"Workers: {C_CYAN}{WORKERS}{C_CLR}\n")

        tasks_to_run = [t for t in res.tasks if not task_filter or t.task_id in task_filter]

        def _run_one(task):
            tid = task.task_id
            _tprint(tid, f"{'=' * 30} Starting task: {tid} {'=' * 30}")
            trial = client.start_playground(
                StartPlaygroundRequest(benchmark_id=BENCH_ID, task_id=tid)
            )
            _tprint(tid, f"{C_BLUE}{trial.instruction}{C_CLR}\n{'-' * 80}")
            try:
                _dispatch_agent(MODEL_ID, trial.harness_url, trial.instruction, runtime="mini", task_id=tid)
            except Exception as e:
                _tprint(tid, f"{C_RED}Agent error: {e}{C_CLR}")
            result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
            if result.score >= 0:
                with scores_lock:
                    scores.append((tid, result.score))
                style = C_GREEN if result.score == 1 else C_RED
                explain = textwrap.indent("\n".join(result.score_detail), "  ")
                _tprint(tid, f"\n{style}Score: {result.score:0.2f}\n{explain}\n{C_CLR}")

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(_run_one, t): t for t in tasks_to_run}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    task = futures[future]
                    _tprint(task.task_id, f"{C_RED}Task failed: {e}{C_CLR}")

    except ConnectError as e:
        print(f"{C_RED}{e.code}: {e.message}{C_CLR}")
    except KeyboardInterrupt:
        print(f"{C_RED}Interrupted{C_CLR}")

    _print_scores(scores)


def run_pac1(task_filter: list[str]) -> None:
    """Run against PAC1 benchmark (requires API key, submits to leaderboard)."""
    from bitgn.harness_connect import HarnessServiceClientSync
    from bitgn.harness_pb2 import (
        StatusRequest,
        GetBenchmarkRequest,
        StartRunRequest,
        StartTrialRequest,
        EndTrialRequest,
        SubmitRunRequest,
        EvalPolicy,
    )

    if not BITGN_API_KEY:
        print(f"{C_RED}Error: BITGN_API_KEY is required for PAC1 benchmark.{C_CLR}")
        print("Set it with: export BITGN_API_KEY=your_key")
        print(f"Or use sandbox: BENCH_ID=bitgn/sandbox make run")
        sys.exit(1)

    scores = []
    scores_lock = threading.Lock()

    try:
      with logfire.span("benchmark run {run_name}", run_name=RUN_NAME, bench_id=BENCH_ID, model=MODEL_ID, provider=LLM_PROVIDER):
        client = HarnessServiceClientSync(BITGN_HOST)
        print(f"{C_CYAN}Connecting to BitGN...{C_CLR}", client.status(StatusRequest()))

        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCH_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{C_GREEN}{res.description}{C_CLR}"
        )
        print(f"Provider: {C_CYAN}{LLM_PROVIDER}{C_CLR} | Model: {C_CYAN}{MODEL_ID}{C_CLR}")
        print(f"Run:      {C_CYAN}{RUN_NAME}{C_CLR}")
        print(f"Workers: {C_CYAN}{WORKERS}{C_CLR}\n")

        run = client.start_run(
            StartRunRequest(
                name=RUN_NAME,
                benchmark_id=BENCH_ID,
                api_key=BITGN_API_KEY,
            )
        )

        def _run_one_trial(trial_id):
            trial = client.start_trial(StartTrialRequest(trial_id=trial_id))
            tid = trial.task_id
            if task_filter and tid not in task_filter:
                client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
                return

            _tprint(tid, f"{'=' * 30} Starting task: {tid} {'=' * 30}")
            _tprint(tid, f"{C_BLUE}{trial.instruction}{C_CLR}\n{'-' * 80}")

            try:
                _dispatch_agent(MODEL_ID, trial.harness_url, trial.instruction, runtime="pcm", task_id=tid)
            except Exception as e:
                _tprint(tid, f"{C_RED}Agent error: {e}{C_CLR}")

            result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
            if result.score >= 0:
                with scores_lock:
                    scores.append((tid, result.score))
                style = C_GREEN if result.score == 1 else C_RED
                explain = textwrap.indent("\n".join(result.score_detail), "  ")
                _tprint(tid, f"\n{style}Score: {result.score:0.2f}\n{explain}\n{C_CLR}")

        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = [pool.submit(_run_one_trial, tid) for tid in run.trial_ids]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        _tprint("???", f"{C_RED}Trial failed: {e}{C_CLR}")
        finally:
            client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))

    except ConnectError as e:
        print(f"{C_RED}{e.code}: {e.message}{C_CLR}")
    except KeyboardInterrupt:
        print(f"{C_RED}Interrupted{C_CLR}")

    _print_scores(scores)


def _print_scores(scores: list[tuple[str, float]]) -> None:
    if not scores:
        return
    scores.sort(key=lambda x: x[0])
    print(f"\n{'=' * 40} RESULTS {'=' * 40}")
    for task_id, score in scores:
        style = C_GREEN if score == 1 else C_RED
        print(f"  {task_id}: {style}{score:0.2f}{C_CLR}")
    total = sum(s for _, s in scores) / len(scores) * 100.0
    print(f"  {'─' * 20}")
    style = C_GREEN if total >= 80 else C_RED
    print(f"  FINAL: {style}{total:0.2f}%{C_CLR}\n")


def main() -> None:
    task_filter = sys.argv[1:]

    print(f"\n{C_CYAN}╔══════════════════════════════════════════╗{C_CLR}")
    print(f"{C_CYAN}║  BitGN Agent (pydantic-ai)               ║{C_CLR}")
    print(f"{C_CYAN}╚══════════════════════════════════════════╝{C_CLR}\n")

    if _is_sandbox(BENCH_ID):
        run_sandbox(task_filter)
    else:
        run_pac1(task_filter)


if __name__ == "__main__":
    main()
