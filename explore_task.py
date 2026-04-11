"""Quick script to explore vault contents for a specific task."""
import json
import sys
import os

from dotenv import load_dotenv
load_dotenv()

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    StatusRequest, GetBenchmarkRequest, StartPlaygroundRequest, EndTrialRequest,
)
from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import (
    ReadRequest as MiniReadRequest, ListRequest as MiniListRequest,
    OutlineRequest, SearchRequest as MiniSearchRequest,
)
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    ReadRequest, ListRequest, TreeRequest, SearchRequest,
)

from bitgn.harness_pb2 import StartRunRequest, StartTrialRequest, SubmitRunRequest

BITGN_HOST = os.getenv("BITGN_HOST") or "https://api.bitgn.com"
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or "bitgn/pac1-dev"

def explore(task_id: str):
    client = HarnessServiceClientSync(BITGN_HOST)

    if "sandbox" in BENCH_ID:
        trial = client.start_playground(StartPlaygroundRequest(
            benchmark_id=BENCH_ID, task_id=task_id
        ))
    else:
        run = client.start_run(StartRunRequest(
            name="explore", benchmark_id=BENCH_ID, api_key=BITGN_API_KEY,
        ))
        # Find the trial for the requested task
        trial = None
        for tid in run.trial_ids:
            t = client.start_trial(StartTrialRequest(trial_id=tid))
            if t.task_id == task_id:
                trial = t
                break
        if not trial:
            print(f"Task {task_id} not found in run")
            client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
            return
    print(f"Task: {task_id}")
    print(f"Instruction: {trial.instruction}")
    print(f"Harness URL: {trial.harness_url}")
    print()

    use_pcm = "sandbox" not in BENCH_ID
    if use_pcm:
        vm = PcmRuntimeClientSync(trial.harness_url)
    else:
        vm = MiniRuntimeClientSync(trial.harness_url)

    def read_file(path):
        if use_pcm:
            return vm.read(ReadRequest(path=path)).content
        return vm.read(MiniReadRequest(path=path)).content

    def list_dir(path):
        if use_pcm:
            r = vm.list(ListRequest(name=path))
            return [e.name for e in r.entries]
        r = vm.list(MiniListRequest(path=path))
        return [f"{f}/" for f in r.folders] + list(r.files)

    # Tree
    if use_pcm:
        tree = vm.tree(TreeRequest(root="/", level=3))
        print(f"=== TREE (level 3) ===")
        # Just print root children
        for c in tree.root.children:
            print(f"  {c.name}")
    else:
        tree = vm.outline(OutlineRequest(path="/"))
        print("=== TREE ===")
        for f in tree.folders:
            print(f"  {f}/")
        for f in tree.files:
            print(f"  {f.path}")
    print()

    # Read key files
    files_to_read = sys.argv[2:] if len(sys.argv) > 2 else []

    if not files_to_read:
        files_to_read = ["AGENTS.md"]

        # List inbox
        try:
            items = list_dir("inbox")
            for f in items:
                if not f.endswith("/"):
                    files_to_read.append(f"inbox/{f}")
            print(f"Inbox: {items}")
        except Exception:
            pass

        # List docs/channels
        try:
            items = list_dir("docs/channels")
            for f in items:
                if not f.endswith("/"):
                    files_to_read.append(f"docs/channels/{f}")
            print(f"Channels: {items}")
        except Exception:
            pass

        # docs/inbox-*
        try:
            items = list_dir("docs")
            for f in items:
                if "inbox" in f.lower() and not f.endswith("/"):
                    files_to_read.append(f"docs/{f}")
            print(f"Docs: {items}")
        except Exception:
            pass

        if "inbox/README.md" not in files_to_read:
            files_to_read.append("inbox/README.md")

    for path in files_to_read:
        try:
            content = read_file(path)
            print(f"\n=== {path} ({len(content)} chars) ===")
            print(content)
        except Exception as e:
            print(f"\n=== {path} === ERROR: {e}")

    # End trial and cleanup
    client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
    if "sandbox" not in BENCH_ID:
        client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))

if __name__ == "__main__":
    task_id = sys.argv[1] if len(sys.argv) > 1 else "t20"
    explore(task_id)
