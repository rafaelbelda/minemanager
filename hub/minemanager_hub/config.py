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


#: SQLite DB + generated key file. Matches the systemd unit and deploy docs; the
#: old ``$HOME/.local/share`` default disagreed with both, and a hub pointed at a
#: different directory silently opens an empty database.
DEFAULT_DATA_DIR = Path("/var/lib/minemanager")


def _default_data_dir() -> Path:
    """Where the hub keeps its SQLite DB and any local state."""
    env = os.environ.get("MM_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


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

    # Hostnames this hub answers to. With no app-layer auth, an unexpected Host
    # means a DNS-rebinding attempt, which reaches the hub's port directly and so
    # bypasses the reverse proxy (and Authelia). Defaults to loopback only, which
    # is correct for the default MM_HOST=127.0.0.1 bind; any real deployment must
    # list its own name, e.g. MM_ALLOWED_HOSTS=mm.example.com. "*" disables.
    allowed_hosts: set[str] = field(
        default_factory=lambda: {
            h.strip() for h in os.environ.get(
                "MM_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,[::1]"
            ).split(",") if h.strip()
        }
    )

    # Let clients that send no Origin header (curl, scripts, CI) make
    # state-changing requests. Off by default: a missing Origin means "not a
    # browser", and allowing it unconditionally would reopen the CSRF hole that
    # checking Origin closes.
    allow_api_clients: bool = field(
        default_factory=lambda: os.environ.get("MM_ALLOW_API_CLIENTS", "").strip().lower()
        in {"1", "true", "yes", "on"}
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
        """Create the data dir, or exit saying exactly what to do about it."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:   # ValueError: malformed path
            raise SystemExit(
                f"cannot create the hub data dir {self.data_dir} ({exc}).\n"
                f"Set MM_DATA_DIR to a writable path: /var/lib/minemanager owned by the "
                f"hub's user in production, or e.g. ./_devdata for a local run."
            ) from None


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
