"""BitGN Codex agent.

Runs a single `codex exec --full-auto` per task with a custom MCP server attached.
The MCP server (`vault_mcp_server.py`) exposes vault tools via gRPC to the BitGN VM.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import logfire
from bitgn.vm.mini_connect import MiniRuntimeClientSync
from bitgn.vm.mini_pb2 import (
    AnswerRequest as MiniAnswerRequest,
    OutlineRequest,
    ReadRequest as MiniReadRequest,
)
from bitgn.vm.pcm_connect import PcmRuntimeClientSync
from bitgn.vm.pcm_pb2 import (
    AnswerRequest,
    ContextRequest,
    Outcome as PcmOutcome,
    ReadRequest,
    TreeRequest,
)
from connectrpc.errors import ConnectError
from google.protobuf.json_format import MessageToDict
from opentelemetry import propagate
from pydantic import BaseModel, Field

from config import (
    CODEX_MULTI_STEP,
    CODEX_REASONING_EFFORT,
    CODEX_TIMEOUT_SEC,
    RETRY_ATTEMPTS,
    RETRY_BACKOFF_SEC,
    VAULT_TREE_DEPTH,
)
from prompts import CODEX_PREAMBLE, INSTRUCTIONS, MULTI_STEP_PROTOCOL
from vault_utils import (
    C_BLUE,
    C_CLR,
    C_CYAN,
    C_GREEN,
    C_RED,
    C_YELLOW,
    format_mini_outline,
    format_pcm_tree,
    tprint,
)

# ══════════════════════════════════════════════════════════════════════════════
# Outcome codes + structured result
# ══════════════════════════════════════════════════════════════════════════════


class Outcome(StrEnum):
    OK = "OUTCOME_OK"
    DENIED_SECURITY = "OUTCOME_DENIED_SECURITY"
    NONE_CLARIFICATION = "OUTCOME_NONE_CLARIFICATION"
    NONE_UNSUPPORTED = "OUTCOME_NONE_UNSUPPORTED"
    ERR_INTERNAL = "OUTCOME_ERR_INTERNAL"


class TaskResult(BaseModel):
    """Final structured output the agent must produce."""

    message: str = Field(..., description="Answer or summary for the task")
    outcome: Outcome = Field(Outcome.OK, description="Task outcome code")
    grounding_refs: list[str] = Field(
        default_factory=list, description="File paths that support the answer"
    )
    completed_steps: list[str] = Field(
        default_factory=list, description="Laconic list of what was done"
    )


# ══════════════════════════════════════════════════════════════════════════════
# VMClient protocol — thin abstraction over the two BitGN runtimes
# ══════════════════════════════════════════════════════════════════════════════


class VMClient(Protocol):
    """Subset of vault operations used by the agent (not the MCP server)."""

    def tree_text(self, root: str = "/", level: int = VAULT_TREE_DEPTH) -> str: ...
    def read(self, path: str) -> str: ...
    def context_json(self) -> str | None: ...
    def submit_answer(self, outcome: Outcome, message: str, refs: list[str]) -> None: ...


_PCM_OUTCOME_MAP = {
    Outcome.OK: PcmOutcome.OUTCOME_OK,
    Outcome.DENIED_SECURITY: PcmOutcome.OUTCOME_DENIED_SECURITY,
    Outcome.NONE_CLARIFICATION: PcmOutcome.OUTCOME_NONE_CLARIFICATION,
    Outcome.NONE_UNSUPPORTED: PcmOutcome.OUTCOME_NONE_UNSUPPORTED,
    Outcome.ERR_INTERNAL: PcmOutcome.OUTCOME_ERR_INTERNAL,
}


class PcmVMClient:
    """Wraps PcmRuntimeClientSync with a uniform interface."""

    def __init__(self, raw: PcmRuntimeClientSync) -> None:
        self._raw = raw

    def tree_text(self, root: str = "/", level: int = VAULT_TREE_DEPTH) -> str:
        return format_pcm_tree(_retry(self._raw.tree, TreeRequest(root=root, level=level)))

    def read(self, path: str) -> str:
        return _retry(self._raw.read, ReadRequest(path=path)).content

    def context_json(self) -> str | None:
        result = _retry(self._raw.context, ContextRequest())
        return json.dumps(MessageToDict(result), indent=2)

    def submit_answer(self, outcome: Outcome, message: str, refs: list[str]) -> None:
        _retry(
            self._raw.answer,
            AnswerRequest(
                message=message,
                outcome=_PCM_OUTCOME_MAP[outcome],
                refs=refs,
            ),
        )


class MiniVMClient:
    """Wraps MiniRuntimeClientSync with a uniform interface."""

    def __init__(self, raw: MiniRuntimeClientSync) -> None:
        self._raw = raw

    def tree_text(self, root: str = "/", level: int = VAULT_TREE_DEPTH) -> str:
        return format_mini_outline(_retry(self._raw.outline, OutlineRequest(path=root)))

    def read(self, path: str) -> str:
        return _retry(self._raw.read, MiniReadRequest(path=path)).content

    def context_json(self) -> str | None:
        # Mini runtime has no context RPC.
        return None

    def submit_answer(self, outcome: Outcome, message: str, refs: list[str]) -> None:
        # Mini runtime has no outcome field; the harness derives pass/fail from the answer.
        _ = outcome
        _retry(self._raw.answer, MiniAnswerRequest(answer=message, refs=refs))


def create_vm(harness_url: str, runtime: str) -> VMClient:
    """Build the right VMClient for the requested runtime."""
    if runtime == "pcm":
        return PcmVMClient(PcmRuntimeClientSync(harness_url))
    if runtime == "mini":
        return MiniVMClient(MiniRuntimeClientSync(harness_url))
    raise ValueError(f"Unknown runtime: {runtime!r}")


# ══════════════════════════════════════════════════════════════════════════════
# Retry helper
# ══════════════════════════════════════════════════════════════════════════════


def _retry[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Retry ``fn`` on transient BitGN gRPC errors.

    Why: the harness occasionally returns DEADLINE_EXCEEDED under load; a short
    backoff turns intermittent timeouts into silent retries rather than task failures.
    """
    last_exc: ConnectError | None = None
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except ConnectError as exc:
            last_exc = exc
            is_timeout = (
                "DEADLINE_EXCEEDED" in str(exc)
                or "timed out" in str(getattr(exc, "message", "")).lower()
            )
            if is_timeout and attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            raise
    assert last_exc is not None
    raise last_exc


# ══════════════════════════════════════════════════════════════════════════════
# AgentDeps — lightweight container passed through the task pipeline
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class AgentDeps:
    """Runtime dependencies for a single task."""

    vm: VMClient
    runtime: str  # "pcm" or "mini" — kept for logging/prompt plumbing


# ══════════════════════════════════════════════════════════════════════════════
# Auto-discovery + prompt assembly
# ══════════════════════════════════════════════════════════════════════════════


def _auto_discover(deps: AgentDeps, task_id: str = "") -> str:
    """Fetch vault tree, AGENTS.md, and task context to seed the agent."""
    parts: list[str] = []

    try:
        tree_text = deps.vm.tree_text("/", VAULT_TREE_DEPTH)
        parts.append(f"## Vault structure\n```\n{tree_text}\n```")
        tprint(task_id, f"{C_GREEN}AUTO{C_CLR}: tree -> {tree_text[:200]}...")
    except ConnectError as exc:
        logfire.warn("auto-discover tree failed", task_id=task_id, error=str(exc))

    try:
        agents = deps.vm.read("AGENTS.md")
        parts.append(f"## AGENTS.md\n{agents}")
        tprint(task_id, f"{C_GREEN}AUTO{C_CLR}: read AGENTS.md -> {agents[:200]}...")
    except ConnectError as exc:
        logfire.warn("auto-discover AGENTS.md failed", task_id=task_id, error=str(exc))

    try:
        ctx_text = deps.vm.context_json()
        if ctx_text is not None:
            parts.append(f"## Task context\n```json\n{ctx_text}\n```")
            tprint(task_id, f"{C_GREEN}AUTO{C_CLR}: context -> {ctx_text[:200]}...")
    except ConnectError as exc:
        logfire.warn("auto-discover context failed", task_id=task_id, error=str(exc))

    return "\n\n".join(parts)


def build_prompt(discovery: str, task_text: str) -> str:
    """Assemble the user prompt sent to Codex."""
    hint = os.environ.get("HINT", "")
    parts: list[str] = []
    if hint:
        parts.append(hint)
    parts.append(discovery)
    parts.append("---")
    parts.append(f"## TASK\n{task_text}")
    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Codex CLI glue
# ══════════════════════════════════════════════════════════════════════════════


def _ensure_no_additional_props(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Pydantic-emitted schema for Codex: add additionalProperties=False
    on every object, and strip keywords that OpenAI rejects alongside a $ref."""
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        # OpenAI: "$ref cannot have keywords {'default', 'description', ...}".
        return {"$ref": schema["$ref"]}
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
    for key in ("properties", "items", "$defs", "definitions"):
        val = schema.get(key)
        if isinstance(val, dict):
            for k, v in val.items():
                if isinstance(v, dict):
                    val[k] = _ensure_no_additional_props(v)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            schema[key] = [_ensure_no_additional_props(s) for s in schema[key]]
    return schema


def _write_temp_schema(schema: dict[str, Any]) -> str:
    """Write JSON schema to a temp file, return its path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(schema, f)
        return f.name


def _parse_jsonl(output: str) -> tuple[str, str, dict[str, Any]]:
    """Parse Codex JSONL events -> (response_text, thread_id, usage)."""
    thread_id = ""
    response_text = ""
    usage: dict[str, Any] = {}

    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type")
        if etype == "thread.started":
            thread_id = event.get("thread_id", "")
        elif etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                response_text = item.get("text", "")
        elif etype == "turn.completed":
            usage = event.get("usage", {})
        elif etype == "turn.failed":
            err = event.get("error", {}).get("message", "unknown error")
            raise RuntimeError(f"Codex turn failed: {err}")

    return response_text, thread_id, usage


def _log_codex_events(output: str, task_id: str) -> None:
    """Stream each Codex JSONL event into Logfire for observability."""
    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        if etype == "item.completed":
            item = event.get("item", {})
            itype = item.get("type", "")
            if itype == "tool_call":
                logfire.info(
                    "codex tool call",
                    task_id=task_id,
                    tool_name=item.get("name", ""),
                    tool_arguments=item.get("arguments", ""),
                    tool_output=item.get("output", ""),
                )
            elif itype == "agent_message":
                logfire.info("codex agent message", task_id=task_id, text=item.get("text", ""))
            elif itype == "user_message":
                logfire.info("codex user message", task_id=task_id, text=item.get("text", ""))
        elif etype == "turn.failed":
            err = event.get("error", {})
            logfire.error(
                "codex turn failed",
                task_id=task_id,
                error_message=err.get("message", ""),
                error_code=err.get("code", ""),
            )


def _build_codex_cmd(
    *,
    model: str,
    harness_url: str,
    runtime: str,
    log_path: str,
    refs_path: str,
    traceparent: str,
    compact_prompt_path: str,
    schema_path: str,
    full_prompt: str,
) -> list[str]:
    """Assemble the argv for the `codex exec` subprocess."""
    return [
        "codex",
        "exec",
        "--json",
        "--full-auto",
        "--skip-git-repo-check",
        "-m",
        model,
        "-c",
        f"model_reasoning_effort={CODEX_REASONING_EFFORT}",
        "-c",
        f'mcp_servers.bitgn-vault.env.VAULT_HARNESS_URL="{harness_url}"',
        "-c",
        f'mcp_servers.bitgn-vault.env.VAULT_RUNTIME="{runtime}"',
        "-c",
        f'mcp_servers.bitgn-vault.env.VAULT_MCP_LOG="{log_path}"',
        "-c",
        f'mcp_servers.bitgn-vault.env.VAULT_MCP_REFS="{refs_path}"',
        "-c",
        f'mcp_servers.bitgn-vault.env.LOGFIRE_TOKEN="{os.environ.get("LOGFIRE_TOKEN", "")}"',
        "-c",
        f'mcp_servers.bitgn-vault.env.TRACEPARENT="{traceparent}"',
        "-c",
        f"experimental_compact_prompt_file={compact_prompt_path}",
        "--output-schema",
        schema_path,
        full_prompt,
    ]


def _read_server_refs(refs_path: str) -> list[str]:
    """Read the server-tracked grounding refs JSON file, deleting it on success."""
    try:
        if not os.path.exists(refs_path):
            return []
        with open(refs_path) as f:
            refs: list[str] = json.load(f)
        os.unlink(refs_path)
        return refs
    except (OSError, json.JSONDecodeError) as exc:
        logfire.warn("failed to read server refs", path=refs_path, error=str(exc))
        return []


def _submit_error(deps: AgentDeps, message: str) -> None:
    """Submit an error result when codex exec fails."""
    try:
        deps.vm.submit_answer(Outcome.ERR_INTERNAL, message, [])
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════


def run_codex_agent(
    model: str,
    harness_url: str,
    task_text: str,
    runtime: str = "pcm",
    task_id: str = "",
) -> None:
    """Run a BitGN task using a single codex exec call with MCP tools."""
    with logfire.span(
        "codex agent {task_id}",
        task_id=task_id,
        model=model,
        runtime=runtime,
        run_name=os.environ.get("RUN_NAME", ""),
    ) as span:
        deps = AgentDeps(vm=create_vm(harness_url, runtime), runtime=runtime)

        discovery = _auto_discover(deps, task_id=task_id)
        user_prompt = build_prompt(discovery, task_text)
        instructions = INSTRUCTIONS + (MULTI_STEP_PROTOCOL if CODEX_MULTI_STEP else "")
        full_prompt = f"{CODEX_PREAMBLE}{instructions}\n\n{user_prompt}"

        # Codex API requires ALL properties in 'required', even those with defaults.
        schema = _ensure_no_additional_props(TaskResult.model_json_schema())
        if "properties" in schema:
            schema["required"] = list(schema["properties"].keys())
        schema_path = _write_temp_schema(schema)

        here = Path(__file__).resolve().parent
        compact_prompt_path = str(here / "compact_prompt.md")
        log_path = str(here / "vault_mcp.log")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            refs_path = f.name

        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        traceparent = carrier.get("traceparent", "")

        cmd = _build_codex_cmd(
            model=model,
            harness_url=harness_url,
            runtime=runtime,
            log_path=log_path,
            refs_path=refs_path,
            traceparent=traceparent,
            compact_prompt_path=compact_prompt_path,
            schema_path=schema_path,
            full_prompt=full_prompt,
        )

        tprint(task_id, f"\n{C_CYAN}Running codex exec (native MCP) with {model}...{C_CLR}")
        started = time.time()
        logfire.info("codex prompt", task_id=task_id, prompt=full_prompt, model=model)

        task_result: TaskResult | None = None
        usage: dict[str, Any] = {}

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=CODEX_TIMEOUT_SEC,
                )
                output = result.stdout + result.stderr

                if result.returncode != 0 and not result.stdout.strip():
                    tprint(task_id, f"{C_RED}codex exec failed (rc={result.returncode}):{C_CLR}")
                    tprint(task_id, result.stderr[:500] if result.stderr else "(no output)")
                    span.set_attribute("error", True)
                    logfire.error(
                        "codex exec failed",
                        task_id=task_id,
                        returncode=result.returncode,
                        stderr=result.stderr[:1000],
                    )
                    _submit_error(deps, "codex exec failed")
                    return

                _log_codex_events(output, task_id)
                response_text, _thread_id, usage = _parse_jsonl(output)

                if not response_text:
                    tprint(task_id, f"{C_RED}No response from codex exec{C_CLR}")
                    span.set_attribute("error", True)
                    logfire.error("codex empty response", task_id=task_id)
                    _submit_error(deps, "Empty response from codex exec")
                    return

                task_result = TaskResult.model_validate_json(response_text)
                logfire.info(
                    "codex response",
                    task_id=task_id,
                    outcome=task_result.outcome,
                    message=task_result.message,
                    grounding_refs=task_result.grounding_refs,
                    completed_steps=task_result.completed_steps,
                    response_text=response_text,
                )
                break

            except subprocess.TimeoutExpired:
                if attempt < RETRY_ATTEMPTS:
                    tprint(
                        task_id,
                        f"{C_YELLOW}codex exec timed out ({CODEX_TIMEOUT_SEC}s), "
                        f"retrying (attempt {attempt + 1}/{RETRY_ATTEMPTS})...{C_CLR}",
                    )
                    logfire.warn("codex timeout retry", task_id=task_id, attempt=attempt)
                    continue
                tprint(
                    task_id,
                    f"{C_RED}codex exec timed out ({CODEX_TIMEOUT_SEC}s) "
                    f"after {RETRY_ATTEMPTS} attempts{C_CLR}",
                )
                span.set_attribute("error", True)
                logfire.error("codex timeout", task_id=task_id)
                _submit_error(deps, "codex exec timed out")
                return
            except Exception as exc:
                tprint(task_id, f"{C_RED}codex exec error: {exc}{C_CLR}")
                span.set_attribute("error", True)
                logfire.error("codex exec error", task_id=task_id, error=str(exc))
                _submit_error(deps, f"codex exec error: {exc}")
                return

        assert task_result is not None
        elapsed = time.time() - started

        if usage:
            inp = usage.get("input_tokens", 0)
            cached = usage.get("cached_input_tokens", 0)
            out = usage.get("output_tokens", 0)
            reasoning = usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
            logfire.info(
                "codex token usage",
                task_id=task_id,
                input_tokens=inp,
                cached_input_tokens=cached,
                output_tokens=out,
                reasoning_tokens=reasoning,
                total_tokens=inp + out,
            )

        # Server-tracked refs are deterministic; prefer them over model-reported ones.
        task_result.grounding_refs = _read_server_refs(refs_path)

        try:
            deps.vm.submit_answer(
                task_result.outcome, task_result.message, task_result.grounding_refs
            )
        except Exception as exc:
            tprint(task_id, f"{C_RED}Submit error: {exc}{C_CLR}")

        span.set_attribute("outcome", task_result.outcome)
        span.set_attribute("elapsed_s", elapsed)

    status_color = C_GREEN if task_result.outcome == Outcome.OK else C_YELLOW
    tprint(task_id, f"\n{status_color}=== Agent {task_result.outcome} ({elapsed:.1f}s) ==={C_CLR}")
    for s in task_result.completed_steps:
        tprint(task_id, f"  - {s}")
    tprint(task_id, f"\n{C_BLUE}ANSWER: {task_result.message}{C_CLR}")
    if task_result.grounding_refs:
        tprint(task_id, f"  Refs: {', '.join(task_result.grounding_refs)}")
    if usage:
        inp = usage.get("input_tokens", 0)
        cached = usage.get("cached_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        reasoning = usage.get("output_tokens_details", {}).get("reasoning_tokens", 0)
        tprint(
            task_id,
            f"  Tokens: {inp + out} (in={inp} cached={cached} out={out} reasoning={reasoning})",
        )
