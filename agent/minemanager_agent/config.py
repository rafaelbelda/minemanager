"""Agent configuration and persisted identity.

On first run the agent enrolls with a one-time token (``MM_ENROLL_TOKEN``) and
persists the node id + long-lived credential the hub issues to
``<data_dir>/identity.json`` (0600). On subsequent runs it reconnects with that
credential; the enrollment token is no longer needed, though a still-valid one
takes precedence if supplied (the hub arbitrates — see its ``_authenticate``).

``<data_dir>/runtime.json`` records the tmux session prefix + socket this agent
last ran with. Those two form a running server's identity, so
:meth:`AgentConfig.pin_runtime_identity` refuses to apply a change to them while
it would orphan live sessions.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from minemanager_agent import tmux

log = logging.getLogger("minemanager.agent")


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
    # tmux session name prefix for managed instances. Together with tmux_socket
    # this forms the *identity* of a managed server; both are pinned by
    # pin_runtime_identity() rather than being taken from the env unconditionally.
    session_prefix: str = field(
        default_factory=lambda: os.environ.get("MM_SESSION_PREFIX", "mm")
    )
    tmux_socket: str = field(
        default_factory=lambda: os.environ.get("MM_TMUX_SOCKET", tmux.DEFAULT_SOCKET)
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

    @property
    def pty_dir(self) -> Path:
        """Where raw pane output is mirrored, one file per instance.

        Kept in the agent's data dir rather than under the instance root so it
        stays out of the operator's file browser and out of anything that gets
        backed up with the world.
        """
        return self.data_dir / "pty"

    @property
    def runtime_file(self) -> Path:
        """Records the session prefix + tmux socket this agent last ran with."""
        return self.data_dir / "runtime.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- pinned runtime identity (session prefix + tmux socket) --------------
    def _load_runtime(self) -> dict[str, str] | None:
        if not self.runtime_file.exists():
            return None
        try:
            data = json.loads(self.runtime_file.read_text())
            return {"session_prefix": data["session_prefix"], "tmux_socket": data["tmux_socket"]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_runtime(self, values: dict[str, str]) -> None:
        self.ensure_dirs()
        self.runtime_file.write_text(json.dumps(values, indent=2))

    async def pin_runtime_identity(self) -> None:
        """Pin the session prefix + tmux socket to what this agent last used.

        Both are part of a *server's* identity: sessions live at
        ``tmux -L <socket>`` under the name ``<prefix>-<instance_id>``. Changing
        either while servers are running makes every existing session invisible —
        the agent reports them stopped, tries to start duplicates (which then
        crash-loop against ``session.lock``), and never stops them on shutdown,
        leaving orphans that survive reboot with no management path.

        So a change is **not applied while it would orphan anything**: the
        recorded values win and the agent keeps control of the servers it already
        supervises. With nothing running, the new values are adopted and
        recorded. Must run before the supervisor is constructed, since it takes
        the prefix at build time.
        """
        configured = {"session_prefix": self.session_prefix, "tmux_socket": self.tmux_socket}
        recorded = self._load_runtime()

        if recorded is None:                       # first run — record and go
            self._save_runtime(configured)
            tmux.use_socket(self.tmux_socket)
            return
        if recorded == configured:                 # unchanged — the normal path
            tmux.use_socket(self.tmux_socket)
            return

        # Changed. Look for live sessions under the *recorded* identity, which is
        # where anything this agent started actually lives.
        tmux.use_socket(recorded["tmux_socket"])
        try:
            live = await tmux.list_sessions(recorded["session_prefix"])
        except Exception:                          # noqa: BLE001 - diagnostic only
            live = []

        if live:
            log.error(
                "IGNORING a tmux identity change while %d server session(s) are running. "
                "Keeping prefix=%r socket=%r (recorded); configured prefix=%r socket=%r is NOT "
                "being applied, because those sessions would become unmanageable and would not "
                "be stopped on shutdown. Stop the servers, then restart the agent to apply it. "
                "Running: %s",
                len(live), recorded["session_prefix"], recorded["tmux_socket"],
                configured["session_prefix"], configured["tmux_socket"], ", ".join(live),
            )
            self.session_prefix = recorded["session_prefix"]
            self.tmux_socket = recorded["tmux_socket"]
            return

        log.warning(
            "tmux identity changed (prefix %r->%r, socket %r->%r) and nothing was running - "
            "adopting the new values",
            recorded["session_prefix"], configured["session_prefix"],
            recorded["tmux_socket"], configured["tmux_socket"],
        )
        tmux.use_socket(self.tmux_socket)
        self._save_runtime(configured)

    def log_resolved(self) -> None:
        """Log the configuration actually in effect.

        Every silent-default failure we have hit (identity written to an
        unexpected data dir, an ignored enrollment token, a changed tmux socket
        orphaning servers) was hard to diagnose only because nothing ever stated
        which values were resolved. Secrets are never logged — only their source.
        """
        node_id, _ = self.load_identity()
        log.info(
            "config: hub_url=%s data_dir=%s identity=%s session_prefix=%s tmux_socket=%s",
            self.hub_url,
            self.data_dir,
            f"node {node_id} ({self.identity_file})" if node_id else "none — will enroll",
            self.session_prefix,
            tmux.socket_name(),
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
        self.ensure_dirs()
        self.identity_file.write_text(json.dumps({"node_id": node_id, "credential": credential}))
        try:
            os.chmod(self.identity_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
