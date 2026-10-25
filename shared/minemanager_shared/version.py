"""Version constants for the MineManager shared library.

``PROTOCOL_VERSION`` is bumped whenever the wire contract in ``protocol.py``
changes in a way that is not backward compatible. Hub and agent exchange it
during the handshake and refuse to talk across a major mismatch.
"""

__version__ = "0.1.1"

# Wire-protocol version. Bump the major on breaking envelope/frame changes.
#
# 2: InstanceSpec.auto_restart became restart_policy, a three-valued enum.
#    Breaks silently if unchecked (an old agent falls back to the default), so
#    the handshake's major check refuses the mismatch instead.
PROTOCOL_VERSION = "2"
