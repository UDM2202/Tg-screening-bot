"""Records prices after each decision and reports whether passing alerts beat the rest."""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections import defaultdict
from html import escape

from .config import TrackerConfig
from .db import Database
from .sources.dexscreener import DexScreener

log = logging.getLogger(__name__)

# A checkpoint taken this much later than planned (e.g. the bot was offline) is
# marked missed rather than recorded with a misleading price.
MAX_LATENESS_FRACTION = 0.25
MIN_LATENESS_SECONDS = 15 * 60

# Below this liquidity a pool is treated as dead even if a price is still quoted.
DEAD_LIQUIDITY_USD = 500


async def take_snapshots(db: Database, dex: DexScreener, cfg: TrackerConfig, now: float | None = None) -> int:
    now = now or time.time()
    statuses = ["alerted", "rejected"] if cfg.track_rejected else ["alerted"]
    due = db.due_snapshots(cfg.checkpoints, statuses, now)
    if not due:
        return 0
    on_time = []
    for addr, checkpoint, target in due:
        allowed = max(MIN_LATENESS_SECONDS, cfg.checkpoints[checkpoint] * MAX_LATENESS_FRACTION)
        if now - target > allowed:
            db.add_snapshot(addr, checkpoint, None, None, None, now, missed=True)
        else:
            on_time.append((addr, checkpoint))
    if not on_time:
        return 0
    markets = await dex.markets(sorted({addr for addr, _ in on_time}))
    for addr, checkpoint in on_time:
        m = markets.get(addr)
        if m is None:
            db.add_snapshot(addr, checkpoint, None, 0.0, None, now)
        else:
            db.add_snapshot(addr, checkpoint, m.price_usd, m.liquidity_usd, m.market_cap_usd, now)
    log.info("Recorded %d price checkpoints", len(on_time))
    return len(on_time)


def outcome_return(ref_price: float | None, price: float | None, liquidity: float | None) -> float | None:
    """Return as a fraction (0.5 = +50%). Dead pools count as -100%."""
    if not ref_price:
        return None
    if price is None or (liquidity is not None and liquidity < DEAD_LIQUIDITY_USD):
        return -1.0
    return price / ref_price - 1


# Alerts are split at this many RugCheck-detected insider wallets in /stats.
INSIDER_SPLIT = 20
INSIDER_WARNING = re.compile(r"(\d+) insider wallets detected")


def insider_count(details_json: str | None) -> int | None:
    """Insider wallets recorded for an alert. Older alerts only have it in their warnings."""
    details = json.loads(details_json or "{}")
    if details.get("insiders") is not None:
        return int(details["insiders"])
    if "warnings" not in details:
        return None
    for warning in details["warnings"]:
        match = INSIDER_WARNING.search(warning)
        if match:
            return int(match.group(1))
    return 0


ROW = "{:<15}{:>4}{:>8}{:>6}{:>6}{:>6}"
HEADER = ROW.format("", "n", "median", "up", "2x", "-50%")


def _row(label: str, returns: list[float]) -> str:
    if not returns:
        return ROW.format(label, 0, "–", "", "", "")
    n = len(returns)
    share = lambda hit: f"{sum(hit(r) for r in returns) / n * 100:.0f}%"  # noqa: E731
    med = f"{statistics.median(returns) * 100:+.0f}%"
    return ROW.format(label, n, med, share(lambda r: r > 0), share(lambda r: r >= 1), share(lambda r: r <= -0.5))


def build_report(db: Database, checkpoints: list[str], since: float = 0, title: str = "Tracker") -> str:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_reason: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_insiders: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in db.outcomes(since):
        r = outcome_return(row["ref_price"], row["price"], row["liquidity"])
        if r is None:
            continue
        groups[(row["status"], row["checkpoint"])].append(r)
        if row["status"] == "alerted":
            insiders = insider_count(row["details"])
            if insiders is not None:
                bucket = f"0-{INSIDER_SPLIT}" if insiders <= INSIDER_SPLIT else f"{INSIDER_SPLIT + 1}+"
                by_insiders[(bucket, row["checkpoint"])].append(r)
        if row["status"] == "rejected":
            for code in json.loads(row["reject_codes"] or "[]"):
                by_reason[(code, row["checkpoint"])].append(r)

    lines = [f"📊 <b>{escape(title)}</b>", "<pre>"]
    for cp in checkpoints:
        lines.append(f"[{cp}]" + HEADER[len(cp) + 2:])
        lines.append(_row("pass", groups[("alerted", cp)]))
        lines.append(_row("fail", groups[("rejected", cp)]))
        lines.append("")

    reasons = sorted({code for code, _ in by_reason})
    if reasons:
        # Shows whether each filter is catching losers. If coins rejected for a
        # reason keep doing well, that rule may be too strict.
        cp = max(
            (c for c in checkpoints if any(by_reason[(code, c)] for code in reasons)),
            key=checkpoints.index,
            default=checkpoints[0],
        )
        lines.append(f"Failed rule [{cp}]" + HEADER[len(cp) + 14:])
        for code in reasons:
            if by_reason[(code, cp)]:
                lines.append(_row(code[:15], by_reason[(code, cp)]))
    buckets = [f"0-{INSIDER_SPLIT}", f"{INSIDER_SPLIT + 1}+"]
    if by_insiders:
        # Many insider wallets can hide a coordinated holding that the top-10 rule misses.
        cp = max(
            (c for c in checkpoints if any(by_insiders[(b, c)] for b in buckets)),
            key=checkpoints.index,
            default=checkpoints[0],
        )
        lines.append("")
        lines.append(f"Alerts by insider wallets [{cp}]")
        lines.append(HEADER)
        for b in buckets:
            lines.append(_row(f"{b} insiders", by_insiders[(b, cp)]))
    lines.append("</pre>")
    lines.append("<i>pass = alerted, fail = passed the market filter but failed a rug check. "
                 "Returns are from the price at decision time; dead pools count as -100%.</i>")
    return "\n".join(lines)
