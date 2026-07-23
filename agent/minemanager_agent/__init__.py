"""MineManager agent — the per-node daemon.

Runs as a non-root user, dials out to the hub over a persistent WebSocket, and
supervises Minecraft servers/proxies in their own tmux sessions. It is the only
systemd-managed daemon on a node; it supervises the servers itself.
"""

__version__ = "0.1.0"
