"""Agent configuration and persisted identity.

On first run the agent enrolls with a one-time token (``MM_ENROLL_TOKEN``) and
persists the node id + long-lived credential the hub issues to
``<data_dir>/identity.json`` (0600). On subsequent runs it reconnects with that
credential and the enrollment token is no longer needed.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    env = os.environ.get("MM_AGENT_DATA_DIR")
    if env:
        return Path(env)
    return Path(os.environ.get("HOME", ".")) / ".local" / "share" / "minemanager-agent"


@dataclass
class AgentConfig:
    hub_url: str = field(
        default_factory=lambda: os.environ.get("MM_HUB_URL", "ws://127.0.0.1:8730/ws/agent")
    )
    data_dir: Path = field(default_factory=_default_data_dir)
    enroll_token: str | None = field(default_factory=lambda: os.environ.get("MM_ENROLL_TOKEN"))
    # tmux session name prefix for managed instances.
    session_prefix: str = field(
        default_factory=lambda: os.environ.get("MM_SESSION_PREFIX", "mm")
    )
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    heartbeat_s: float = 15.0

    @property
    def http_base(self) -> str:
        """HTTP(S) origin of the hub, derived from the WS hub URL — used for the
        outbound large-file transfer connections."""
        u = self.hub_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        return u.rsplit("/ws/", 1)[0] if "/ws/" in u else u.rstrip("/")

    @property
    def identity_file(self) -> Path:
        return self.data_dir / "identity.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- persisted identity --------------------------------------------------
    def load_identity(self) -> tuple[str | None, str | None]:
        """Return ``(node_id, credential)`` from disk, or ``(None, None)``."""
        if not self.identity_file.exists():
            return None, None
        data = json.loads(self.identity_file.read_text())
        return data.get("node_id"), data.get("credential")

    def save_identity(self, node_id: str, credential: str) -> None:
        self.ensure_dirs()
        self.identity_file.write_text(json.dumps({"node_id": node_id, "credential": credential}))
        try:
            os.chmod(self.identity_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
