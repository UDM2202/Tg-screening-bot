"""Entry point: python -m screener [--config config.yaml] [--once]"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re

import httpx

from . import usage
from .app import Screener
from .config import load_config
from .db import Database


async def main() -> None:
    parser = argparse.ArgumentParser(description="Solana memecoin screener with Telegram alerts")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="run one screening cycle and exit")
    parser.add_argument("--stats", action="store_true", help="print the tracker report and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    db = Database(cfg.database)
    headers = {"User-Agent": "tg-screening-bot/1.0"}
    hooks = {"response": [usage.count_response]}
    async with httpx.AsyncClient(timeout=20, headers=headers, event_hooks=hooks) as client:
        screener = Screener(cfg, client, db)
        if args.stats:
            print(html.unescape(re.sub(r"<[^>]+>", "", screener.report())))
        elif args.once:
            await screener.run_cycle()
            await screener.track()
        else:
            await screener.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
