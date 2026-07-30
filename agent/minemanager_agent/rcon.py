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
_SERVERDATA_AUTH_RESPONSE = 2   # same wire value as EXECCOMMAND; different direction

# Correlation ids we use. The sentinel is the standard way to delimit a
# multi-packet reply: the server answers requests in order, so its response to
# the sentinel cannot arrive before the last fragment of the command's reply.
_AUTH_ID = 1
_CMD_ID = 2
_END_ID = 3

# Protocol maximum is 4096; allow a little slack for the header and terminators.
_MIN_PACKET = 10          # 4 id + 4 type + 0 body + 2 NUL
_MAX_PACKET = 4096 + 16


class RconError(Exception):
    pass


def _encode(req_id: int, req_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, req_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    raw_len = await reader.readexactly(4)
    (length,) = struct.unpack("<i", raw_len)
    # Bound the declared length: a hostile or malfunctioning server could
    # otherwise send a negative value (opaque ValueError) or a huge one.
    if not _MIN_PACKET <= length <= _MAX_PACKET:
        raise RconError(f"invalid RCON packet length: {length}")
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
        writer.write(_encode(_AUTH_ID, _SERVERDATA_AUTH, password))
        await writer.drain()
        # Some implementations send an empty RESPONSE_VALUE *before* the auth
        # result; keep reading until the actual AUTH_RESPONSE arrives so a junk
        # packet can't be mistaken for a successful authentication.
        while True:
            req_id, req_type, _ = await asyncio.wait_for(_read_packet(reader), timeout=timeout)
            if req_type == _SERVERDATA_AUTH_RESPONSE:
                break
        if req_id == -1:
            raise RconError("RCON authentication failed")

        # Long replies (`list` on a busy server, `help`) are split across several
        # packets. Send a sentinel straight after the command and read until it
        # comes back, concatenating every fragment — reading a single packet
        # silently truncated the output.
        writer.write(_encode(_CMD_ID, _SERVERDATA_EXECCOMMAND, command))
        writer.write(_encode(_END_ID, _SERVERDATA_RESPONSE_VALUE, ""))
        await writer.drain()

        parts: list[str] = []
        while True:
            try:
                req_id, _, body = await asyncio.wait_for(_read_packet(reader), timeout=timeout)
            except asyncio.TimeoutError:
                if parts:
                    break  # server never echoed the sentinel — return what we got
                raise RconError("timed out waiting for the RCON response")
            if req_id == _END_ID:
                break
            if req_id == _CMD_ID:
                parts.append(body)
        return "".join(parts)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
