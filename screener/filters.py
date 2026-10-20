"""Screening rules. Pure functions over source data, so they're easy to test and tune."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import PullbackConfig, Tier1Config, Tier2Config
from .sources.dexscreener import Market
from .sources.goplus import GoPlusFacts
from .sources.jupiter import SellSim
from .sources.rugcheck import RugFacts


@dataclass
class Tier2Result:
    passed: bool
    expired: bool  # too old to ever pass; stop watching
    reasons: list[str] = field(default_factory=list)


def tier2(market: Market, cfg: Tier2Config, now: datetime, peak_mcap: float | None = None) -> Tier2Result:
    """Cheap market pre-filter. Failing tokens are re-checked next cycle until they expire."""
    age = market.age_minutes(now)
    if age is not None and age > cfg.max_age_hours * 60:
        return Tier2Result(False, True, [f"older than {cfg.max_age_hours:g}h"])

    if (
        age is not None
        and age > cfg.give_up_after_minutes
        and market.liquidity_usd < cfg.give_up_below_liquidity_usd
    ):
        return Tier2Result(False, True, [f"still under ${cfg.give_up_below_liquidity_usd:,.0f} liquidity"])

    reasons = []
    if age is None:
        reasons.append("pool age unknown")
    elif age < cfg.min_age_minutes:
        reasons.append(f"younger than {cfg.min_age_minutes:g}m")

    liq = market.liquidity_usd
    mcap = market.market_cap_usd
    if liq < cfg.min_liquidity_usd:
        reasons.append(f"liquidity ${liq:,.0f} < ${cfg.min_liquidity_usd:,.0f}")
    if mcap is None:
        reasons.append("market cap unknown")
    else:
        if not cfg.min_market_cap_usd <= mcap <= cfg.max_market_cap_usd:
            reasons.append(f"market cap ${mcap:,.0f} outside range")
        if mcap > 0 and liq / mcap * 100 < cfg.min_liquidity_to_mcap_pct:
            reasons.append(f"liquidity/mcap {liq / mcap * 100:.1f}% < {cfg.min_liquidity_to_mcap_pct:g}%")
    if liq > 0 and market.volume_h24 / liq < cfg.min_volume_to_liquidity:
        reasons.append(f"24h volume {market.volume_h24 / liq:.2f}x liquidity")
    if peak_mcap and mcap is not None and mcap < peak_mcap * (1 - cfg.max_drop_from_peak_pct / 100):
        reasons.append(f"down {(1 - mcap / peak_mcap) * 100:.0f}% from its peak of ${peak_mcap:,.0f}")
    if market.price_change_m5 is not None and market.price_change_m5 <= -cfg.max_drop_5m_pct:
        reasons.append(f"dropped {-market.price_change_m5:.0f}% in the last 5 minutes")
    if cfg.require_any_social and not (market.websites or market.socials):
        reasons.append("no socials")
    return Tier2Result(not reasons, False, reasons)


def pullback_reasons(market: Market, cfg: PullbackConfig, history, now_ts: float) -> list[str]:
    """Is this a pullback worth an alert? history: first/peak/trough market caps. Empty = yes."""
    mcap = market.market_cap_usd
    first, peak = history["first_mcap"], history["peak_mcap"]
    low, low_at = history["trough_mcap"], history["trough_at"]
    if not (mcap and first and peak and low and low_at):
        return ["no pullback seen yet"]
    reasons = []
    if peak < first * cfg.min_run_x:
        reasons.append(f"first run only {peak / first:.1f}x")
    drop = (1 - low / peak) * 100
    if not cfg.min_drop_pct <= drop <= cfg.max_drop_pct:
        reasons.append(f"pullback {drop:.0f}% from the peak, outside {cfg.min_drop_pct:g}-{cfg.max_drop_pct:g}%")
    if mcap < low * (1 + cfg.min_bounce_pct / 100):
        reasons.append(f"only {(mcap / low - 1) * 100:.0f}% off the low")
    if (now_ts - low_at) / 60 < cfg.min_minutes_since_low:
        reasons.append("the low is too recent")
    if mcap > peak * (1 - cfg.min_below_peak_pct / 100):
        reasons.append("already back near the peak")
    if market.buys_m5 is not None and market.sells_m5 is not None and market.buys_m5 <= market.sells_m5:
        reasons.append("more sells than buys in the last 5 minutes")
    if market.price_change_m5 is not None and market.price_change_m5 <= 0:
        reasons.append("not rising in the last 5 minutes")
    return reasons


def near_bond_reasons(coin, cfg, now: datetime, peak_mcap: float | None) -> list[str]:
    """Market filter for pump.fun coins still on the bonding curve. Empty list = passes."""
    reasons = []
    if not cfg.min_progress_pct <= coin.progress_pct <= cfg.max_progress_pct:
        reasons.append(f"{coin.progress_pct:.0f}% bonded, outside {cfg.min_progress_pct:g}-{cfg.max_progress_pct:g}%")
    if not cfg.min_market_cap_usd <= coin.market_cap_usd <= cfg.max_market_cap_usd:
        reasons.append(f"market cap ${coin.market_cap_usd:,.0f} outside range")
    age = coin.age_minutes(now)
    if age is None or age < cfg.min_age_minutes:
        reasons.append(f"younger than {cfg.min_age_minutes:g}m")
    if peak_mcap and coin.market_cap_usd < peak_mcap * (1 - cfg.max_drop_from_peak_pct / 100):
        reasons.append(f"down {(1 - coin.market_cap_usd / peak_mcap) * 100:.0f}% from its peak of ${peak_mcap:,.0f}")
    return reasons


@dataclass
class Tier1Result:
    rejects: list[tuple[str, str]] = field(default_factory=list)  # (code, message)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.rejects

    def reject(self, code: str, message: str) -> None:
        self.rejects.append((code, message))


def tier1(
    rug: RugFacts,
    goplus: GoPlusFacts | None,
    sell: SellSim | None,
    cfg: Tier1Config,
    on_curve: bool = False,
) -> Tier1Result:
    """Hard rug rejects. Every failed rule is recorded so the tracker can grade each one.

    on_curve: the coin still trades on pump.fun's bonding curve. Its liquidity is held by
    the pump.fun program, so there is no LP to lock and the dev can't pull it.
    """
    r = Tier1Result()

    if rug.rugged:
        r.reject("rugged", "RugCheck marks this token as rugged")

    mintable = rug.mint_authority is not None or (goplus is not None and goplus.mintable)
    if cfg.require_mint_authority_revoked and mintable:
        r.reject("mint_authority", "mint authority not revoked")
    freezable = rug.freeze_authority is not None or (goplus is not None and goplus.freezable)
    if cfg.require_freeze_authority_revoked and freezable:
        r.reject("freeze_authority", "freeze authority not revoked")

    if on_curve:
        pass
    elif rug.lp_locked_pct is None:
        r.reject("lp_unlocked", "LP lock status unknown")
    elif rug.lp_locked_pct < cfg.min_lp_locked_pct:
        r.reject("lp_unlocked", f"only {rug.lp_locked_pct:.0f}% of LP locked/burned")

    if rug.top10_pct > cfg.max_top10_holders_pct:
        r.reject("top10_holders", f"top 10 holders own {rug.top10_pct:.1f}%")
    if rug.max_holder_pct > cfg.max_single_holder_pct:
        r.reject("single_holder", f"one wallet owns {rug.max_holder_pct:.1f}%")
    if rug.dev_pct > cfg.max_dev_holding_pct:
        r.reject("dev_holding", f"dev wallet owns {rug.dev_pct:.1f}%")

    bad_creator = rug.creator_rug_history or (goplus is not None and goplus.malicious_creator)
    if cfg.reject_creator_rug_history and bad_creator:
        r.reject("creator_history", "creator has a rug history or is flagged malicious")

    if cfg.reject_rugcheck_danger_risks:
        ignored = {name.lower() for name in cfg.ignore_danger_risks}
        danger = [d for d in rug.danger_risks if d.lower() not in ignored]
        if on_curve:  # LP and liquidity warnings don't apply before the pool exists
            danger = [d for d in danger if "lp" not in d.lower().split() and "liquidity" not in d.lower()]
        if danger:
            r.reject("rugcheck_danger", "RugCheck danger: " + ", ".join(danger))

    if cfg.reject_token2022_traps and goplus is not None and goplus.traps:
        r.reject("token2022_trap", "Token-2022 trap: " + ", ".join(goplus.traps))

    if cfg.require_sell_route and sell is not None:
        if not sell.sellable:
            r.reject("honeypot", f"sell simulation failed ({sell.error})")
        elif (sell.round_trip_loss_pct or 0) > cfg.max_round_trip_loss_pct:
            r.reject("honeypot", f"round trip loses {sell.round_trip_loss_pct:.1f}%")

    r.warnings.extend(rug.warn_risks)
    if rug.insiders_detected > cfg.max_insider_wallets:
        r.reject("insiders", f"{rug.insiders_detected} insider wallets detected")
    elif rug.insiders_detected:
        r.warnings.append(f"{rug.insiders_detected} insider wallets detected")
    if rug.bundle_bag_pct is not None:
        r.warnings.append(
            f"possible bundle: {rug.bundle_wallets} top holders with near-identical bags "
            f"(~{rug.bundle_bag_pct:.2f}% each)"
        )
    return r
