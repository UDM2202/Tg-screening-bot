"""Socials soft score. Socials are easy to fake, so this only adds context and warnings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .config import SocialsConfig
from .db import Database
from .sources.dexscreener import Market
from .telegram import Telegram

TG_USERNAME = re.compile(r"^/(?:s/)?([A-Za-z0-9_]{5,32})/?$")


@dataclass
class SocialsReport:
    score: int = 0
    max_score: int = 4
    website: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    telegram_members: int | None = None
    warnings: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def classify_links(market: Market, extra_links: list[dict] | None = None) -> SocialsReport:
    """Pick one website, X and Telegram link from DexScreener pair info and profile links."""
    links = list(market.socials) + [("website", w) for w in market.websites]
    for link in extra_links or []:
        if link.get("url"):
            links.append(((link.get("type") or link.get("label") or "").lower(), link["url"]))

    report = SocialsReport()
    for kind, url in links:
        host = _host(url)
        if not host:
            continue
        if kind in ("twitter", "x") or host in ("x.com", "twitter.com"):
            report.twitter = report.twitter or url
        elif kind == "telegram" or host in ("t.me", "telegram.me"):
            report.telegram = report.telegram or url
        elif kind in ("website", "") and host not in ("dexscreener.com", "pump.fun"):
            report.website = report.website or url
    return report


def telegram_username(url: str) -> str | None:
    if _host(url) not in ("t.me", "telegram.me"):
        return None
    match = TG_USERNAME.match(urlparse(url).path)
    if not match or match.group(1).lower() in ("joinchat", "addlist"):
        return None
    return match.group(1)


async def assess(
    market: Market,
    extra_links: list[dict] | None,
    db: Database,
    tg: Telegram,
    cfg: SocialsConfig,
) -> SocialsReport:
    report = classify_links(market, extra_links)

    if report.website:
        report.score += 1
        if cfg.warn_website_reuse:
            reused = db.record_websites(market.address, [report.website])[report.website]
            if reused:
                report.warnings.append(f"website already used by {reused} other token(s)")
    if report.twitter:
        report.score += 1
        if "/status/" in report.twitter or "/communities/" in report.twitter:
            report.warnings.append("X link is a post/community, not an account")
    if report.telegram:
        report.score += 1
        username = telegram_username(report.telegram)
        if username:
            report.telegram_members = await tg.member_count(username)
        if report.telegram_members is not None:
            if report.telegram_members >= cfg.min_telegram_members:
                report.score += 1
            else:
                report.warnings.append(f"Telegram has only {report.telegram_members} members")

    if not (report.website or report.twitter or report.telegram):
        report.warnings.append("no socials at all")
    return report
