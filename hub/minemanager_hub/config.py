"""Hub configuration, driven by environment variables.

The hub runs behind Auth Service + WireGuard, so it does not configure its own user
authentication here. What it *does* own is machine-to-machine trust: agent
credentials, and the Host/Origin guards.

Parsing lives in :mod:`minemanager_shared.env`; this module only names the
variables, their defaults, and why each one exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from minemanager_shared.env import env_bool, env_csv, env_int, env_opt, env_path, env_str

#: SQLite DB location. Matches the systemd unit; a hub pointed at a different
#: directory silently opens an empty database.
DEFAULT_DATA_DIR = Path("/var/lib/minemanager")

#: Served same-origin by the hub. Defaults to ``<repo>/web`` so a source checkout
#: or editable install just works; set ``MM_WEB_DIR`` when deployed elsewhere.
DEFAULT_WEB_DIR = Path(__file__).resolve().parents[2] / "web"

#: Loopback only, which is correct for the default MM_HOST bind. Any real
#: deployment must list the name clients use.
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1,::1,[::1]"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: env_path("MM_DATA_DIR", DEFAULT_DATA_DIR))
    web_dir: Path = field(default_factory=lambda: env_path("MM_WEB_DIR", DEFAULT_WEB_DIR))
    host: str = field(default_factory=lambda: env_str("MM_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: env_int("MM_PORT", 8730))

    # How long an enrollment token is valid once minted (seconds).
    enrollment_ttl_s: int = field(default_factory=lambda: env_int("MM_ENROLLMENT_TTL", 900))

    autostart: bool = field(default_factory=lambda: env_bool("MM_AUTOSTART", True))

    autostart_stagger_s: int = field(default_factory=lambda: env_int("MM_AUTOSTART_STAGGER", 5))

    # Allowed CORS origins for the web UI during development (e.g. a dev server
    # on another port). In production the UI is same-origin, so this stays empty.
    cors_origins: list[str] = field(default_factory=lambda: env_csv("MM_CORS_ORIGINS"))

    editor_warn_bytes: int = field(
        default_factory=lambda: env_int("MM_EDITOR_WARN_BYTES", 2_000_000)
    )
    editor_max_bytes: int = field(
        default_factory=lambda: env_int("MM_EDITOR_MAX_BYTES", 5_000_000)
    )
    transfer_cap_bytes: int = field(
        default_factory=lambda: env_int("MM_TRANSFER_CAP_BYTES", 8 * 1024 * 1024)
    )

    # Hostnames this hub answers to. With no app-layer auth an unexpected Host
    # means a DNS-rebinding attempt, which reaches the hub's port directly and so
    # bypasses the reverse proxy. "*" disables the check.
    allowed_hosts: set[str] = field(
        default_factory=lambda: set(env_csv("MM_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS))
    )

    # Let clients that send no Origin header (curl, scripts, CI) make
    # state-changing requests. Off by default: a missing Origin means "not a
    # browser", and allowing it would reopen the CSRF hole that checking closes.
    allow_api_clients: bool = field(default_factory=lambda: env_bool("MM_ALLOW_API_CLIENTS"))

    # Serve /docs, /redoc and /openapi.json. Off by default: with no app-layer
    # auth they hand anyone who reaches the hub a map of the attack surface.
    enable_docs: bool = field(default_factory=lambda: env_bool("MM_ENABLE_DOCS"))

    # Sent by the reverse proxy after it authenticates a request. Without it,
    # anything that can reach MM_HOST bypasses the proxy's auth.
    proxy_secret: str | None = field(default_factory=lambda: env_opt("MM_PROXY_SECRET"))

    # Authenticated-user header (e.g. Remote-User). Required and logged when
    # set; trusted only alongside the proxy secret.
    proxy_user_header: str | None = field(
        default_factory=lambda: env_opt("MM_PROXY_USER_HEADER")
    )

    # Who the user is, beyond their login name. Set by the same proxy on the
    # same request, so they are believed on the same terms as the user header
    # and only alongside it. The defaults are Authelia's forward-auth names;
    # override one when a proxy spells it differently. A header the proxy does
    # not send reads as absent, so nothing needs switching off.
    proxy_name_header: str = field(
        default_factory=lambda: env_str("MM_PROXY_NAME_HEADER", "Remote-Name")
    )
    proxy_groups_header: str = field(
        default_factory=lambda: env_str("MM_PROXY_GROUPS_HEADER", "Remote-Groups")
    )

    # Where the proxy's own sign-out lives (Authelia: https://<portal>/logout).
    # The hub cannot end a session it never issued, so the menu item is only
    # offered when there is somewhere to send the browser.
    proxy_logout_url: str | None = field(
        default_factory=lambda: env_opt("MM_PROXY_LOGOUT_URL")
    )

    # Allow a non-loopback bind without a proxy secret.
    allow_unauthenticated: bool = field(
        default_factory=lambda: env_bool("MM_ALLOW_UNAUTHENTICATED")
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "minemanager.db"

    @property
    def proxy_profile_headers(self) -> dict[str, str]:
        """Attribute name -> header, for the ones actually configured."""
        pairs = (("name", self.proxy_name_header),
                 ("groups", self.proxy_groups_header))
        return {attr: header.strip() for attr, header in pairs if header and header.strip()}

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

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
