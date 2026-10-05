"""PumpPortal websocket: pump.fun tokens that just graduated to an AMM pool."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)


async def stream_migrations(url: str, on_token: Callable[[str], Awaitable[None]]) -> None:
    """Call on_token(mint) for each migration. Reconnects forever."""
    delay = 5
    while True:
        try:
            async with websockets.connect(url, ping_interval=30) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log.info("Connected to PumpPortal migrations")
                delay = 5
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    mint = msg.get("mint") if isinstance(msg, dict) else None
                    if mint:
                        await on_token(mint)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network drops are expected; keep reconnecting
            log.warning("PumpPortal stream dropped (%s), reconnecting in %ss", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)
