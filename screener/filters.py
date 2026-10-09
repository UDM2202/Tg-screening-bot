"""Screening rules. Pure functions over source data, so they're easy to test and tune."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Tier1Config, Tier2Config
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
) -> Tier1Result:
    """Hard rug rejects. Every failed rule is recorded so the tracker can grade each one."""
    r = Tier1Result()

    if rug.rugged:
        r.reject("rugged", "RugCheck marks this token as rugged")

    mintable = rug.mint_authority is not None or (goplus is not None and goplus.mintable)
    if cfg.require_mint_authority_revoked and mintable:
        r.reject("mint_authority", "mint authority not revoked")
    freezable = rug.freeze_authority is not None or (goplus is not None and goplus.freezable)
    if cfg.require_freeze_authority_revoked and freezable:
        r.reject("freeze_authority", "freeze authority not revoked")

    if rug.lp_locked_pct is None:
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
    if rug.insiders_detected:
        r.warnings.append(f"{rug.insiders_detected} insider wallets detected")
    return r
