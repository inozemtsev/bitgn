"""Module-level constants for the BitGN Codex agent."""

from __future__ import annotations

import os

CODEX_TIMEOUT_SEC = 600
RETRY_ATTEMPTS = 2
RETRY_BACKOFF_SEC = 3
VAULT_TREE_DEPTH = 2
DEFAULT_MODEL = "gpt-5.3-codex"

VAULT_TAGS = os.environ.get("VAULT_TAGS", "0") == "1"
CODEX_MULTI_STEP = os.environ.get("CODEX_MULTI_STEP", "off").lower() == "on"
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "high")
AUTO_DISCOVERY = os.environ.get("AUTO_DISCOVERY", "1") == "1"
COMPACT_PROMPT = os.environ.get("COMPACT_PROMPT", "1") == "1"
GROUNDING_REFS = os.environ.get("GROUNDING_REFS", "1") == "1"

ABLATION_FLAGS = (
    "MODEL_ID",
    "VAULT_TAGS",
    "CODEX_MULTI_STEP",
    "CODEX_REASONING_EFFORT",
    "AUTO_DISCOVERY",
    "COMPACT_PROMPT",
    "GROUNDING_REFS",
    "HINT",
)
