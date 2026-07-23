"""MineManager hub — the web control plane.

Serves the web UI, holds declared/desired state in SQLite, terminates the
persistent agent WebSockets, and fans console/log streams out to UI clients.
"""

__version__ = "0.1.0"
