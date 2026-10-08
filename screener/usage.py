"""Counts how much data the bot downloads, per source, since it started."""

from __future__ import annotations

import time
from collections import Counter

import httpx

started_at = time.time()
downloaded: Counter[str] = Counter()


def add(source: str, nbytes: int) -> None:
    downloaded[source] += nbytes


async def count_response(response: httpx.Response) -> None:
    """httpx response hook: records the bytes received over the network."""
    await response.aread()
    # Compressed bytes off the wire when known, else the body size.
    add(response.url.host or "http", response.num_bytes_downloaded or len(response.content))


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def report() -> str:
    total = sum(downloaded.values())
    hours = max((time.time() - started_at) / 3600, 1 / 60)
    per_day = total / hours * 24
    lines = [f"Data used since start: {fmt_bytes(total)} (about {fmt_bytes(per_day)}/day)"]
    for source, n in downloaded.most_common(4):
        lines.append(f"  {source}: {fmt_bytes(n)}")
    return "\n".join(lines)
