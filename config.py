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
