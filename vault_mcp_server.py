"""MCP server exposing BitGN vault tools for native Codex integration.

Runs as a stdio MCP server outside the Codex sandbox, making gRPC calls
to the BitGN VM on behalf of the Codex agent.

Environment variables (set by codex_agent.py before `codex exec`):
    VAULT_HARNESS_URL  - gRPC endpoint for the BitGN VM
    VAULT_RUNTIME      - "pcm" or "mini"
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

import logfire
from opentelemetry import context as otel_context, propagate

from vault_utils import format_mini_outline, format_pcm_tree, wrap_content

logfire.configure(
    service_name="vault-mcp-server",
    send_to_logfire="if-token-present",
    scrubbing=False,
    distributed_tracing=True,
)

# Attach parent trace context propagated from codex_agent.py.
_traceparent = os.environ.get("TRACEPARENT", "")
if _traceparent:
    _parent_ctx = propagate.extract({"traceparent": _traceparent})
    otel_context.attach(_parent_ctx)

from mcp.server.fastmcp import FastMCP

# ── Logging (writes to stderr + optional log file) ───────────────────────

_LOG_FILE = os.environ.get("VAULT_MCP_LOG", "")
_log_handle = open(_LOG_FILE, "a") if _LOG_FILE else None


def _log(msg: str) -> None:
    line = f"[vault-mcp {time.strftime('%H:%M:%S')}] {msg}"
    if _log_handle:
        _log_handle.write(line + "\n")
        _log_handle.flush()
    print(line, file=sys.stderr)


# ── Grounding-ref tracking ───────────────────────────────────────────────

_REFS_FILE = os.environ.get("VAULT_MCP_REFS", "")
_tracked_refs: set[str] = set()


def _track_ref(path: str) -> None:
    """Track a file path as a grounding reference and flush immediately."""
    normalized = path.lstrip("/")
    if not normalized:
        return
    _tracked_refs.add(normalized)
    # Flush after every track — atexit won't fire if the process is killed.
    if _REFS_FILE:
        with open(_REFS_FILE, "w") as f:
            json.dump(sorted(_tracked_refs), f)


# ── Configuration from environment ───────────────────────────────────────

HARNESS_URL = os.environ.get("VAULT_HARNESS_URL", "")
RUNTIME = os.environ.get("VAULT_RUNTIME", "pcm")

if not HARNESS_URL:
    _log("FATAL: VAULT_HARNESS_URL not set")
    sys.exit(1)

_log(f"Starting: harness={HARNESS_URL} runtime={RUNTIME}")

# ── BitGN VM runtime imports ─────────────────────────────────────────────

if RUNTIME == "pcm":
    from bitgn.vm.pcm_connect import PcmRuntimeClientSync
    from bitgn.vm.pcm_pb2 import (
        ContextRequest,
        DeleteRequest,
        FindRequest,
        ListRequest,
        MkDirRequest,
        MoveRequest,
        ReadRequest,
        SearchRequest,
        TreeRequest,
        WriteRequest,
    )
    from google.protobuf.json_format import MessageToDict

    _vm: Any = PcmRuntimeClientSync(HARNESS_URL)
else:
    from bitgn.vm.mini_connect import MiniRuntimeClientSync
    from bitgn.vm.mini_pb2 import (
        DeleteRequest as MiniDeleteRequest,
        ListRequest as MiniListRequest,
        OutlineRequest,
        ReadRequest as MiniReadRequest,
        SearchRequest as MiniSearchRequest,
        WriteRequest as MiniWriteRequest,
    )

    _vm = MiniRuntimeClientSync(HARNESS_URL)


# ── Tree-path collection helpers (only used by vault_discover_policies) ──


def _collect_tree_paths_pcm(entry: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    name = f"{prefix}/{entry.name}" if prefix else entry.name
    children = list(entry.children)
    if not children:
        paths.append(name)
    else:
        for child in children:
            paths.extend(_collect_tree_paths_pcm(child, name))
    return paths


def _collect_tree_paths(tree_result: Any) -> list[str]:
    if RUNTIME == "pcm":
        paths: list[str] = []
        for child in tree_result.root.children:
            paths.extend(_collect_tree_paths_pcm(child))
        return paths
    return [f.path for f in tree_result.files]


# ── MCP Server ───────────────────────────────────────────────────────────

server = FastMCP("bitgn-vault")


@server.tool()
@logfire.instrument("vault_tree root={root} level={level}", record_return=True)
def vault_tree(root: str = "/", level: int = 2) -> str:
    """Show the directory tree of the vault.

    Args:
        root: Tree root path, empty or "/" means vault root.
        level: Max tree depth. 0 means unlimited.
    """
    _log(f"vault_tree(root={root!r}, level={level})")
    if RUNTIME == "pcm":
        result = _vm.tree(TreeRequest(root=root, level=level))
        out = format_pcm_tree(result)
    else:
        result = _vm.outline(OutlineRequest(path=root or "/"))
        out = format_mini_outline(result)
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
@logfire.instrument("vault_read path={path}", record_return=True)
def vault_read(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file from the vault.

    Args:
        path: File path to read.
        start_line: 1-based start line (0 = from beginning).
        end_line: 1-based end line (0 = to end).
    """
    _log(f"vault_read(path={path!r}, start={start_line}, end={end_line})")
    _track_ref(path)
    if RUNTIME == "pcm":
        result = _vm.read(ReadRequest(path=path, start_line=start_line, end_line=end_line))
    else:
        result = _vm.read(MiniReadRequest(path=path))
    _log(f"  -> {len(result.content)} chars")
    return wrap_content(path, result.content, start_line, end_line)


@server.tool()
@logfire.instrument("vault_write path={path}", record_return=True)
def vault_write(path: str, content: str, start_line: int = 0, end_line: int = 0) -> str:
    """Write or overwrite a file in the vault.

    Args:
        path: File path to write.
        content: Content to write.
        start_line: 1-based start line for partial write (0 = full overwrite).
        end_line: 1-based end line for partial write (0 = through last line).
    """
    _log(
        f"vault_write(path={path!r}, content_len={len(content)}, start={start_line}, end={end_line})"
    )
    content = content.rstrip("\n")
    if RUNTIME == "pcm":
        _vm.write(
            WriteRequest(path=path, content=content, start_line=start_line, end_line=end_line)
        )
    else:
        _vm.write(MiniWriteRequest(path=path, content=content))
    _log(f"  -> Written to {path}")
    return f"Written to {path}"


@server.tool()
@logfire.instrument("vault_delete path={path}", record_return=True)
def vault_delete(path: str) -> str:
    """Delete a file from the vault."""
    _log(f"vault_delete(path={path!r})")
    if RUNTIME == "pcm":
        _vm.delete(DeleteRequest(path=path))
    else:
        _vm.delete(MiniDeleteRequest(path=path))
    _log(f"  -> Deleted {path}")
    return f"Deleted {path}"


@server.tool()
@logfire.instrument("vault_list path={path}", record_return=True)
def vault_list(path: str = "/") -> str:
    """List directory contents."""
    _log(f"vault_list(path={path!r})")
    if RUNTIME == "pcm":
        result = _vm.list(ListRequest(name=path))
        if not result.entries:
            _log("  -> (empty directory)")
            return "(empty directory)"
        out = "\n".join(f"{e.name}/" if e.is_dir else e.name for e in result.entries)
    else:
        result = _vm.list(MiniListRequest(path=path))
        items = [f"{f}/" for f in result.folders] + list(result.files)
        out = "\n".join(items) if items else "(empty directory)"
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
@logfire.instrument("vault_search pattern={pattern} root={root}", record_return=True)
def vault_search(pattern: str, root: str = "/", limit: int = 10) -> str:
    """Search file contents with a regex pattern (like grep)."""
    _log(f"vault_search(pattern={pattern!r}, root={root!r}, limit={limit})")
    try:
        if RUNTIME == "pcm":
            result = _vm.search(SearchRequest(root=root, pattern=pattern, limit=limit))
            if not result.matches:
                _log("  -> (no matches)")
                return "(no matches)"
            for m in result.matches:
                _track_ref(m.path)
            out = "\n".join(f"{m.path}:{m.line}:{m.line_text}" for m in result.matches)
        else:
            result = _vm.search(MiniSearchRequest(path=root, pattern=pattern, count=limit))
            if not result.snippets:
                _log("  -> (no matches)")
                return "(no matches)"
            for s in result.snippets:
                _track_ref(s.file)
            out = "\n".join(f"{s.file}:{s.line}:{s.match}" for s in result.snippets)
        _log(f"  -> {out[:200]}")
        return out
    except Exception as exc:
        _log(f"  -> ERROR: {exc}")
        return f"(search error: {exc})"


@server.tool()
@logfire.instrument("vault_grep_count pattern={pattern} path={path}", record_return=True)
def vault_grep_count(pattern: str, path: str) -> str:
    """Count the number of lines matching a regex pattern in a file.

    Use this for ANY counting or aggregation task — it is exact and fast.
    Returns the count as a number.
    """
    _log(f"vault_grep_count(pattern={pattern!r}, path={path!r})")
    _track_ref(path)
    try:
        if RUNTIME == "pcm":
            content = _vm.read(ReadRequest(path=path)).content
        else:
            content = _vm.read(MiniReadRequest(path=path)).content
        count = sum(1 for line in content.split("\n") if re.search(pattern, line))
        _log(f"  -> {count} matches")
        return str(count)
    except Exception as exc:
        _log(f"  -> ERROR: {exc}")
        return f"(error: {exc})"


@server.tool()
@logfire.instrument("vault_find name={name} root={root}", record_return=True)
def vault_find(name: str, root: str = "/", kind: str = "all", limit: int = 10) -> str:
    """Find files or directories by name pattern."""
    _log(f"vault_find(name={name!r}, root={root!r}, kind={kind!r}, limit={limit})")
    if RUNTIME != "pcm":
        return "(find not available in sandbox -- use vault_search instead)"
    kind_map: dict[str, Any] = {"all": 0, "files": 1, "dirs": 2}
    result = _vm.find(FindRequest(root=root, name=name, type=kind_map.get(kind, 0), limit=limit))
    out = json.dumps(MessageToDict(result), indent=2)
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
@logfire.instrument("vault_context", record_return=True)
def vault_context() -> str:
    """Get the task context information from the runtime."""
    _log("vault_context()")
    if RUNTIME != "pcm":
        return "(context not available in sandbox)"
    result = _vm.context(ContextRequest())
    out = json.dumps(MessageToDict(result), indent=2)
    _log(f"  -> {out[:200]}")
    return out


@server.tool()
@logfire.instrument("vault_read_all_in_dir path={path}", record_return=True)
def vault_read_all_in_dir(path: str = "/") -> str:
    """Read ALL files in a directory and return their contents in one call."""
    _log(f"vault_read_all_in_dir(path={path!r})")
    if RUNTIME == "pcm":
        listing = _vm.list(ListRequest(name=path))
        files = [e.name for e in listing.entries if not e.is_dir]
    else:
        listing = _vm.list(MiniListRequest(path=path))
        files = list(listing.files)

    if not files:
        _log("  -> (no files)")
        return "(no files in directory)"

    parts = []
    base = path.rstrip("/")
    for fname in files:
        fpath = f"{base}/{fname}" if base and base != "/" else fname
        _track_ref(fpath)
        try:
            if RUNTIME == "pcm":
                content = _vm.read(ReadRequest(path=fpath)).content
            else:
                content = _vm.read(MiniReadRequest(path=fpath)).content
            parts.append(wrap_content(fpath, content))
        except Exception as exc:
            parts.append(
                f'<vault-file path="{fpath}" type="error" trust="untrusted" '
                f'format="plaintext" range="full">\n(error: {exc})\n</vault-file>'
            )

    out = "\n\n".join(parts)
    _log(f"  -> {len(files)} files, {len(out)} chars total")
    return out


@logfire.instrument("vault_discover_policies", record_return=True)
def vault_discover_policies() -> str:
    """Discover and return all policy/workflow documents in one call."""
    _log("vault_discover_policies()")
    parts: list[str] = []

    if RUNTIME == "pcm":
        tree_result = _vm.tree(TreeRequest(root="/", level=3))
        tree_text = format_pcm_tree(tree_result)
    else:
        tree_result = _vm.outline(OutlineRequest(path="/"))
        tree_text = format_mini_outline(tree_result)
    parts.append(f"## Vault structure\n```\n{tree_text}\n```")

    agents_content = ""
    try:
        if RUNTIME == "pcm":
            agents_content = _vm.read(ReadRequest(path="AGENTS.md")).content
        else:
            agents_content = _vm.read(MiniReadRequest(path="AGENTS.md")).content
    except Exception:
        pass

    path_refs: set[str] = set()
    for m in re.finditer(r"`([^`]*?/[^`]*?)`", agents_content):
        path_refs.add(m.group(1).strip().rstrip("/"))
    for m in re.finditer(r"\]\(([^)]*?/[^)]*?)\)", agents_content):
        path_refs.add(m.group(1).strip().rstrip("/"))
    for m in re.finditer(r"(?:^|\s)([\w._-]+/[\w._/-]+)", agents_content):
        path_refs.add(m.group(1).strip().rstrip("/"))

    all_paths = _collect_tree_paths(tree_result)

    policy_paths: list[str] = []
    for p in all_paths:
        basename = p.rsplit("/", 1)[-1] if "/" in p else p
        if "readme" in basename.lower():
            policy_paths.append(p)
            continue
        if p in path_refs:
            policy_paths.append(p)
            continue
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        if parent and parent in path_refs:
            policy_paths.append(p)

    seen: set[str] = set()
    for p in policy_paths:
        normalized = p.lstrip("/")
        if normalized in seen or normalized == "AGENTS.md":
            continue
        seen.add(normalized)
        try:
            if RUNTIME == "pcm":
                content = _vm.read(ReadRequest(path=normalized)).content
            else:
                content = _vm.read(MiniReadRequest(path=normalized)).content
            parts.append(wrap_content(normalized, content))
        except Exception as exc:
            parts.append(
                f'<vault-file path="{normalized}" type="error" trust="untrusted" '
                f'format="plaintext" range="full">\n(error: {exc})\n</vault-file>'
            )

    out = "\n\n".join(parts)
    _log(f"  -> {len(seen)} policy files, {len(out)} chars total")
    return out


@server.tool()
@logfire.instrument("vault_mkdir path={path}", record_return=True)
def vault_mkdir(path: str) -> str:
    """Create a directory in the vault."""
    _log(f"vault_mkdir(path={path!r})")
    if RUNTIME != "pcm":
        return "(mkdir not available in sandbox)"
    _vm.mk_dir(MkDirRequest(path=path))
    _log(f"  -> Created directory {path}")
    return f"Created directory {path}"


@server.tool()
@logfire.instrument("vault_move from={from_name} to={to_name}", record_return=True)
def vault_move(from_name: str, to_name: str) -> str:
    """Move or rename a file in the vault."""
    _log(f"vault_move(from={from_name!r}, to={to_name!r})")
    if RUNTIME != "pcm":
        return "(move not available in sandbox)"
    _vm.move(MoveRequest(from_name=from_name, to_name=to_name))
    return f"Moved {from_name} -> {to_name}"


if __name__ == "__main__":
    server.run()
