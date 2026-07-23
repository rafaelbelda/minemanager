"""Minimal async Source RCON client — the secondary command channel.

Per the plan, RCON is a fallback, not the primary console (that's the tmux pty).
It's handy for one-shot commands against servers that expose RCON. Velocity does
not implement RCON, so this only applies to Paper/Vanilla.
"""

from __future__ import annotations

import asyncio
import struct

_SERVERDATA_AUTH = 3
_SERVERDATA_EXECCOMMAND = 2
_SERVERDATA_RESPONSE_VALUE = 0
_SERVERDATA_AUTH_RESPONSE = 2


class RconError(Exception):
    pass


def _encode(req_id: int, req_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, req_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    raw_len = await reader.readexactly(4)
    (length,) = struct.unpack("<i", raw_len)
    data = await reader.readexactly(length)
    req_id, req_type = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", errors="replace")
    return req_id, req_type, body


async def execute(
    host: str, port: int, password: str, command: str, timeout: float = 10.0
) -> str:
    """Authenticate and run a single command, returning the server's response."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise RconError(f"could not connect to RCON {host}:{port}: {exc}") from exc

    try:
        writer.write(_encode(1, _SERVERDATA_AUTH, password))
        await writer.drain()
        req_id, _, _ = await asyncio.wait_for(_read_packet(reader), timeout=timeout)
        if req_id == -1:
            raise RconError("RCON authentication failed")

        writer.write(_encode(2, _SERVERDATA_EXECCOMMAND, command))
        await writer.drain()
        _, _, body = await asyncio.wait_for(_read_packet(reader), timeout=timeout)
        return body
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
