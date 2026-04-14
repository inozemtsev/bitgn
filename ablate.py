"""Autonomous ablation driver.

Runs the agent under a leave-one-out matrix from the current best config and
emits a markdown comparison table built from the per-run JSON artifacts that
main.py writes to ``runs/``.

Usage:
    uv run python ablate.py             # full sweep against PAC1
    uv run python ablate.py --sandbox   # cheaper smoke run against sandbox
    uv run python ablate.py --force     # re-run even if a JSON artifact exists
    uv run python ablate.py --only baseline no_multi_step
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"

BASELINE_ENV: dict[str, str] = {
    "MODEL_ID": "gpt-5.4",
    "WORKERS": "6",
}

ABLATIONS: list[tuple[str, dict[str, str]]] = [
    ("baseline", {}),
    ("no_vault_tags", {"VAULT_TAGS": "0"}),
    ("no_multi_step", {"CODEX_MULTI_STEP": "off"}),
    ("no_auto_discovery", {"AUTO_DISCOVERY": "0"}),
    ("no_compact_prompt", {"COMPACT_PROMPT": "0"}),
    ("no_grounding_refs", {"GROUNDING_REFS": "0"}),
    ("reasoning_medium", {"CODEX_REASONING_EFFORT": "medium"}),
    ("reasoning_low", {"CODEX_REASONING_EFFORT": "low"}),
    ("model_gpt53codex", {"MODEL_ID": "gpt-5.3-codex"}),
]

NON_ABLATABLE = [
    "Custom MCP server (foundational architecture)",
    "Security-aware instructions (embedded in prompts/instructions.md)",
    "Structured output + outcome codes (load-bearing for result parsing)",
    "Logfire observability (~20 scattered call sites)",
]


def _artifact_for(name: str) -> Path | None:
    """Most recent JSON artifact for ablation ``name``, or None if missing."""
    safe = f"ablation-{name}".replace("/", "_")
    matches = sorted(RUNS_DIR.glob(f"{safe}_*.json"))
    return matches[-1] if matches else None


def _run_one(name: str, overrides: dict[str, str], bench_id: str, *, force: bool) -> int:
    if not force and _artifact_for(name):
        print(f"[skip] {name}: artifact exists (use --force to re-run)")
        return 0

    env = os.environ.copy()
    env.update(BASELINE_ENV)
    env.update(overrides)
    env["BENCH_ID"] = bench_id
    env["RUN_NAME"] = f"ablation-{name}"

    RUNS_DIR.mkdir(exist_ok=True)
    log_path = RUNS_DIR / f"ablation-{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    print(f"\n=== [{name}] overrides={overrides or '{}'} bench={bench_id} ===")
    print(f"    log -> {log_path}")
    started = time.time()
    with log_path.open("w") as log:
        proc = subprocess.run(
            ["uv", "run", "python", "main.py"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
        )
    print(f"    exit={proc.returncode} elapsed={time.time() - started:.0f}s")
    return proc.returncode


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _fmt_elapsed(s: float) -> str:
    if s >= 3600:
        return f"{int(s // 3600)}h{int((s % 3600) // 60)}m"
    return f"{int(s // 60)}m{int(s % 60)}s"


def _aggregate(exit_codes: dict[str, int]) -> str:
    rows: list[dict[str, Any]] = []
    for name, _ in ABLATIONS:
        path = _artifact_for(name)
        if not path:
            rows.append(
                {"name": name, "missing": True, "exit": exit_codes.get(name, "?")}
            )
            continue
        data = json.loads(path.read_text())
        tasks = data.get("tasks", [])
        total_tokens = sum(
            (t.get("input_tokens") or 0) + (t.get("output_tokens") or 0) for t in tasks
        )
        won = sum(1 for t in tasks if (t.get("score") or 0) >= 1.0)
        rows.append(
            {
                "name": name,
                "missing": False,
                "final": data.get("final_score_pct", 0.0),
                "task_count": data.get("task_count", 0),
                "won": won,
                "tokens": total_tokens,
                "elapsed": data.get("elapsed_s", 0.0),
                "exit": exit_codes.get(name, 0),
                "artifact": path.name,
            }
        )

    baseline = next((r for r in rows if r["name"] == "baseline" and not r["missing"]), None)
    base_score = baseline["final"] if baseline else None

    def _delta(r: dict[str, Any]) -> float:
        return (r["final"] - base_score) if (base_score is not None and not r["missing"]) else 0.0

    rows.sort(key=lambda r: (r["missing"], _delta(r)))

    out: list[str] = []
    out.append("# Ablation results\n")
    out.append(f"_Generated {datetime.now(tz=timezone.utc).isoformat()}_\n")
    out.append(f"Baseline: {BASELINE_ENV}\n")
    out.append("| Config | Final % | Δ vs baseline | Won | Total tokens | Wall time | Exit | Notes |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for r in rows:
        if r["missing"]:
            out.append(f"| {r['name']} | — | — | — | — | — | {r['exit']} | no artifact |")
            continue
        delta = _delta(r)
        delta_str = "—" if r["name"] == "baseline" else f"{delta:+.2f}"
        out.append(
            f"| {r['name']} | {r['final']:.2f} | {delta_str} | "
            f"{r['won']}/{r['task_count']} | {_fmt_tokens(r['tokens'])} | "
            f"{_fmt_elapsed(r['elapsed'])} | {r['exit']} | {r['artifact']} |"
        )

    out.append("\n## Non-ablatable features (kept on for all runs)")
    for n in NON_ABLATABLE:
        out.append(f"- {n}")
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sandbox", action="store_true", help="Use bitgn/sandbox instead of pac1-dev")
    p.add_argument("--force", action="store_true", help="Re-run even if artifact exists")
    p.add_argument("--only", nargs="+", help="Run only the named ablations")
    p.add_argument("--no-run", action="store_true", help="Skip runs, just rebuild the table")
    args = p.parse_args()

    bench_id = "bitgn/sandbox" if args.sandbox else "bitgn/pac1-prod"
    selected = ABLATIONS if not args.only else [a for a in ABLATIONS if a[0] in set(args.only)]
    if args.only and len(selected) != len(args.only):
        unknown = set(args.only) - {a[0] for a in ABLATIONS}
        print(f"Unknown ablation(s): {unknown}", file=sys.stderr)
        sys.exit(2)

    exit_codes: dict[str, int] = {}
    if not args.no_run:
        for name, overrides in selected:
            exit_codes[name] = _run_one(name, overrides, bench_id, force=args.force)

    table = _aggregate(exit_codes)
    RUNS_DIR.mkdir(exist_ok=True)
    out_path = RUNS_DIR / f"ablation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out_path.write_text(table)
    print(f"\n{table}")
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
