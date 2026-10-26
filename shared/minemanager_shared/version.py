"""Version constants for the MineManager shared library.

``__version__`` is the release, dated ``YY.MM.DD``.

``PROTOCOL_VERSION`` is the wire contract in ``protocol.py``. Hub and agent
exchange it during the handshake and refuse to talk across a major mismatch,
which is what catches a half-finished upgrade: the two sides are installed from
the same source and are expected to move together.
"""

__version__ = "26.10.26"

PROTOCOL_VERSION = "1"
