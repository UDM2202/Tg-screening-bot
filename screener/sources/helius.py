"""Helius websocket: new liquidity pools on Raydium, PumpSwap and Meteora.

Subscribes to program logs with the standard `logsSubscribe` method (works on the
free plan). A log notification only carries the transaction signature, so pool
creations are fetched with `getTransaction` to find the token's mint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import websockets

from .. import usage
from ..http import ApiError

log = logging.getLogger(__name__)

# Program id -> (name, regex that matches the pool-creation log line).
POOL_PROGRAMS: dict[str, tuple[str, str]] = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("raydium_amm_v4", r"initialize2"),
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP52": ("raydium_cpmm", r"Instruction: Initialize$"),
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": ("pumpswap", r"Instruction: CreatePool$"),
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": ("meteora_dlmm", r"Instruction: InitializeLbPair"),
}

# Quote tokens a new pool is paired against. These are never the new token.
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def is_pool_creation(program_id: str, logs: list[str]) -> bool:
    pattern = re.compile(POOL_PROGRAMS[program_id][1])
    return any(pattern.search(line) for line in logs)


def new_token_mints(tx: dict[str, Any]) -> list[str]:
    """Token mints in a pool-creation transaction, excluding quote tokens.

    Mints that already had balances before the transaction are the traded token.
    The pool's LP mint is created in the same transaction, so it only appears
    afterwards and is skipped whenever a pre-existing mint is found.
    """
    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return []
    pre = {b.get("mint") for b in meta.get("preTokenBalances") or []} - QUOTE_MINTS - {None}
    post = {b.get("mint") for b in meta.get("postTokenBalances") or []} - QUOTE_MINTS - {None}
    return sorted(pre & post) or sorted(post)


class HeliusPools:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        ws_url: str,
        rpc_url: str,
        on_token: Callable[[str, str], Awaitable[None]],
        programs: list[str] | None = None,
    ):
        self.client = client
        self.api_key = api_key
        self.ws_url = f"{ws_url.rstrip('/')}/?api-key={api_key}"
        self.rpc_url = f"{rpc_url.rstrip('/')}/?api-key={api_key}"
        self.on_token = on_token
        self.programs = [p for p in POOL_PROGRAMS if programs is None or POOL_PROGRAMS[p][0] in programs]
        known = {name for name, _ in POOL_PROGRAMS.values()}
        for name in set(programs or []) - known:
            log.warning("Helius: unknown program %r in helius_programs (known: %s)", name, sorted(known))
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=1000)
        self.subscriptions: dict[int, str] = {}  # subscription id -> program id
        self._pending: dict[int, str] = {}  # request id -> program id

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
            ],
        }
        try:
            resp = await self.client.post(self.rpc_url, json=payload)
        except httpx.HTTPError as exc:
            raise ApiError(self._redact(f"Helius RPC: {exc}")) from exc
        if resp.status_code != 200:
            raise ApiError(f"Helius RPC: HTTP {resp.status_code}")
        body = resp.json()
        if body.get("error"):
            raise ApiError(f"Helius RPC: {body['error']}")
        return body.get("result")

    async def handle_message(self, msg: dict[str, Any]) -> None:
        """Route one websocket message. Pool creations are queued for lookup."""
        if "id" in msg and msg["id"] in self._pending:
            program = self._pending.pop(msg["id"])
            if "result" in msg:
                self.subscriptions[msg["result"]] = program
                log.info("Helius: watching %s pools", POOL_PROGRAMS[program][0])
            else:
                log.warning("Helius: subscribe to %s failed: %s", POOL_PROGRAMS[program][0], msg.get("error"))
            return
        if msg.get("method") != "logsNotification":
            return
        params = msg.get("params") or {}
        program = self.subscriptions.get(params.get("subscription"))
        value = (params.get("result") or {}).get("value") or {}
        if not program or value.get("err") is not None or not value.get("signature"):
            return
        if is_pool_creation(program, value.get("logs") or []):
            try:
                self.queue.put_nowait((value["signature"], program))
            except asyncio.QueueFull:
                log.warning("Helius: lookup queue full, dropping a new pool")

    async def resolve(self, signature: str, program: str) -> None:
        tx = None
        for _ in range(3):  # the transaction can take a moment to become fetchable
            tx = await self.get_transaction(signature)
            if tx:
                break
            await asyncio.sleep(2)
        for mint in new_token_mints(tx or {}):
            await self.on_token(mint, POOL_PROGRAMS[program][0])

    async def _worker(self) -> None:
        while True:
            signature, program = await self.queue.get()
            try:
                await self.resolve(signature, program)
            except ApiError as exc:
                log.warning("Helius: lookup of %s failed: %s", signature[:12], exc)
            except Exception:
                log.exception("Helius: unexpected error resolving %s", signature[:12])
            finally:
                self.queue.task_done()

    async def _listen(self) -> None:
        delay = 5
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=30) as ws:
                    self.subscriptions.clear()
                    self._pending.clear()
                    for i, program in enumerate(self.programs, start=1):
                        self._pending[i] = program
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": i,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [program]}, {"commitment": "confirmed"}],
                        }))
                    delay = 5
                    async for raw in ws:
                        usage.add("helius websocket", len(raw))
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            await self.handle_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # drops are expected; keep reconnecting
                log.warning("Helius stream dropped (%s), reconnecting in %ss", self._redact(str(exc)), delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def run(self, workers: int = 4) -> None:
        await asyncio.gather(self._listen(), *(self._worker() for _ in range(workers)))
