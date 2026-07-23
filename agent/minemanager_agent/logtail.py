"""Follow an instance's log file and emit new lines.

Console *output* comes from tailing ``logs/latest.log`` (clean, no ANSI — what
we display), rather than scraping the pty. Handles the common rotation case
where ``latest.log`` is truncated/replaced (size shrinks) by seeking back to 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path


async def follow(path: Path, poll_s: float = 0.5, from_start: bool = False) -> AsyncIterator[str]:
    """Yield lines appended to ``path``, tolerating rotation and late creation."""
    pos = 0
    started = from_start
    buf = ""
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            await asyncio.sleep(poll_s)
            continue

        if not started:
            # Begin at the current end so we only stream *new* output.
            pos = size
            started = True

        if size < pos:
            # File was rotated/truncated — restart from the top.
            pos = 0
            buf = ""

        if size > pos:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            buf += chunk
            *lines, buf = buf.split("\n")
            for line in lines:
                yield line

        await asyncio.sleep(poll_s)
