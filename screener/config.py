"""Loads config.yaml and environment variables into typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class DiscoveryConfig:
    poll_interval_seconds: int = 60
    dexscreener_profiles: bool = True
    dexscreener_boosts: bool = True
    pumpportal_migrations: bool = True
    helius_new_pools: bool = True
    helius_programs: list[str] = field(
        default_factory=lambda: ["raydium_amm_v4", "raydium_cpmm", "pumpswap", "meteora_dlmm"]
    )
    max_watchlist: int = 3000


@dataclass
class Tier2Config:
    min_liquidity_usd: float = 8000
    min_market_cap_usd: float = 30000
    max_market_cap_usd: float = 1_500_000
    min_liquidity_to_mcap_pct: float = 5
    min_age_minutes: float = 15
    max_age_hours: float = 24
    min_volume_to_liquidity: float = 1.0
    give_up_below_liquidity_usd: float = 2000
    give_up_after_minutes: float = 120
    require_any_social: bool = False


@dataclass
class Tier1Config:
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    min_lp_locked_pct: float = 90
    max_top10_holders_pct: float = 30
    max_single_holder_pct: float = 10
    max_dev_holding_pct: float = 5
    reject_creator_rug_history: bool = True
    reject_rugcheck_danger_risks: bool = True
    ignore_danger_risks: list[str] = field(default_factory=lambda: ["Low Liquidity"])
    reject_token2022_traps: bool = True
    require_sell_route: bool = True
    max_round_trip_loss_pct: float = 15
    sell_sim_sol_amount: float = 0.03
    sell_sim_slippage_bps: int = 1000
    use_goplus: bool = True


@dataclass
class SocialsConfig:
    min_telegram_members: int = 100
    warn_website_reuse: bool = True


@dataclass
class TrackerConfig:
    checkpoints: dict[str, int] = field(
        default_factory=lambda: {"1h": 3600, "24h": 86400, "7d": 604800}
    )
    interval_seconds: int = 300
    track_rejected: bool = True
    weekly_summary: bool = True


@dataclass
class ApiConfig:
    dexscreener: str = "https://api.dexscreener.com"
    rugcheck: str = "https://api.rugcheck.xyz"
    goplus: str = "https://api.gopluslabs.io"
    jupiter: str = "https://lite-api.jup.ag"
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    helius_ws: str = "wss://mainnet.helius-rpc.com"
    helius_rpc: str = "https://mainnet.helius-rpc.com"


@dataclass
class Config:
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    tier2: Tier2Config = field(default_factory=Tier2Config)
    tier1: Tier1Config = field(default_factory=Tier1Config)
    socials: SocialsConfig = field(default_factory=SocialsConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    apis: ApiConfig = field(default_factory=ApiConfig)
    database: str = "data/screener.db"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    jupiter_api_key: str = ""
    helius_api_key: str = ""


_SECTIONS = {
    "discovery": DiscoveryConfig,
    "tier2": Tier2Config,
    "tier1": Tier1Config,
    "socials": SocialsConfig,
    "tracker": TrackerConfig,
    "apis": ApiConfig,
}


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw = yaml.safe_load(Path(path).read_text()) or {}
    unknown = set(raw) - set(_SECTIONS) - {"database"}
    if unknown:
        raise ValueError(f"Unknown config sections: {sorted(unknown)}")

    cfg = Config()
    for name, cls in _SECTIONS.items():
        # Unknown keys raise TypeError, which catches typos in config.yaml.
        setattr(cfg, name, cls(**(raw.get(name) or {})))
    cfg.tracker.checkpoints = {str(k): int(v) for k, v in cfg.tracker.checkpoints.items()}
    cfg.database = raw.get("database", cfg.database)
    cfg.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    cfg.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    cfg.jupiter_api_key = os.getenv("JUPITER_API_KEY", "").strip()
    cfg.helius_api_key = os.getenv("HELIUS_API_KEY", "").strip()
    return cfg
