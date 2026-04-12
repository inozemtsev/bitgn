"""Entry point for the BitGN Codex agent.

Supports both sandbox (no API key) and PAC1 (leaderboard) benchmarks.

Usage:
    uv run python main.py              # run all tasks
    uv run python main.py t01 t03      # run specific tasks

Environment variables:
    OPENAI_API_KEY  - Required (Codex CLI calls OpenAI under the hood)
    MODEL_ID        - Codex model (default: gpt-5.3-codex)
    BITGN_HOST      - API host (default: https://api.bitgn.com)
    BITGN_API_KEY   - Required for PAC1 leaderboard submission
    BENCH_ID        - "bitgn/sandbox" or "bitgn/pac1-dev" (default)
    RUN_NAME        - Experiment name (shown in BitGN + Logfire)
    WORKERS         - Parallel task workers (default: 5)
"""

from __future__ import annotations

import os
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from dotenv import load_dotenv

load_dotenv()  # load .env before any SDK reads API keys

import logfire
from connectrpc.errors import ConnectError

from codex_agent import run_codex_agent
from config import DEFAULT_MODEL
from vault_utils import C_BLUE, C_CLR, C_CYAN, C_GREEN, C_RED, tprint

# ── Observability (Logfire) ──────────────────────────────────────────────

RUN_NAME = os.getenv("RUN_NAME") or "Adaptive Multi-Phase Agent"
logfire.configure(environment=RUN_NAME, scrubbing=False)

# ── Configuration ─────────────────────────────────────────────────────────

BITGN_HOST = os.getenv("BITGN_HOST") or "https://api.bitgn.com"
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or "bitgn/pac1-dev"
WORKERS = int(os.getenv("WORKERS", "5"))
MODEL_ID = os.getenv("MODEL_ID") or DEFAULT_MODEL


def _is_sandbox(bench_id: str) -> bool:
    return "sandbox" in bench_id


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


def _record_score(
    scores: list[tuple[str, float]],
    scores_lock: threading.Lock,
    task_id: str,
    result: Any,
) -> None:
    if result.score < 0:
        return
    with scores_lock:
        scores.append((task_id, result.score))
    style = C_GREEN if result.score == 1 else C_RED
    explain = textwrap.indent("\n".join(result.score_detail), "  ")
    tprint(task_id, f"\n{style}Score: {result.score:0.2f}\n{explain}\n{C_CLR}")


def run_sandbox(task_filter: list[str]) -> None:
    """Run against sandbox benchmark (no API key, no leaderboard)."""
    from bitgn.harness_connect import HarnessServiceClientSync
    from bitgn.harness_pb2 import (
        EndTrialRequest,
        EvalPolicy,
        GetBenchmarkRequest,
        StartPlaygroundRequest,
        StatusRequest,
    )

    scores: list[tuple[str, float]] = []
    scores_lock = threading.Lock()

    try:
        with logfire.span(
            "benchmark run {run_name}",
            run_name=RUN_NAME,
            bench_id=BENCH_ID,
            model=MODEL_ID,
        ):
            client = HarnessServiceClientSync(BITGN_HOST)
            print(f"{C_CYAN}Connecting to BitGN...{C_CLR}", client.status(StatusRequest()))

            res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCH_ID))
            print(
                f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
                f"with {len(res.tasks)} tasks.\n{C_GREEN}{res.description}{C_CLR}"
            )
            print(f"Model:   {C_CYAN}{MODEL_ID}{C_CLR}")
            print(f"Run:     {C_CYAN}{RUN_NAME}{C_CLR}")
            print(f"Workers: {C_CYAN}{WORKERS}{C_CLR}\n")

            tasks_to_run = [t for t in res.tasks if not task_filter or t.task_id in task_filter]

            def _run_one(task: Any) -> None:
                tid = task.task_id
                tprint(tid, f"{'=' * 30} Starting task: {tid} {'=' * 30}")
                trial = client.start_playground(
                    StartPlaygroundRequest(benchmark_id=BENCH_ID, task_id=tid)
                )
                tprint(tid, f"{C_BLUE}{trial.instruction}{C_CLR}\n{'-' * 80}")
                try:
                    run_codex_agent(
                        MODEL_ID, trial.harness_url, trial.instruction, runtime="mini", task_id=tid
                    )
                except Exception as exc:
                    tprint(tid, f"{C_RED}Agent error: {exc}{C_CLR}")
                result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
                _record_score(scores, scores_lock, tid, result)

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(_run_one, t): t for t in tasks_to_run}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        task = futures[future]
                        tprint(task.task_id, f"{C_RED}Task failed: {exc}{C_CLR}")

    except ConnectError as exc:
        print(f"{C_RED}{exc.code}: {exc.message}{C_CLR}")
    except KeyboardInterrupt:
        print(f"{C_RED}Interrupted{C_CLR}")

    _print_scores(scores)


def run_pac1(task_filter: list[str]) -> None:
    """Run against PAC1 benchmark (requires API key, submits to leaderboard)."""
    from bitgn.harness_connect import HarnessServiceClientSync
    from bitgn.harness_pb2 import (
        EndTrialRequest,
        EvalPolicy,
        GetBenchmarkRequest,
        StartRunRequest,
        StartTrialRequest,
        StatusRequest,
        SubmitRunRequest,
    )

    if not BITGN_API_KEY:
        print(f"{C_RED}Error: BITGN_API_KEY is required for PAC1 benchmark.{C_CLR}")
        print("Set it with: export BITGN_API_KEY=your_key")
        print("Or use sandbox: BENCH_ID=bitgn/sandbox make run")
        sys.exit(1)

    scores: list[tuple[str, float]] = []
    scores_lock = threading.Lock()

    try:
        with logfire.span(
            "benchmark run {run_name}",
            run_name=RUN_NAME,
            bench_id=BENCH_ID,
            model=MODEL_ID,
        ):
            client = HarnessServiceClientSync(BITGN_HOST)
            print(f"{C_CYAN}Connecting to BitGN...{C_CLR}", client.status(StatusRequest()))

            res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCH_ID))
            print(
                f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
                f"with {len(res.tasks)} tasks.\n{C_GREEN}{res.description}{C_CLR}"
            )
            print(f"Model:   {C_CYAN}{MODEL_ID}{C_CLR}")
            print(f"Run:     {C_CYAN}{RUN_NAME}{C_CLR}")
            print(f"Workers: {C_CYAN}{WORKERS}{C_CLR}\n")

            run = client.start_run(
                StartRunRequest(name=RUN_NAME, benchmark_id=BENCH_ID, api_key=BITGN_API_KEY)
            )

            def _run_one_trial(trial_id: str) -> None:
                trial = client.start_trial(StartTrialRequest(trial_id=trial_id))
                tid = trial.task_id
                if task_filter and tid not in task_filter:
                    client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
                    return

                tprint(tid, f"{'=' * 30} Starting task: {tid} {'=' * 30}")
                tprint(tid, f"{C_BLUE}{trial.instruction}{C_CLR}\n{'-' * 80}")
                try:
                    run_codex_agent(
                        MODEL_ID, trial.harness_url, trial.instruction, runtime="pcm", task_id=tid
                    )
                except Exception as exc:
                    tprint(tid, f"{C_RED}Agent error: {exc}{C_CLR}")

                result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
                _record_score(scores, scores_lock, tid, result)

            try:
                with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                    futures = [pool.submit(_run_one_trial, tid) for tid in run.trial_ids]
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            tprint("???", f"{C_RED}Trial failed: {exc}{C_CLR}")
            finally:
                client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))

    except ConnectError as exc:
        print(f"{C_RED}{exc.code}: {exc.message}{C_CLR}")
    except KeyboardInterrupt:
        print(f"{C_RED}Interrupted{C_CLR}")

    _print_scores(scores)


def main() -> None:
    task_filter = sys.argv[1:]

    print(f"\n{C_CYAN}╔══════════════════════════════════════════╗{C_CLR}")
    print(f"{C_CYAN}║  BitGN Codex Agent                       ║{C_CLR}")
    print(f"{C_CYAN}╚══════════════════════════════════════════╝{C_CLR}\n")

    if _is_sandbox(BENCH_ID):
        run_sandbox(task_filter)
    else:
        run_pac1(task_filter)


if __name__ == "__main__":
    main()
