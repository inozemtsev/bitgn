"""
Direct Codex agent integration via MCP server.

Replaces the pydantic-ai + proxy path with a single `codex exec` call
that uses vault_mcp_server.py for tool access.
"""

import json
import os
import subprocess
import tempfile
import time

import logfire
from opentelemetry import propagate

from agent import (
    INSTRUCTIONS,
    MULTI_STEP_PROTOCOL,
    AgentDeps,
    TaskResult,
    _auto_discover,
    _create_vm,
    _retry,
    _submit_answer,
    _tprint,
    build_prompt,
    C_CYAN,
    C_GREEN,
    C_RED,
    C_YELLOW,
    C_BLUE,
    C_CLR,
)

CODEX_MULTI_STEP = os.environ.get("CODEX_MULTI_STEP", "off").lower()
def _write_temp_schema(schema: dict) -> str:
    """Write JSON schema to a temp file, return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(schema, f)
    f.close()
    return f.name


def ensure_no_additional_props(schema: dict) -> dict:
    """Recursively add additionalProperties: false to all objects (Codex requirement)."""
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
    for key in ("properties", "items", "$defs", "definitions"):
        if key in schema:
            val = schema[key]
            if isinstance(val, dict):
                for k, v in val.items():
                    if isinstance(v, dict):
                        val[k] = ensure_no_additional_props(v)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            schema[key] = [ensure_no_additional_props(s) for s in schema[key]]
    return schema


def parse_jsonl(output: str) -> tuple[str, str, dict]:
    """Parse Codex JSONL events -> (response_text, thread_id, usage)."""
    thread_id = ""
    response_text = ""
    usage = {}

    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id", "")
        elif event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                response_text = item.get("text", "")
        elif event.get("type") == "turn.completed":
            usage = event.get("usage", {})
        elif event.get("type") == "turn.failed":
            err = event.get("error", {}).get("message", "unknown error")
            raise RuntimeError(f"Codex turn failed: {err}")

    return response_text, thread_id, usage



def _log_codex_events(output: str, task_id: str) -> None:
    """Parse Codex JSONL output and log each event as a Logfire span."""
    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")
            if item_type == "tool_call":
                logfire.info(
                    "codex tool call",
                    task_id=task_id,
                    tool_name=item.get("name", ""),
                    tool_arguments=item.get("arguments", ""),
                    tool_output=item.get("output", ""),
                )
            elif item_type == "agent_message":
                logfire.info(
                    "codex agent message",
                    task_id=task_id,
                    text=item.get("text", ""),
                )
            elif item_type == "user_message":
                logfire.info(
                    "codex user message",
                    task_id=task_id,
                    text=item.get("text", ""),
                )
        elif event_type == "turn.failed":
            err = event.get("error", {})
            logfire.error(
                "codex turn failed",
                task_id=task_id,
                error_message=err.get("message", ""),
                error_code=err.get("code", ""),
            )


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
        # 1. Create VM client (for auto-discovery and answer submission)
        vm_client, actual_runtime = _create_vm(harness_url, runtime)
        deps = AgentDeps(vm=vm_client, runtime=actual_runtime)

        # 2. Auto-discover vault structure
        discovery = _auto_discover(deps, task_id=task_id)

        # 3. Build unified prompt (same content as other providers)
        user_prompt = build_prompt(discovery, task_text)
        codex_preamble = (
            "IMPORTANT: You are operating inside a VIRTUAL FILE SYSTEM accessed exclusively "
            "through MCP tools (vault_tree, vault_read, vault_write, vault_list, vault_search, "
            "vault_find, vault_delete, vault_mkdir, vault_move, vault_context, "
            "vault_read_all_in_dir, vault_grep_count). "
            "Do NOT use shell commands (ls, cat, echo, mkdir, etc.) to read or write vault files directly. "
            "They will NOT affect the virtual vault. You MUST use the vault_* MCP tools for ALL "
            "vault file operations. However, you CAN use shell commands (grep, wc, awk, jq) for "
            "computation and analysis AFTER reading file content via MCP tools and saving it locally.\n\n"
        )
        instructions = INSTRUCTIONS
        if CODEX_MULTI_STEP == "on":
            instructions += MULTI_STEP_PROTOCOL
        full_prompt = f"{codex_preamble}{instructions}\n\n{user_prompt}"

        # 4. Write TaskResult output schema
        #    Codex API requires ALL properties in 'required', even those with defaults
        schema = ensure_no_additional_props(TaskResult.model_json_schema())
        if "properties" in schema:
            schema["required"] = list(schema["properties"].keys())
        schema_path = _write_temp_schema(schema)

        # 5. Custom compaction prompt — preserves vault state across context compaction
        compact_prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compact_prompt.md")

        # 6. MCP log and refs paths
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_mcp.log")
        refs_path = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False).name

        # 7. Propagate trace context so MCP server spans nest under this span
        carrier = {}
        propagate.inject(carrier)
        traceparent = carrier.get("traceparent", "")

        # 8. Single codex exec call — per-task harness_url passed via -c override
        reasoning_effort = os.environ.get("CODEX_REASONING_EFFORT", "high")
        cmd = [
            "codex", "exec", "--json",
            "--full-auto",
            "--skip-git-repo-check",
            "-m", model,
            "-c", f"model_reasoning_effort={reasoning_effort}",
            "-c", f'mcp_servers.bitgn-vault.env.VAULT_HARNESS_URL="{harness_url}"',
            "-c", f'mcp_servers.bitgn-vault.env.VAULT_RUNTIME="{actual_runtime}"',
            "-c", f'mcp_servers.bitgn-vault.env.VAULT_MCP_LOG="{log_path}"',
            "-c", f'mcp_servers.bitgn-vault.env.VAULT_MCP_REFS="{refs_path}"',
            "-c", f'mcp_servers.bitgn-vault.env.LOGFIRE_TOKEN="{os.environ.get("LOGFIRE_TOKEN", "")}"',
            "-c", f'mcp_servers.bitgn-vault.env.TRACEPARENT="{traceparent}"',
            "-c", f"experimental_compact_prompt_file={compact_prompt_path}",
            "--output-schema", schema_path,
            full_prompt,
        ]

        _tprint(task_id, f"\n{C_CYAN}Running codex exec (native MCP) with {model}...{C_CLR}")
        started = time.time()

        # Log the full prompt sent to Codex
        logfire.info(
            "codex prompt",
            task_id=task_id,
            prompt=full_prompt,
            model=model,
        )

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=600,
                )
                output = result.stdout + result.stderr

                if result.returncode != 0:
                    if not result.stdout.strip():
                        _tprint(task_id, f"{C_RED}codex exec failed (rc={result.returncode}):{C_CLR}")
                        _tprint(task_id, result.stderr[:500] if result.stderr else "(no output)")
                        span.set_attribute("error", True)
                        logfire.error("codex exec failed", task_id=task_id, returncode=result.returncode, stderr=result.stderr[:1000])
                        _submit_error(deps, "codex exec failed")
                        return

                # 8. Parse JSONL output — log all events for full observability
                _log_codex_events(output, task_id)
                response_text, _thread_id, usage = parse_jsonl(output)

                if not response_text:
                    _tprint(task_id, f"{C_RED}No response from codex exec{C_CLR}")
                    span.set_attribute("error", True)
                    logfire.error("codex empty response", task_id=task_id)
                    _submit_error(deps, "Empty response from codex exec")
                    return

                # 9. Parse TaskResult
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
                break  # success — exit retry loop

            except subprocess.TimeoutExpired:
                if attempt < max_attempts:
                    _tprint(task_id, f"{C_YELLOW}codex exec timed out (600s), retrying (attempt {attempt + 1}/{max_attempts})...{C_CLR}")
                    logfire.warn("codex timeout retry", task_id=task_id, attempt=attempt)
                    continue
                _tprint(task_id, f"{C_RED}codex exec timed out (600s) after {max_attempts} attempts{C_CLR}")
                span.set_attribute("error", True)
                logfire.error("codex timeout", task_id=task_id)
                _submit_error(deps, "codex exec timed out")
                return
            except Exception as e:
                _tprint(task_id, f"{C_RED}codex exec error: {e}{C_CLR}")
                span.set_attribute("error", True)
                logfire.error("codex exec error", task_id=task_id, error=str(e))
                _submit_error(deps, f"codex exec error: {e}")
                return

        elapsed = time.time() - started

        # Log token usage
        if usage:
            inp = usage.get("input_tokens", 0)
            cached = usage.get("cached_input_tokens", 0)
            out = usage.get("output_tokens", 0)
            details = usage.get("output_tokens_details", {})
            reasoning = details.get("reasoning_tokens", 0)
            logfire.info(
                "codex token usage",
                task_id=task_id,
                input_tokens=inp,
                cached_input_tokens=cached,
                output_tokens=out,
                reasoning_tokens=reasoning,
                total_tokens=inp + out,
            )

        # 10. Use server-tracked refs (deterministic) instead of model-reported refs
        task_result.grounding_refs = []
        try:
            if os.path.exists(refs_path):
                with open(refs_path) as f:
                    task_result.grounding_refs = json.load(f)
                os.unlink(refs_path)
        except Exception:
            pass

        # 11. Submit answer to BitGN
        try:
            _retry(_submit_answer, deps, task_result)
        except Exception as exc:
            _tprint(task_id, f"{C_RED}Submit error: {exc}{C_CLR}")

        span.set_attribute("outcome", task_result.outcome)
        span.set_attribute("elapsed_s", elapsed)

    # 12. Display results
    status_color = C_GREEN if task_result.outcome == "OUTCOME_OK" else C_YELLOW
    _tprint(task_id, f"\n{status_color}=== Agent {task_result.outcome} ({elapsed:.1f}s) ==={C_CLR}")
    for s in task_result.completed_steps:
        _tprint(task_id, f"  - {s}")
    _tprint(task_id, f"\n{C_BLUE}ANSWER: {task_result.message}{C_CLR}")
    if task_result.grounding_refs:
        _tprint(task_id, f"  Refs: {', '.join(task_result.grounding_refs)}")
    if usage:
        inp = usage.get("input_tokens", 0)
        cached = usage.get("cached_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        details = usage.get("output_tokens_details", {})
        reasoning = details.get("reasoning_tokens", 0)
        _tprint(task_id, f"  Tokens: {inp + out} (in={inp} cached={cached} out={out} reasoning={reasoning})")


def _submit_error(deps: AgentDeps, message: str) -> None:
    """Submit an error result when codex exec fails."""
    task_result = TaskResult(
        message=message,
        outcome="OUTCOME_ERR_INTERNAL",
        grounding_refs=[],
        completed_steps=["codex exec failed"],
    )
    try:
        _submit_answer(deps, task_result)
    except Exception:
        pass
