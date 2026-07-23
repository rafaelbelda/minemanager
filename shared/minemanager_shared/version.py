"""Version constants for the MineManager shared library.

``PROTOCOL_VERSION`` is bumped whenever the wire contract in ``protocol.py``
changes in a way that is not backward compatible. Hub and agent exchange it
during the handshake and refuse to talk across a major mismatch.
"""

__version__ = "0.1.0"

# Wire-protocol version. Bump the major on breaking envelope/frame changes.
PROTOCOL_VERSION = "1"
