"""Version constants for the MineManager shared library.

``PROTOCOL_VERSION`` is bumped whenever the wire contract in ``protocol.py``
changes in a way that is not backward compatible. Hub and agent exchange it
during the handshake and refuse to talk across a major mismatch.
"""

__version__ = "0.1.1"

# Wire-protocol version. Bump the major on breaking envelope/frame changes.
#
# 2: InstanceSpec.auto_restart became restart_policy, a three-valued enum.
#    Breaking in the silent direction — an agent still reading the old field
#    would not error, it would fall back to the default and cheerfully restart
#    servers the operator had asked it to leave alone. The handshake's major
#    check turns that into a refusal with a readable reason, so hub and agent
#    must be upgraded together.
PROTOCOL_VERSION = "2"
