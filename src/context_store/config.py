"""Path configuration from environment variables.

`~` is expanded in code — environment values are not shell-expanded.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB = "~/.local/share/opencode/context.db"
DEFAULT_OPENCODE_DB = "~/.local/share/opencode/opencode.db"


def context_db_path() -> Path:
    return Path(os.environ.get("CONTEXT_DB", DEFAULT_DB)).expanduser()


def opencode_db_path() -> Path:
    return Path(os.environ.get("OPENCODE_DB", DEFAULT_OPENCODE_DB)).expanduser()
