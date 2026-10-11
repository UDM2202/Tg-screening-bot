"""pump.fun coins still on the bonding curve, with how close each is to bonding.

Uses pump.fun's public (unofficial) web API: the coins it lists by market cap,
excluding ones that already graduated, are exactly the ones nearest to bonding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from ..http import ApiError, get_json
from .dexscreener import Market

TOKEN_DECIMALS = 6
TOTAL_SUPPLY_TOKENS = 1_000_000_000
# pump.fun sells 793.1M tokens on the curve; it's bonded when they're all sold.
CURVE_TOKENS = 793_100_000
# Virtual token reserves start at 1,073M (the curve tokens plus a 279.9M offset).
VIRTUAL_TOKEN_OFFSET = 279_900_000
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tg-screening-bot)", "Accept": "application/json"}


@dataclass
class CurveCoin:
    mint: str
    symbol: str
    name: str
    progress_pct: float
    market_cap_usd: float
    created_at: datetime | None
    creator: str | None = None
    curve_accounts: set[str] = field(default_factory=set)
    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None

    @property
    def price_usd(self) -> float:
        return self.market_cap_usd / TOTAL_SUPPLY_TOKENS

    def age_minutes(self, now: datetime) -> float | None:
        return (now - self.created_at).total_seconds() / 60 if self.created_at else None

    def as_market(self) -> Market:
        """A Market view, so socials checks and follow-ups work the same as for other alerts."""
        socials = [(k, v) for k, v in (("twitter", self.twitter), ("telegram", self.telegram)) if v]
        return Market(
            address=self.mint,
            symbol=self.symbol,
            name=self.name,
            pair_address="",
            dex_id="pump.fun curve",
            url=f"https://dexscreener.com/solana/{self.mint}",
            price_usd=self.price_usd,
            liquidity_usd=0.0,
            market_cap_usd=self.market_cap_usd,
            volume_h24=0.0,
            price_change_h1=None,
            created_at=self.created_at,
            websites=[self.website] if self.website else [],
            socials=socials,
        )


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bonding_progress(coin: dict[str, Any]) -> float | None:
    """Percent of the curve's tokens sold (100 = bonded)."""
    real = _num(coin.get("real_token_reserves"))
    if real is not None:
        tokens_left = real / 10**TOKEN_DECIMALS
    else:
        virtual = _num(coin.get("virtual_token_reserves"))
        if virtual is None:
            return None
        tokens_left = virtual / 10**TOKEN_DECIMALS - VIRTUAL_TOKEN_OFFSET
    return max(0.0, min(100.0, (1 - tokens_left / CURVE_TOKENS) * 100))


def parse_coin(coin: dict[str, Any]) -> CurveCoin | None:
    mint = coin.get("mint")
    if not mint or coin.get("complete") or coin.get("raydium_pool") or coin.get("pump_swap_pool"):
        return None
    progress = bonding_progress(coin)
    mcap = _num(coin.get("usd_market_cap"))
    if progress is None or mcap is None:
        return None
    created_ms = _num(coin.get("created_timestamp"))
    return CurveCoin(
        mint=mint,
        symbol=coin.get("symbol") or "?",
        name=coin.get("name") or "?",
        progress_pct=progress,
        market_cap_usd=mcap,
        created_at=datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None,
        creator=coin.get("creator"),
        curve_accounts={a for a in (coin.get("bonding_curve"), coin.get("associated_bonding_curve")) if a},
        website=coin.get("website") or None,
        twitter=coin.get("twitter") or None,
        telegram=coin.get("telegram") or None,
    )


class PumpFun:
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.base = base_url.rstrip("/")
        self.last_listed = 0  # coins in the last response, before dropping graduated ones

    async def top_unbonded(self, limit: int) -> list[CurveCoin]:
        """Coins still on the curve, largest market cap (closest to bonding) first."""
        data = await get_json(
            self.client,
            f"{self.base}/coins",
            params={
                "offset": 0,
                "limit": limit,
                "sort": "market_cap",
                "order": "DESC",
                "includeNsfw": "false",
                "complete": "false",
            },
            headers=HEADERS,
        )
        if isinstance(data, dict):
            data = data.get("coins") or data.get("data") or []
        if not isinstance(data, list):
            raise ApiError(f"pump.fun: unexpected response {str(data)[:200]}")
        self.last_listed = len(data)
        return [c for c in (parse_coin(raw) for raw in data if isinstance(raw, dict)) if c]
