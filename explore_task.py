"""Quick read-only exploration of a BitGN task's vault.

Uses `start_playground` for all benchmarks (including pac1-prod), so no
leaderboard run is ever created. Prints the instruction + vault tree, reads
interesting files, ends the trial.

Usage:
    BENCH_ID=bitgn/pac1-prod uv run python explore_task.py t005
    BENCH_ID=bitgn/pac1-prod uv run python explore_task.py t022 00_inbox/000_invoice-bundle-request.md
    BENCH_ID=bitgn/sandbox    uv run python explore_task.py t01

Env:
    BENCH_ID        - bitgn/sandbox | bitgn/pac1-dev | bitgn/pac1-prod
    BITGN_HOST      - default https://api.bitgn.com
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
BENCH_ID = os.getenv("BENCH_ID") or "bitgn/pac1-dev"


def _is_sandbox() -> bool:
    return "sandbox" in BENCH_ID


def _find_trial(client: HarnessServiceClientSync, task_id: str) -> Any:
    """Start a playground trial for task_id. Never creates a leaderboard run."""
    return client.start_playground(
        StartPlaygroundRequest(benchmark_id=BENCH_ID, task_id=task_id)
    )


def _tree_pcm(vm: PcmRuntimeClientSync, level: int = 3) -> list[str]:
    tree = vm.tree(TreeRequest(root="/", level=level))
    top: list[str] = []

    def walk(entry: Any, depth: int, prefix: str = "") -> None:
        for c in entry.children:
            name = c.name
            kind = "/" if c.children or (getattr(c, "is_dir", False)) else ""
            print(f"{prefix}{name}{kind}")
            if depth == 0:
                top.append(name)
            if c.children and depth < level - 1:
                walk(c, depth + 1, prefix + "  ")

    walk(tree.root, 0)
    return top


def _tree_mini(vm: MiniRuntimeClientSync) -> list[str]:
    tree = vm.outline(OutlineRequest(path="/"))
    top: list[str] = []
    for folder in tree.folders:
        print(f"  {folder}/")
        top.append(folder.rstrip("/"))
    for f in tree.files:
        print(f"  {f.path}")
    return top


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


# Directories we want to auto-inspect if they exist. Covers both the
# sandbox-style ("inbox/", "docs/") and PAC1-style ("00_inbox/", "99_system/")
# lane names. `_find_candidates` checks actual presence against the vault tree.
_INBOX_HINTS = ("inbox", "00_inbox")
_SYSTEM_HINTS = ("99_system", "docs")
_OUTBOX_HINTS = ("60_outbox", "outbox")


def explore(task_id: str, extra_files: list[str]) -> None:
    client = HarnessServiceClientSync(BITGN_HOST)
    trial = _find_trial(client, task_id)

    print(f"Task:        {task_id}")
    print(f"Instruction: {trial.instruction}")
    print(f"Harness:     {trial.harness_url}\n")

    use_pcm = not _is_sandbox()
    pcm_vm = PcmRuntimeClientSync(trial.harness_url) if use_pcm else None
    mini_vm = None if use_pcm else MiniRuntimeClientSync(trial.harness_url)

    try:
        print("=== TREE ===")
        top_dirs = _tree_pcm(pcm_vm) if pcm_vm else _tree_mini(mini_vm)  # type: ignore[arg-type]
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
            files_to_read = ["AGENTS.md", "AGENTS.MD"]
            top_lower = {d.lower().rstrip("/") for d in top_dirs}
            for hint in _INBOX_HINTS + _SYSTEM_HINTS + _OUTBOX_HINTS:
                if hint.lower() in top_lower:
                    try:
                        items = list_dir(hint)
                        print(f"{hint}/: {items}")
                        for f in items:
                            if not f.endswith("/"):
                                files_to_read.append(f"{hint}/{f}")
                    except Exception as exc:
                        print(f"{hint}/: ERROR {exc}")

        seen: set[str] = set()
        for path in files_to_read:
            if path in seen:
                continue
            seen.add(path)
            try:
                content = read_file(path)
                print(f"\n=== {path} ({len(content)} chars) ===")
                print(content)
            except Exception as exc:
                # silently skip the AGENTS.md/AGENTS.MD fallback pair misses
                if path in ("AGENTS.md", "AGENTS.MD"):
                    continue
                print(f"\n=== {path} === ERROR: {exc}")
    finally:
        client.end_trial(EndTrialRequest(trial_id=trial.trial_id))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python explore_task.py <task_id> [path ...]")
        sys.exit(2)
    explore(sys.argv[1], sys.argv[2:])
