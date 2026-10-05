"""Sample API payloads shaped like the real DexScreener, RugCheck, GoPlus and Jupiter responses."""

from __future__ import annotations

import time

GOOD = "GoodToken1111111111111111111111111111111pump"
RUG = "RugToken11111111111111111111111111111111pump"
YOUNG = "YoungToken111111111111111111111111111111pump"
CREATOR = "Creator1111111111111111111111111111111111111"
POOL = "PoolAcct111111111111111111111111111111111111"


def pair(address: str, *, symbol: str = "GOOD", age_min: float = 120, liq: float = 20_000,
         mcap: float = 200_000, vol: float = 60_000, price: float = 0.0002, socials: bool = True) -> dict:
    info = {}
    if socials:
        info = {
            "websites": [{"label": "Website", "url": "https://good.example"}],
            "socials": [
                {"type": "twitter", "url": "https://x.com/goodtoken"},
                {"type": "telegram", "url": "https://t.me/goodtoken"},
            ],
        }
    return {
        "chainId": "solana",
        "dexId": "pumpswap",
        "url": f"https://dexscreener.com/solana/{address.lower()}",
        "pairAddress": f"pair-{address[:8]}",
        "baseToken": {"address": address, "name": symbol.title(), "symbol": symbol},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
        "priceUsd": str(price),
        "volume": {"h24": vol, "h1": vol / 10},
        "priceChange": {"h1": 12.5},
        "liquidity": {"usd": liq},
        "fdv": mcap,
        "marketCap": mcap,
        "pairCreatedAt": int((time.time() - age_min * 60) * 1000),
        "info": info,
    }


def rug_report(*, mint_auth=None, freeze_auth=None, lp_locked=100.0, holders=None,
               risks=None, rugged=False) -> dict:
    if holders is None:
        holders = [
            {"address": POOL, "owner": "pool-owner", "pct": 25.0},  # pool, must be ignored
            {"address": "h1", "owner": CREATOR, "pct": 1.0},
            *({"address": f"h{i}", "owner": f"o{i}", "pct": 2.0} for i in range(2, 12)),
        ]
    return {
        "mint": GOOD,
        "creator": CREATOR,
        "token": {"mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "supply": 1_000_000_000, "decimals": 6},
        "topHolders": holders,
        "risks": risks or [],
        "rugged": rugged,
        "markets": [
            {"pubkey": "market1", "liquidityAAccount": POOL, "liquidityBAccount": "solvault",
             "lp": {"lpLockedPct": lp_locked, "baseUSD": 10_000, "quoteUSD": 10_000}},
        ],
        "graphInsidersDetected": 0,
    }


def goplus_result(mint: str, **overrides) -> dict:
    data = {
        "mintable": {"status": "0"},
        "freezable": {"status": "0"},
        "closable": {"status": "0"},
        "balance_mutable_authority": {"status": "0"},
        "non_transferable": "0",
        "transfer_hook": [],
        "transfer_fee": {},
        "default_account_state": "0",
        "creators": [{"address": CREATOR, "malicious_address": 0}],
    }
    data.update(overrides)
    return {"code": 1, "message": "OK", "result": {mint: data}}
