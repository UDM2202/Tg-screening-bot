"""Honeypot check: quote a buy on Jupiter, then quote selling what the buy would return.

This is a quote, not an on-chain simulation, so it catches tokens with no sell route
or heavy sell taxes. The freeze authority and Token-2022 checks cover the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..http import ApiError

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class SellSim:
    sellable: bool
    round_trip_loss_pct: float | None = None
    error: str | None = None


class Jupiter:
    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str = ""):
        self.client = client
        self.base = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else {}

    async def _quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> int | None:
        """Return the quoted output amount, or None when Jupiter finds no route."""
        try:
            resp = await self.client.get(
                f"{self.base}/swap/v1/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount),
                    "slippageBps": str(slippage_bps),
                },
                headers=self.headers,
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"Jupiter: {exc}") from exc
        if resp.status_code in (400, 404) and "route" in resp.text.lower():
            return None
        if resp.status_code != 200:
            raise ApiError(f"Jupiter: HTTP {resp.status_code} {resp.text[:200]}")
        out = int(resp.json().get("outAmount") or 0)
        return out or None

    async def round_trip(self, mint: str, sol_amount: float, slippage_bps: int) -> SellSim:
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        tokens = await self._quote(SOL_MINT, mint, lamports, slippage_bps)
        if tokens is None:
            return SellSim(sellable=False, error="no buy route")
        sol_back = await self._quote(mint, SOL_MINT, tokens, slippage_bps)
        if sol_back is None:
            return SellSim(sellable=False, error="no sell route")
        return SellSim(sellable=True, round_trip_loss_pct=(1 - sol_back / lamports) * 100)
