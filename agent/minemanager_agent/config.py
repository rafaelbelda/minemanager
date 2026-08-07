"""Agent configuration and persisted identity."""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("minemanager.agent")


def _write_private(path: Path, text: str) -> None:
    """Write a file only the owner may read, with no world-readable window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)          # atomic; keeps the 0600 mode


#: Where a packaged agent keeps identity.json and runtime.json.
DEFAULT_DATA_DIR = Path("/var/lib/minemanager-agent")


def _default_data_dir() -> Path:
    env = os.environ.get("MM_AGENT_DATA_DIR")
    return Path(env) if env else DEFAULT_DATA_DIR


@dataclass
class AgentConfig:
    hub_url: str = field(
        default_factory=lambda: os.environ.get("MM_HUB_URL", "ws://127.0.0.1:8730/ws/agent")
    )
    data_dir: Path = field(default_factory=_default_data_dir)
    enroll_token: str | None = field(default_factory=lambda: os.environ.get("MM_ENROLL_TOKEN"))
    # Permit a plaintext ws:// hub URL to a non-loopback host. Off by default.
    allow_insecure: bool = field(
        default_factory=lambda: os.environ.get("MM_ALLOW_INSECURE", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    heartbeat_s: float = 15.0

    @property
    def http_base(self) -> str:
        """HTTP(S) origin of the hub, derived from the WS URL. Used by the
        agent's outbound large-file transfer connections."""
        u = self.hub_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        return u.rsplit("/ws/", 1)[0] if "/ws/" in u else u.rstrip("/")

    @property
    def identity_file(self) -> Path:
        return self.data_dir / "identity.json"

    @property
    def pty_dir(self) -> Path:
        return self.data_dir / "pty"

    def ensure_dirs(self) -> None:
        """Create the data dir, or exit saying what to do about it."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:   # ValueError: malformed path
            raise SystemExit(
                f"cannot create the agent data dir {self.data_dir} ({exc}).\n"
                f"Set MM_AGENT_DATA_DIR to a writable path: /var/lib/minemanager-agent "
                f"owned by the agent's user in production, or e.g. ./_agentdata for a "
                f"local run."
            ) from None

    # -- transport security --------------------------------------------------
    @property
    def hub_is_loopback(self) -> bool:
        host = urlparse(self.hub_url).hostname or ""
        return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")

    @property
    def hub_is_encrypted(self) -> bool:
        return urlparse(self.hub_url).scheme == "wss"

    def check_transport_security(self) -> None:
        """Refuse a plaintext hub URL unless the operator opted in. Loopback is
        exempt."""
        if self.hub_is_encrypted:
            if self.allow_insecure:
                log.warning(
                    "MM_ALLOW_INSECURE is set but the hub URL is already wss:// - the flag has "
                    "no effect here and should be removed so it cannot mask a later downgrade"
                )
            return

        if self.hub_is_loopback:
            log.info("hub URL is plaintext ws:// but loopback-only; traffic never leaves this host")
            return

        if self.allow_insecure:
            log.warning(
                "INSECURE TRANSPORT: talking to %s over plaintext ws://. This node's long-lived "
                "credential crosses the network in clear on every transfer. Permitted only "
                "because MM_ALLOW_INSECURE is set - acceptable inside WireGuard, not otherwise. "
                "Switch to wss:// when you can.",
                self.hub_url,
            )
            return

        raise SystemExit(
            f"refusing to start: MM_HUB_URL={self.hub_url} is plaintext ws:// to a non-loopback "
            f"host, so this node's long-lived credential would cross the network in clear.\n"
            f"  - use wss:// (recommended), or\n"
            f"  - set MM_ALLOW_INSECURE=1 to accept the risk (e.g. the hop is already inside "
            f"WireGuard)."
        )

    def log_resolved(self) -> None:
        """Log the configuration in effect."""
        node_id, _ = self.load_identity()
        log.info(
            "loaded config: hub_url=%s | data_dir=%s | identity=%s",
            self.hub_url,
            self.data_dir,
            f"node {node_id} ({self.identity_file})" if node_id else "none — will enroll",
        )
        if self.enroll_token and node_id:
            log.warning(
                "MM_ENROLL_TOKEN is set and %s already holds an identity for node %s - "
                "a still-valid token wins and re-enrolls; an already-used one falls back "
                "to the stored credential",
                self.identity_file,
                node_id,
            )

    # -- persisted identity --------------------------------------------------
    def load_identity(self) -> tuple[str | None, str | None]:
        """Return ``(node_id, credential)`` from disk, or ``(None, None)``."""
        if not self.identity_file.exists():
            return None, None
        data = json.loads(self.identity_file.read_text())
        return data.get("node_id"), data.get("credential")

    def save_identity(self, node_id: str, credential: str) -> None:
        """Persist the node's long-lived credential, 0600 from the moment it exists."""
        self.ensure_dirs()
        _write_private(
            self.identity_file,
            json.dumps({"node_id": node_id, "credential": credential}),
        )
