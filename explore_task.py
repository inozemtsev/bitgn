"""Quick script to explore vault contents for a specific task.

Usage:
    uv run python explore_task.py t20                  # list inbox + read AGENTS.md
    uv run python explore_task.py t20 path/to/file.md  # read specific files
"""

from __future__ import annotations

import os
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    StartPlaygroundRequest,
    StartRunRequest,
    StartTrialRequest,
    SubmitRunRequest,
)
from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import (
    ListRequest as MiniListRequest,
    OutlineRequest,
    ReadRequest as MiniReadRequest,
)
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    ListRequest as PcmListRequest,
    ReadRequest as PcmReadRequest,
    TreeRequest,
)

BITGN_HOST = os.getenv("BITGN_HOST") or "https://api.bitgn.com"
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or "bitgn/pac1-dev"


def _find_trial(client: HarnessServiceClientSync, task_id: str) -> tuple[Any, str | None]:
    """Return (trial, run_id). In PAC1 mode the caller must submit_run(run_id) when done."""
    if "sandbox" in BENCH_ID:
        trial = client.start_playground(
            StartPlaygroundRequest(benchmark_id=BENCH_ID, task_id=task_id)
        )
        return trial, None

    run = client.start_run(
        StartRunRequest(name="explore", benchmark_id=BENCH_ID, api_key=BITGN_API_KEY)
    )
    for tid in run.trial_ids:
        t = client.start_trial(StartTrialRequest(trial_id=tid))
        if t.task_id == task_id:
            return t, run.run_id
    client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
    raise RuntimeError(f"Task {task_id} not found in run")


def _print_tree_pcm(vm: PcmRuntimeClientSync) -> None:
    tree = vm.tree(TreeRequest(root="/", level=3))
    print("=== TREE (level 3) ===")
    for c in tree.root.children:
        print(f"  {c.name}")


def _print_tree_mini(vm: MiniRuntimeClientSync) -> None:
    tree = vm.outline(OutlineRequest(path="/"))
    print("=== TREE ===")
    for folder in tree.folders:
        print(f"  {folder}/")
    for f in tree.files:
        print(f"  {f.path}")


def _read_pcm(vm: PcmRuntimeClientSync, path: str) -> str:
    return vm.read(PcmReadRequest(path=path)).content


def _read_mini(vm: MiniRuntimeClientSync, path: str) -> str:
    return vm.read(MiniReadRequest(path=path)).content


def _list_pcm(vm: PcmRuntimeClientSync, path: str) -> list[str]:
    r = vm.list(PcmListRequest(name=path))
    return [e.name for e in r.entries]


def _list_mini(vm: MiniRuntimeClientSync, path: str) -> list[str]:
    r = vm.list(MiniListRequest(path=path))
    return [f"{f}/" for f in r.folders] + list(r.files)


def explore(task_id: str, extra_files: list[str]) -> None:
    client = HarnessServiceClientSync(BITGN_HOST)
    trial, run_id = _find_trial(client, task_id)

    print(f"Task: {task_id}")
    print(f"Instruction: {trial.instruction}")
    print(f"Harness URL: {trial.harness_url}\n")

    use_pcm = "sandbox" not in BENCH_ID
    pcm_vm = PcmRuntimeClientSync(trial.harness_url) if use_pcm else None
    mini_vm = None if use_pcm else MiniRuntimeClientSync(trial.harness_url)

    if pcm_vm is not None:
        _print_tree_pcm(pcm_vm)
    else:
        assert mini_vm is not None
        _print_tree_mini(mini_vm)
    print()

    def list_dir(path: str) -> list[str]:
        if pcm_vm is not None:
            return _list_pcm(pcm_vm, path)
        assert mini_vm is not None
        return _list_mini(mini_vm, path)

    def read_file(path: str) -> str:
        if pcm_vm is not None:
            return _read_pcm(pcm_vm, path)
        assert mini_vm is not None
        return _read_mini(mini_vm, path)

    files_to_read = list(extra_files)
    if not files_to_read:
        files_to_read = ["AGENTS.md"]
        for subdir in ("inbox", "docs/channels"):
            try:
                items = list_dir(subdir)
                for f in items:
                    if not f.endswith("/"):
                        files_to_read.append(f"{subdir}/{f}")
                print(f"{subdir}: {items}")
            except Exception:
                pass

        try:
            docs = list_dir("docs")
            for f in docs:
                if "inbox" in f.lower() and not f.endswith("/"):
                    files_to_read.append(f"docs/{f}")
            print(f"docs: {docs}")
        except Exception:
            pass

        if "inbox/README.md" not in files_to_read:
            files_to_read.append("inbox/README.md")

    for path in files_to_read:
        try:
            content = read_file(path)
            print(f"\n=== {path} ({len(content)} chars) ===")
            print(content)
        except Exception as exc:
            print(f"\n=== {path} === ERROR: {exc}")

    client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
    if run_id is not None:
        client.submit_run(SubmitRunRequest(run_id=run_id, force=True))


if __name__ == "__main__":
    task_id = sys.argv[1] if len(sys.argv) > 1 else "t20"
    explore(task_id, sys.argv[2:])
