"""Shared vault helpers: file metadata, tree formatting, colors, thread-safe print.

Used by both `codex_agent.py` and `vault_mcp_server.py`.
"""

from __future__ import annotations

import threading
from typing import Literal, NamedTuple

from google.protobuf.message import Message

FileType = Literal[
    "policy",
    "workflow-doc",
    "inbox-message",
    "outbox-record",
    "contact-record",
    "account-record",
    "invoice",
    "channel-config",
    "template",
    "note",
    "memory",
    "file",
]
Trust = Literal["trusted", "untrusted"]
Format = Literal["markdown", "json", "yaml", "csv", "plaintext", "toml", "xml"]


class FileMeta(NamedTuple):
    type: FileType
    trust: Trust
    format: Format


_EXT_FORMAT_MAP: dict[str, Format] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".csv": "csv",
    ".txt": "plaintext",
    ".log": "plaintext",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "markdown",
    ".ics": "plaintext",
}


def infer_file_meta(path: str) -> FileMeta:
    """Infer (type, trust, format) from a vault file path."""
    normalized = path.lstrip("/")
    parts = normalized.split("/")
    basename = parts[-1] if parts else ""

    ext = ""
    if "." in basename:
        ext = "." + basename.rsplit(".", 1)[-1].lower()
    fmt: Format = _EXT_FORMAT_MAP.get(ext, "plaintext")

    if len(parts) == 1:
        if basename == "AGENTS.md" or basename.lower() == "readme.md":
            return FileMeta("policy", "trusted", fmt)

    if basename.lower().startswith("readme"):
        return FileMeta("workflow-doc", "untrusted", fmt)

    top_dir = parts[0].lower() if parts else ""
    if top_dir in ("inbox", "00_inbox"):
        return FileMeta("inbox-message", "untrusted", fmt)
    if top_dir == "outbox":
        return FileMeta("outbox-record", "untrusted", fmt)
    if top_dir == "contacts":
        return FileMeta("contact-record", "untrusted", fmt)
    if top_dir == "accounts":
        return FileMeta("account-record", "untrusted", fmt)
    if top_dir in ("my-invoices", "invoices"):
        return FileMeta("invoice", "untrusted", fmt)
    if top_dir == "docs":
        if len(parts) > 1 and parts[1].lower() == "channels":
            return FileMeta("channel-config", "untrusted", fmt)
        return FileMeta("workflow-doc", "untrusted", fmt)
    if top_dir == "templates" or basename.startswith("_"):
        return FileMeta("template", "untrusted", fmt)
    if "notes" in top_dir:
        return FileMeta("note", "untrusted", fmt)
    if "memory" in top_dir:
        return FileMeta("memory", "untrusted", fmt)
    return FileMeta("file", "untrusted", fmt)


def wrap_content(path: str, content: str, start_line: int = 0, end_line: int = 0) -> str:
    """Wrap file content with <vault-file> XML tags."""
    meta = infer_file_meta(path)
    if start_line > 0 or end_line > 0:
        range_str = f"lines {start_line or 1}-{end_line or 'end'}"
    else:
        range_str = "full"
    return (
        f'<vault-file path="{path}" type="{meta.type}" trust="{meta.trust}" '
        f'format="{meta.format}" range="{range_str}">\n'
        f"{content}\n"
        f"</vault-file>"
    )


def format_tree_entry(entry: Message, prefix: str = "", is_last: bool = True) -> list[str]:
    branch = "└── " if is_last else "├── "
    lines = [f"{prefix}{branch}{entry.name}"]  # type: ignore[attr-defined]
    child_prefix = f"{prefix}{'    ' if is_last else '│   '}"
    children = list(entry.children)  # type: ignore[attr-defined]
    for idx, child in enumerate(children):
        lines.extend(
            format_tree_entry(child, prefix=child_prefix, is_last=idx == len(children) - 1)
        )
    return lines


def format_pcm_tree(result: Message) -> str:
    """Format a PCM TreeResponse (has .root with .name and .children)."""
    root = result.root  # type: ignore[attr-defined]
    if not root.name:
        return "."
    lines = [root.name]
    children = list(root.children)
    for idx, child in enumerate(children):
        lines.extend(format_tree_entry(child, is_last=idx == len(children) - 1))
    return "\n".join(lines)


def format_mini_outline(result: Message) -> str:
    """Format a Mini OutlineResponse (has .path, .files[], .folders[])."""
    path = result.path  # type: ignore[attr-defined]
    lines = [path]
    items: list[str] = []
    for folder in result.folders:  # type: ignore[attr-defined]
        items.append(f"{folder}/")
    for f in result.files:  # type: ignore[attr-defined]
        items.append(f.path)
    for idx, item in enumerate(items):
        branch = "└── " if idx == len(items) - 1 else "├── "
        lines.append(f"{branch}{item}")
    return "\n".join(lines) if items else path or "(empty)"


# ── Terminal colors ───────────────────────────────────────────────────────

C_RED = "\x1b[31m"
C_GREEN = "\x1b[32m"
C_BLUE = "\x1b[34m"
C_YELLOW = "\x1b[33m"
C_CYAN = "\x1b[36m"
C_CLR = "\x1b[0m"


_print_lock = threading.Lock()


def tprint(task_id: str, msg: str) -> None:
    """Thread-safe print with optional task prefix."""
    with _print_lock:
        if task_id:
            for line in msg.split("\n"):
                print(f"[{task_id}] {line}")
        else:
            print(msg)
