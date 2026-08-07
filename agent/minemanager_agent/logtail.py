"""Follow an instance's log file and emit new lines.

Console *output* comes from tailing ``logs/latest.log`` (clean, no ANSI — what
we display), rather than scraping the pty.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path


async def follow(path: Path, poll_s: float = 0.5, from_start: bool = False) -> AsyncIterator[str]:
    """Yield lines appended to ``path``, tolerating rotation and late creation."""
    pos = 0
    started = from_start
    ident: tuple[int, int] | None = None
    buf = b""
    while True:
        try:
            st = path.stat()
        except OSError:
            # Missing (or briefly unreadable mid-rotation) is normal: a server
            # that has not written logs/latest.log yet is not an error.
            await asyncio.sleep(poll_s)
            continue

        now_ident = (st.st_dev, st.st_ino)
        if ident is not None and now_ident != ident:
            pos, buf = 0, b""      # replaced by a new file — read it from the top
        ident = now_ident

        if not started:
            pos = st.st_size       # begin at EOF so we only stream *new* output
            started = True
        elif st.st_size < pos:
            pos, buf = 0, b""      # truncated in place

        if st.st_size > pos:
            try:
                with path.open("rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except OSError:
                await asyncio.sleep(poll_s)
                continue
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                yield line.decode("utf-8", errors="replace").rstrip("\r")

        await asyncio.sleep(poll_s)
