"""Environment-variable parsing, shared by the hub and the agent.

Both packages are configured entirely through ``MM_*`` variables and are always
installed together, so the parsing rules — what counts as true, what a malformed
integer does — belong in one place rather than being re-derived in each. Before
this, the truthy set was written out three times across two files and drifting it
would have been silent.

A malformed value exits with a readable message rather than raising or falling
back to a default. These are read once at startup, where a wrong value is better
caught loudly than carried into a running hub.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Values accepted as true. Anything else (including empty) is false.
_TRUE = {"1", "true", "yes", "on"}


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_str(name: str, default: str) -> str:
    return _raw(name) or default


def env_opt(name: str) -> str | None:
    """An optional value with no default (an unset token, a credential)."""
    return _raw(name)


def env_bool(name: str, default: bool = False) -> bool:
    raw = _raw(name)
    return default if raw is None else raw.lower() in _TRUE


def env_int(name: str, default: int) -> int:
    raw = _raw(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


def env_path(name: str, default: Path | str) -> Path:
    raw = _raw(name)
    return Path(raw) if raw else Path(default)


def env_csv(name: str, default: str = "") -> list[str]:
    """Comma-separated list, blank entries dropped."""
    return [part.strip() for part in env_str(name, default).split(",") if part.strip()]
