"""Sample API payloads shaped like the real DexScreener, RugCheck, GoPlus and Jupiter responses."""

from __future__ import annotations

import time

GOOD = "GoodToken1111111111111111111111111111111pump"
RUG = "RugToken11111111111111111111111111111111pump"
YOUNG = "YoungToken111111111111111111111111111111pump"
CREATOR = "Creator1111111111111111111111111111111111111"
POOL = "PoolAcct111111111111111111111111111111111111"


def pair(address: str, *, symbol: str = "GOOD", age_min: float = 180, liq: float = 20_000,
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
            # Ten more holders with varied bags (20% together), as real buyers have.
            *({"address": f"h{i}", "owner": f"o{i}", "pct": pct}
              for i, pct in zip(range(2, 12), (3.0, 2.6, 2.3, 2.1, 2.0, 1.9, 1.8, 1.6, 1.5, 1.2))),
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
        "totalHolders": 850,
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

CURVE = "CurveCoin1111111111111111111111111111111pump"
CURVE_ACCOUNT = "BondingCurve11111111111111111111111111111111"
CURVE_TOKENS_ACCOUNT = "CurveTokens111111111111111111111111111111111"


def pumpfun_coin(mint: str = CURVE, *, progress: float = 90.0, mcap: float = 35_000, age_min: float = 30,
                 complete: bool = False) -> dict:
    tokens_left = 793_100_000 * (1 - progress / 100)
    return {
        "mint": mint,
        "name": "Curve Coin",
        "symbol": "CURVE",
        "creator": CREATOR,
        "bonding_curve": CURVE_ACCOUNT,
        "associated_bonding_curve": CURVE_TOKENS_ACCOUNT,
        "real_token_reserves": int(tokens_left * 10**6),
        "usd_market_cap": mcap,
        "created_timestamp": int((time.time() - age_min * 60) * 1000),
        "complete": complete,
        "twitter": "https://x.com/curvecoin",
        "website": "https://curve.example",
    }


def curve_rug_report() -> dict:
    """A bonding-curve coin: no LP market yet, and the curve's token account holds the unsold supply."""
    report = rug_report()
    report["markets"] = []
    report["topHolders"] = [
        {"address": CURVE_TOKENS_ACCOUNT, "owner": CURVE_ACCOUNT, "pct": 35.0},
        *({"address": f"c{i}", "owner": f"co{i}", "pct": 1.5 - i / 10} for i in range(10)),
    ]
    report["risks"] = [{"name": "Large Amount of LP Unlocked", "level": "danger"}]
    return report


def bundled_holders() -> list[dict]:
    """FEDLIENS-style top holders: eight wallets with bags of 0.41-0.45%, among normal ones."""
    bags = (1.42, 1.05, 1.04, 0.85, 0.77, 0.66, 0.65, 0.58, 0.51,
            0.45, 0.44, 0.44, 0.43, 0.42, 0.42, 0.42, 0.41, 0.38, 0.2)
    return [{"address": POOL, "owner": "pool-owner", "pct": 30.0}] + [
        {"address": f"b{i}", "owner": f"bo{i}", "pct": pct} for i, pct in enumerate(bags)
    ]
