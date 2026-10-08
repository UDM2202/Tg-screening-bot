"""DexScreener: token discovery and market data (price, liquidity, volume, socials)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from ..http import get_json

CHAIN = "solana"
BATCH_SIZE = 30  # max addresses per /tokens/v1 request


@dataclass
class Market:
    address: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    url: str
    price_usd: float | None
    liquidity_usd: float
    market_cap_usd: float | None
    volume_h24: float
    price_change_h1: float | None
    created_at: datetime | None
    websites: list[str] = field(default_factory=list)
    socials: list[tuple[str, str]] = field(default_factory=list)  # (type, url)
    buys_m5: int = 0
    sells_m5: int = 0
    price_change_m5: float | None = None

    def age_minutes(self, now: datetime) -> float | None:
        if self.created_at is None:
            return None
        return (now - self.created_at).total_seconds() / 60


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_market(address: str, pairs: list[dict[str, Any]]) -> Market | None:
    """Combine all Solana pairs of a token into one Market view.

    Liquidity and volume are summed across pools, price and market cap come from
    the deepest pool, and age comes from the oldest pool.
    """
    pairs = [
        p for p in pairs
        if p.get("chainId") == CHAIN and (p.get("baseToken") or {}).get("address") == address
    ]
    if not pairs:
        return None

    def liq(p: dict[str, Any]) -> float:
        return _float((p.get("liquidity") or {}).get("usd")) or 0.0

    best = max(pairs, key=liq)
    created = [p["pairCreatedAt"] for p in pairs if p.get("pairCreatedAt")]
    websites: list[str] = []
    socials: list[tuple[str, str]] = []
    for p in pairs:
        info = p.get("info") or {}
        for w in info.get("websites") or []:
            if w.get("url") and w["url"] not in websites:
                websites.append(w["url"])
        for s in info.get("socials") or []:
            kind = (s.get("type") or s.get("platform") or "").lower()
            if s.get("url") and (kind, s["url"]) not in socials:
                socials.append((kind, s["url"]))

    base = best.get("baseToken") or {}
    return Market(
        address=address,
        symbol=base.get("symbol") or "?",
        name=base.get("name") or "?",
        pair_address=best.get("pairAddress") or "",
        dex_id=best.get("dexId") or "",
        url=best.get("url") or f"https://dexscreener.com/solana/{address}",
        price_usd=_float(best.get("priceUsd")),
        liquidity_usd=sum(liq(p) for p in pairs),
        market_cap_usd=_float(best.get("marketCap")) or _float(best.get("fdv")),
        volume_h24=sum(_float((p.get("volume") or {}).get("h24")) or 0.0 for p in pairs),
        price_change_h1=_float((best.get("priceChange") or {}).get("h1")),
        price_change_m5=_float((best.get("priceChange") or {}).get("m5")),
        buys_m5=sum(int(((p.get("txns") or {}).get("m5") or {}).get("buys") or 0) for p in pairs),
        sells_m5=sum(int(((p.get("txns") or {}).get("m5") or {}).get("sells") or 0) for p in pairs),
        created_at=(
            datetime.fromtimestamp(min(created) / 1000, tz=timezone.utc) if created else None
        ),
        websites=websites,
        socials=socials,
    )


class DexScreener:
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.base = base_url.rstrip("/")

    async def _latest(self, path: str) -> list[dict[str, Any]]:
        data = await get_json(self.client, f"{self.base}{path}")
        if isinstance(data, dict):  # some endpoints wrap the list
            data = data.get("data") or data.get("pairs") or []
        return [d for d in data if d.get("chainId") == CHAIN and d.get("tokenAddress")]

    async def latest_profiles(self) -> list[dict[str, Any]]:
        return await self._latest("/token-profiles/latest/v1")

    async def latest_boosts(self) -> list[dict[str, Any]]:
        return await self._latest("/token-boosts/latest/v1")

    async def markets(self, addresses: list[str]) -> dict[str, Market]:
        """Market data for many tokens. Tokens with no pairs are left out."""
        out: dict[str, Market] = {}
        for i in range(0, len(addresses), BATCH_SIZE):
            chunk = addresses[i : i + BATCH_SIZE]
            pairs = await get_json(self.client, f"{self.base}/tokens/v1/{CHAIN}/{','.join(chunk)}")
            if isinstance(pairs, dict):
                pairs = pairs.get("pairs") or []
            by_token: dict[str, list[dict[str, Any]]] = {}
            for p in pairs or []:
                addr = (p.get("baseToken") or {}).get("address")
                if addr:
                    by_token.setdefault(addr, []).append(p)
            for addr in chunk:
                market = parse_market(addr, by_token.get(addr, []))
                if market:
                    out[addr] = market
        return out
