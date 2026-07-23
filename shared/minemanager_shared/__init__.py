"""MineManager shared protocol library.

Imported by both the hub and the agent so the wire contract has a single
definition and cannot drift between the two sides.
"""

from minemanager_shared.version import PROTOCOL_VERSION, __version__

__all__ = ["PROTOCOL_VERSION", "__version__"]
