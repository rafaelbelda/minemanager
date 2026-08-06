"""Hub configuration, driven by environment variables.

The hub runs behind Authelia + WireGuard, so it does not configure its own user
authentication here. What it *does* own is machine-to-machine trust (agent
credentials) and secret encryption — hence the required ``MM_SECRET_KEY``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    # Read an integer env var, failing with a readable message.
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


def _default_web_dir() -> Path:
    """Where the static web UI lives (served same-origin by the hub).

    Defaults to ``<repo>/web`` so a source checkout or editable install just
    works; set ``MM_WEB_DIR`` when the UI is deployed elsewhere.
    """
    env = os.environ.get("MM_WEB_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "web"


def _default_data_dir() -> Path:
    """Where the hub keeps its SQLite DB and any local state."""
    env = os.environ.get("MM_DATA_DIR")
    if env:
        return Path(env)
    # Linux target default; overridable for dev on any OS.
    return Path(os.environ.get("HOME", ".")) / ".local" / "share" / "minemanager"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=_default_data_dir)
    web_dir: Path = field(default_factory=_default_web_dir)
    host: str = field(default_factory=lambda: os.environ.get("MM_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("MM_PORT", 8730))

    # Secret-vault key. In production this comes from the environment (systemd
    # EnvironmentFile / a secret manager), never from the DB. Dev falls back to
    # a file under data_dir so local runs work without extra setup.
    secret_key: str | None = field(default_factory=lambda: os.environ.get("MM_SECRET_KEY"))

    # How long an enrollment token is valid once minted (seconds).
    enrollment_ttl_s: int = field(
        default_factory=lambda: _env_int("MM_ENROLLMENT_TTL", 900)
    )

    # Comma-separated allowed CORS origins for the web UI during development
    # (e.g. a Vite dev server on another port). In production the UI is served
    # same-origin behind the reverse proxy, so this can stay empty.
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.environ.get("MM_CORS_ORIGINS", "").split(",") if o.strip()
        ]
    )

    # File-explorer thresholds (served to the UI via /api/config).
    # Above the *warn* size the editor asks before opening; above the *max* size
    # it won't open a file as text at all. The *transfer cap* bounds the simple
    # (non-streaming) upload/download path — larger transfers use the streaming
    # feature. All in bytes.
    editor_warn_bytes: int = field(
        default_factory=lambda: _env_int("MM_EDITOR_WARN_BYTES", 2_000_000)
    )
    editor_max_bytes: int = field(
        default_factory=lambda: _env_int("MM_EDITOR_MAX_BYTES", 5_000_000)
    )
    transfer_cap_bytes: int = field(
        default_factory=lambda: _env_int("MM_TRANSFER_CAP_BYTES", 8 * 1024 * 1024)
    )

    # Serve /docs, /redoc and /openapi.json. Off by default: there is no
    # app-layer auth, so they hand anyone who reaches the hub a complete map of
    # the attack surface, parameter names included. Set MM_ENABLE_DOCS=1 in dev.
    enable_docs: bool = field(
        default_factory=lambda: os.environ.get("MM_ENABLE_DOCS", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "minemanager.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def secret_key_file(self) -> Path:
        return self.data_dir / "secret.key"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
