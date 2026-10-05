"""RugCheck: authorities, LP lock, holder concentration, creator history and risk flags."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..http import get_json

# RugCheck risk names that point at the creator's past launches.
CREATOR_HISTORY_KEYWORDS = ("creator history", "rugged", "previous rug")


@dataclass
class RugFacts:
    mint_authority: str | None
    freeze_authority: str | None
    lp_locked_pct: float | None
    top10_pct: float
    max_holder_pct: float
    dev_pct: float
    creator: str | None
    rugged: bool
    danger_risks: list[str] = field(default_factory=list)
    warn_risks: list[str] = field(default_factory=list)
    creator_rug_history: bool = False
    insiders_detected: int = 0
    score_normalised: float | None = None


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pool_accounts(markets: list[dict[str, Any]]) -> set[str]:
    """Accounts that hold tokens on behalf of a pool. They aren't real holders."""
    accounts: set[str] = set()
    for m in markets:
        for key in ("pubkey", "liquidityAAccount", "liquidityBAccount", "liquidityA", "liquidityB"):
            if isinstance(m.get(key), str):
                accounts.add(m[key])
    return accounts


def _lp_locked_pct(markets: list[dict[str, Any]]) -> float | None:
    """Liquidity-weighted LP locked percentage across all pools."""
    weighted, total, pcts = 0.0, 0.0, []
    for m in markets:
        lp = m.get("lp") or {}
        if lp.get("lpLockedPct") is None:
            continue
        pct = _num(lp["lpLockedPct"])
        pcts.append(pct)
        weight = _num(lp.get("baseUSD")) + _num(lp.get("quoteUSD"))
        weighted += pct * weight
        total += weight
    if not pcts:
        return None
    # Without USD weights, take the worst pool so an unlocked pool can't hide.
    return weighted / total if total > 0 else min(pcts)


def parse_report(report: dict[str, Any]) -> RugFacts:
    token = report.get("token") or {}
    markets = report.get("markets") or []
    creator = report.get("creator")
    pools = _pool_accounts(markets)

    holders = [
        h for h in report.get("topHolders") or []
        if h.get("address") not in pools and h.get("owner") not in pools
    ]
    pcts = sorted((_num(h.get("pct")) for h in holders), reverse=True)

    dev_pct = sum(_num(h.get("pct")) for h in holders if creator and h.get("owner") == creator)
    supply = _num(token.get("supply"))
    if supply > 0 and report.get("creatorBalance") is not None:
        dev_pct = max(dev_pct, _num(report["creatorBalance"]) / supply * 100)

    danger, warn = [], []
    for r in report.get("risks") or []:
        name = r.get("name") or "?"
        (danger if r.get("level") == "danger" else warn).append(name)
    history = any(
        kw in f"{r.get('name', '')} {r.get('description', '')}".lower()
        for r in report.get("risks") or []
        for kw in CREATOR_HISTORY_KEYWORDS
    )

    return RugFacts(
        mint_authority=token.get("mintAuthority") or report.get("mintAuthority"),
        freeze_authority=token.get("freezeAuthority") or report.get("freezeAuthority"),
        lp_locked_pct=_lp_locked_pct(markets),
        top10_pct=sum(pcts[:10]),
        max_holder_pct=pcts[0] if pcts else 0.0,
        dev_pct=dev_pct,
        creator=creator,
        rugged=bool(report.get("rugged")),
        danger_risks=danger,
        warn_risks=warn,
        creator_rug_history=history,
        insiders_detected=int(_num(report.get("graphInsidersDetected"))),
        score_normalised=report.get("score_normalised"),
    )


class RugCheck:
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.base = base_url.rstrip("/")

    async def report(self, mint: str) -> RugFacts:
        return parse_report(await get_json(self.client, f"{self.base}/v1/tokens/{mint}/report"))
