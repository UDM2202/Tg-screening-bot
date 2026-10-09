"""Follow-up warnings after an alert: milestones, dips, sell pressure and pulled liquidity.

Every alert is watched from its alert price: the full set of warnings when
watch_all_alerts is on, otherwise only pulled liquidity. /bought restarts the watch from
the price when it was sent, and keeps it running longer.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from html import escape
from typing import Any

from .alerts import usd
from .config import WatchConfig
from .sources.dexscreener import Market

# A dip warning re-arms once price makes a new high this much above the last warned peak.
DIP_REARM_FACTOR = 1.2


@dataclass
class Watch:
    address: str
    symbol: str
    started_at: float
    bought: bool = False
    active: bool = True
    entry_price: float | None = None
    entry_mcap: float | None = None
    peak_price: float | None = None
    peak_mcap: float | None = None
    max_liquidity: float = 0.0
    sent: set[str] = field(default_factory=set)
    dip_peak: float | None = None
    last_pressure_at: float | None = None
    alert_message_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Watch:
        return cls(
            address=row["address"],
            symbol=row["symbol"] or "?",
            started_at=row["started_at"],
            bought=bool(row["bought"]),
            active=bool(row["active"]),
            entry_price=row["entry_price"],
            entry_mcap=row["entry_mcap"],
            peak_price=row["peak_price"],
            peak_mcap=row["peak_mcap"],
            max_liquidity=row["max_liquidity"] or 0.0,
            sent=set(json.loads(row["sent"] or "[]")),
            dip_peak=row["dip_peak"],
            last_pressure_at=row["last_pressure_at"],
            alert_message_id=row["alert_message_id"],
        )

    def to_values(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "symbol": self.symbol,
            "bought": int(self.bought),
            "active": int(self.active),
            "started_at": self.started_at,
            "entry_price": self.entry_price,
            "entry_mcap": self.entry_mcap,
            "peak_price": self.peak_price,
            "peak_mcap": self.peak_mcap,
            "max_liquidity": self.max_liquidity,
            "sent": json.dumps(sorted(self.sent)),
            "dip_peak": self.dip_peak,
            "last_pressure_at": self.last_pressure_at,
            "alert_message_id": self.alert_message_id,
        }

    def start_position(self, market: Market, now: float) -> None:
        """Begin the full watch from the current price (called on /bought)."""
        self.bought = True
        self.active = True
        self.started_at = now
        self.symbol = market.symbol
        self.entry_price = self.peak_price = market.price_usd
        self.entry_mcap = self.peak_mcap = market.market_cap_usd
        self.max_liquidity = max(self.max_liquidity, market.liquidity_usd)
        self.sent = {e for e in self.sent if e == "rug"}
        self.dip_peak = None
        self.last_pressure_at = None


def _multiple(w: Watch, price: float) -> str:
    if not w.entry_price:
        return ""
    basis = "your entry" if w.bought else "the alert price"
    return f" Now {price / w.entry_price:.1f}x vs {basis}."


def evaluate(w: Watch, market: Market | None, cfg: WatchConfig, now: float) -> list[str]:
    """Update the watch with fresh market data and return any warnings to send."""
    name = f"<b>${escape(w.symbol)}</b>"
    messages: list[str] = []

    liquidity = market.liquidity_usd if market else 0.0
    if liquidity > w.max_liquidity:
        w.max_liquidity = liquidity
    drop = 1 - liquidity / w.max_liquidity if w.max_liquidity else 0.0
    if "rug" not in w.sent and w.max_liquidity and drop * 100 >= cfg.liquidity_drop_pct:
        w.sent.add("rug")
        messages.append(
            f"🚨 {name}: liquidity fell {drop * 100:.0f}% "
            f"({usd(w.max_liquidity)} → {usd(liquidity)}). Possible rug. Check it now."
        )

    full_watch = w.bought or cfg.watch_all_alerts
    if not full_watch or market is None or not market.price_usd or not w.entry_price:
        return messages
    price = market.price_usd
    mcap = market.market_cap_usd

    if w.peak_price is None or price > w.peak_price:
        w.peak_price, w.peak_mcap = price, mcap

    # Milestones: if price jumps past several at once, announce only the highest.
    multiple = price / w.entry_price
    reached = [m for m in sorted(cfg.milestones) if multiple >= m and f"x{m:g}" not in w.sent]
    if reached:
        w.sent.update(f"x{m:g}" for m in reached)
        since = "since you bought" if w.bought else "since the alert"
        tip = " Consider taking some profit." if w.bought else ""
        messages.append(
            f"🚀 {name} hit {reached[-1]:g}x {since} (MC {usd(w.entry_mcap)} → {usd(mcap)}).{tip}"
        )

    fall = 1 - price / w.peak_price
    rearmed = w.dip_peak is None or w.peak_price >= w.dip_peak * DIP_REARM_FACTOR
    if fall * 100 >= cfg.dip_from_peak_pct and rearmed:
        w.dip_peak = w.peak_price
        messages.append(
            f"⚠️ {name} is down {fall * 100:.0f}% from its peak "
            f"(MC {usd(w.peak_mcap)} → {usd(mcap)}).{_multiple(w, price)}"
        )

    heavy_selling = (
        market.sells_m5 >= cfg.sell_min_count
        and market.sells_m5 >= cfg.sell_ratio * max(market.buys_m5, 1)
        and (market.price_change_m5 or 0) <= -cfg.sell_price_drop_pct
    )
    cooled_down = (
        w.last_pressure_at is None or now - w.last_pressure_at >= cfg.pressure_cooldown_minutes * 60
    )
    if heavy_selling and cooled_down:
        w.last_pressure_at = now
        messages.append(
            f"🔻 {name}: {market.sells_m5} sells vs {market.buys_m5} buys in the last 5 min, "
            f"price {market.price_change_m5:+.0f}%.{_multiple(w, price)}"
        )
    return messages
