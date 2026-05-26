"""Expand ${VAR} and ${VAR:-default} in YAML text before parsing."""

from __future__ import annotations

import os
import re

_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def expand_env_tokens(text: str) -> str:
    """Replace env placeholders in a string; missing vars raise ValueError."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        default = match.group(2)
        val = os.environ.get(key)
        if val is not None:
            return val
        if default is not None:
            return default
        raise ValueError(
            f"Environment variable {key!r} is not set (used in YAML job); "
            f"set it or use ${{{key}:-default}} syntax."
        )

    return _PATTERN.sub(repl, text)
