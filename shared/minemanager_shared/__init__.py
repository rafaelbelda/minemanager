"""Code the hub and the agent both need.

Chiefly the wire contract (:mod:`minemanager_shared.protocol`), so it has a
single definition and cannot drift between the two sides. Also the ``MM_*``
environment parsing (:mod:`minemanager_shared.env`), for the same reason: both
packages are configured that way and are always installed together.
"""

from minemanager_shared.version import PROTOCOL_VERSION, __version__

__all__ = ["PROTOCOL_VERSION", "__version__"]
