"""Prompt constants loaded from sibling Markdown files."""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent


def _load(name: str) -> str:
    return (_HERE / name).read_text()


INSTRUCTIONS: str = _load("instructions.md")
MULTI_STEP_PROTOCOL: str = _load("multi_step.md")
CODEX_PREAMBLE: str = _load("codex_preamble.md")

__all__ = ["INSTRUCTIONS", "MULTI_STEP_PROTOCOL", "CODEX_PREAMBLE"]
