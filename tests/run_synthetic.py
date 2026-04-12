"""
Runner for synthetic hard tasks.

Uses the BitGN sandbox runtime to create vaults with custom content,
then runs the agent against them and validates outcomes.

Usage:
    uv run python -m tests.run_synthetic                 # run all tasks
    uv run python -m tests.run_synthetic inject-01       # run specific task
    uv run python -m tests.run_synthetic --category trunc # run category
    uv run python -m tests.run_synthetic --list          # list all tasks
"""

import os
import sys
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    StartPlaygroundRequest,
)
from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import (
    DeleteRequest as MiniDeleteRequest,
    ListRequest as MiniListRequest,
    OutlineRequest,
    WriteRequest as MiniWriteRequest,
)

from config import DEFAULT_MODEL
from tests.synthetic_tasks import ALL_TASKS, TASKS_BY_ID, SyntheticTask
from vault_utils import C_BLUE, C_CLR, C_CYAN, C_GREEN, C_RED

# ── Configuration ────────────────────────────────────────────────────────────

BITGN_HOST = os.getenv("BITGN_HOST") or "https://api.bitgn.com"
SANDBOX_BENCH = "bitgn/sandbox"
# We use sandbox task t01 as a template — we'll overwrite its vault contents
TEMPLATE_TASK = "t01"


def _clear_vault(vm: MiniRuntimeClientSync) -> None:
    """Delete all files from the sandbox vault."""
    outline = vm.outline(OutlineRequest(path="/"))

    # Delete files first
    for f in outline.files:
        try:
            vm.delete(MiniDeleteRequest(path=f.path))
        except Exception:
            pass

    # Then recurse into folders
    for folder in outline.folders:
        _clear_folder(vm, folder)


def _clear_folder(vm: MiniRuntimeClientSync, path: str) -> None:
    """Recursively delete folder contents."""
    try:
        result = vm.list(MiniListRequest(path=path))
    except Exception:
        return

    for f in result.files:
        try:
            vm.delete(MiniDeleteRequest(path=f"{path}/{f}" if not f.startswith(path) else f))
        except Exception:
            pass

    for folder in result.folders:
        sub = f"{path}/{folder}" if not folder.startswith(path) else folder
        _clear_folder(vm, sub)


def _populate_vault(vm: MiniRuntimeClientSync, files: dict[str, str]) -> None:
    """Write all task files into the vault."""
    for path, content in files.items():
        try:
            vm.write(MiniWriteRequest(path=path, content=content))
        except Exception as e:
            print(f"  {C_RED}Failed to write {path}: {e}{C_CLR}")


def run_task(task: SyntheticTask) -> dict[str, Any]:
    """Run a single synthetic task and return results."""
    print(f"\n{'=' * 30} {task.task_id} {'=' * 30}")
    print(
        f"{C_BLUE}Instruction: {task.instruction[:100]}{'...' if len(task.instruction) > 100 else ''}{C_CLR}"
    )
    print(f"Expected: {C_CYAN}{task.expected_outcome}{C_CLR}")
    print(f"Tests: {task.description}")
    print("-" * 80)

    client = HarnessServiceClientSync(BITGN_HOST)

    # Start a sandbox playground (we use t01 as template)
    trial = client.start_playground(
        StartPlaygroundRequest(benchmark_id=SANDBOX_BENCH, task_id=TEMPLATE_TASK)
    )

    vm = MiniRuntimeClientSync(trial.harness_url)

    # Clear existing vault and populate with our test data
    _clear_vault(vm)
    _populate_vault(vm, task.vault_files)

    # Import here to avoid circular imports
    from codex_agent import run_codex_agent

    # Run the agent with our custom instruction
    started = time.time()
    try:
        run_codex_agent(
            model=os.getenv("MODEL_ID", DEFAULT_MODEL),
            harness_url=trial.harness_url,
            task_text=task.instruction,
            runtime="mini",
        )
    except Exception as e:
        print(f"{C_RED}Agent error: {e}{C_CLR}")

    elapsed = time.time() - started

    # End the trial (we won't check BitGN scoring since our vault is custom)
    try:
        client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
    except Exception:
        pass

    # Note: We can't easily capture the TaskResult from run_agent since it
    # submits directly. For now, we rely on visual inspection of the output.
    # TODO: Refactor run_agent to return TaskResult for testability.
    return {
        "task_id": task.task_id,
        "elapsed": elapsed,
        "expected_outcome": task.expected_outcome,
    }


def list_tasks() -> None:
    """Print all available synthetic tasks."""
    categories: dict[str, list[SyntheticTask]] = {}
    for task in ALL_TASKS:
        cat = task.task_id.split("-")[0]
        categories.setdefault(cat, []).append(task)

    print(f"\n{C_CYAN}Synthetic Tasks ({len(ALL_TASKS)} total){C_CLR}\n")
    for cat, tasks in categories.items():
        cat_names = {
            "trunc": "Truncation & Malformed",
            "inject": "Prompt Injection",
            "ambig": "Compound Ambiguity",
            "workflow": "Complex Workflows",
            "edge": "Edge Cases",
        }
        print(f"{C_GREEN}{cat_names.get(cat, cat)} ({len(tasks)}){C_CLR}")
        for t in tasks:
            print(f"  {t.task_id:12s} {t.expected_outcome:30s} {t.description[:60]}")
        print()


def main() -> None:
    args = sys.argv[1:]

    if "--list" in args:
        list_tasks()
        return

    # Filter tasks
    if "--category" in args:
        idx = args.index("--category")
        cat = args[idx + 1] if idx + 1 < len(args) else ""
        tasks = [t for t in ALL_TASKS if t.task_id.startswith(cat)]
    elif args and not args[0].startswith("--"):
        tasks = [TASKS_BY_ID[tid] for tid in args if tid in TASKS_BY_ID]
    else:
        tasks = ALL_TASKS

    if not tasks:
        print(f"{C_RED}No tasks found. Use --list to see available tasks.{C_CLR}")
        return

    print(f"\n{C_CYAN}╔══════════════════════════════════════════╗{C_CLR}")
    print(f"{C_CYAN}║  Synthetic Task Runner                   ║{C_CLR}")
    print(f"{C_CYAN}╚══════════════════════════════════════════╝{C_CLR}")
    print(f"\nRunning {len(tasks)} synthetic tasks...\n")

    results = []
    for task in tasks:
        try:
            result = run_task(task)
            results.append(result)
        except KeyboardInterrupt:
            print(f"\n{C_RED}Interrupted{C_CLR}")
            break
        except Exception as e:
            print(f"{C_RED}Runner error for {task.task_id}: {e}{C_CLR}")
            results.append(
                {
                    "task_id": task.task_id,
                    "elapsed": 0,
                    "expected_outcome": task.expected_outcome,
                    "error": str(e),
                }
            )

    # Summary
    if results:
        print(f"\n{'=' * 40} SUMMARY {'=' * 40}")
        for r in results:
            print(
                f"  {r['task_id']:12s} expected={r['expected_outcome']:30s} ({r['elapsed']:.1f}s)"
            )
        print()


if __name__ == "__main__":
    main()
