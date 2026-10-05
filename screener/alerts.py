"""Formats the Telegram alert for a token that passed every check."""

from __future__ import annotations

from datetime import datetime
from html import escape

from .filters import Tier1Result
from .socials import SocialsReport
from .sources.dexscreener import Market
from .sources.goplus import GoPlusFacts
from .sources.jupiter import SellSim
from .sources.rugcheck import RugFacts
from .telegram import Button


def usd(value: float | None) -> str:
    if value is None:
        return "?"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:,.0f}"


def age_text(minutes: float | None) -> str:
    if minutes is None:
        return "?"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "?"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def format_alert(
    market: Market,
    rug: RugFacts,
    goplus: GoPlusFacts | None,
    sell: SellSim | None,
    socials: SocialsReport,
    result: Tier1Result,
    now: datetime,
) -> tuple[str, list[list[Button]]]:
    m = market
    liq_ratio = m.liquidity_usd / m.market_cap_usd * 100 if m.market_cap_usd else None
    lines = [
        f"🟢 <b>${escape(m.symbol)}</b> · {escape(m.name)}",
        f"<code>{m.address}</code>",
        "",
        f"MC {usd(m.market_cap_usd)} · Liq {usd(m.liquidity_usd)} ({pct(liq_ratio)})",
        f"Vol 24h {usd(m.volume_h24)} · 1h {pct(m.price_change_h1, signed=True)} · Age {age_text(m.age_minutes(now))}",
        f"DEX: {escape(m.dex_id)}",
        "",
        "✅ Mint revoked · ✅ Freeze revoked",
        f"✅ LP locked/burned {pct(rug.lp_locked_pct)}",
        f"Top 10: {pct(rug.top10_pct)} · Max wallet: {pct(rug.max_holder_pct)} · Dev: {pct(rug.dev_pct)}",
    ]
    if sell is not None and sell.round_trip_loss_pct is not None:
        lines.append(f"Sell sim: round trip {pct(-sell.round_trip_loss_pct, signed=True)}")
    if goplus is None:
        lines.append("GoPlus: not checked")

    social_bits = []
    if socials.website:
        social_bits.append("🌐 site")
    if socials.twitter:
        social_bits.append("𝕏")
    if socials.telegram:
        members = f" ({socials.telegram_members:,})" if socials.telegram_members is not None else ""
        social_bits.append(f"💬 TG{members}")
    lines += ["", f"Socials {socials.score}/{socials.max_score}: " + (" · ".join(social_bits) or "none")]

    warnings = result.warnings + socials.warnings
    if warnings:
        lines.append("")
        lines += [f"⚠️ {escape(w)}" for w in warnings]

    lines += ["", "<i>Shortlist, not a buy signal. 30-second manual check first: chart, holders, X.</i>"]

    links: list[Button] = [("DexScreener", m.url), ("RugCheck", f"https://rugcheck.xyz/tokens/{m.address}")]
    links2: list[Button] = [
        ("Solscan", f"https://solscan.io/token/{m.address}"),
        ("Search X", f"https://x.com/search?q={m.address}&f=live"),
    ]
    buttons = [links, links2]
    social_row = [
        (label, url)
        for label, url in (("Website", socials.website), ("X", socials.twitter), ("Telegram", socials.telegram))
        if url and url.startswith("http")
    ]
    if social_row:
        buttons.append(social_row)
    return "\n".join(lines), buttons
