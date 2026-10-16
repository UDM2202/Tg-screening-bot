"""Paper trading: what a fixed stake in every alert would have made, after fees.

For 24 hours after each alert the bot notes when the price first reached the
take-profit level and when it first fell to the stop-loss level (both relative to
the alert price). Closed trades are then scored three ways: hold for 24h, sell at
the take-profit, or sell at whichever of take-profit and stop-loss came first.

It also notes when the price first reached each of EXIT_TARGETS, so /paper can
compare exit targets (1.5x vs 2x vs 3x, and selling half at each of two targets).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from html import escape

from .config import PaperConfig
from .db import Database
from .sources.dexscreener import Market
from .tracker import DEAD_LIQUIDITY_USD, outcome_return

WINDOW_HOURS = 24
HOLD_CHECKPOINT = "24h"
# When this bot first started recording paper marks. Earlier alerts have no marks.
STARTED_KEY = "paper_started_at"
# Multiples of the alert price whose first hit is recorded, to compare exit targets.
EXIT_TARGETS = (1.5, 2.0, 3.0)
# When the bot first recorded hits for EXIT_TARGETS. Earlier alerts can't be compared.
TARGETS_STARTED_KEY = "paper_targets_started_at"


def record_marks(db: Database, markets: dict[str, Market], cfg: PaperConfig, now: float) -> None:
    """Note the first take-profit and stop-loss hits for alerts in their 24h window."""
    for key in (STARTED_KEY, TARGETS_STARTED_KEY):
        if db.get_meta(key) is None:
            db.set_meta(key, str(now))
    for row in db.paper_open(now - WINDOW_HOURS * 3600):
        ref = row["ref_price"]
        if not ref:
            continue
        m = markets.get(row["address"])
        if m is None and row["kind"] == "near_bond":
            continue  # a curve coin DexScreener hasn't listed yet: unknown, not dead
        dead = m is None or not m.price_usd or (not m.on_curve and m.liquidity_usd < DEAD_LIQUIDITY_USD)
        ret = -1.0 if dead else m.price_usd / ref - 1
        if row["paper_tp_at"] is None and ret >= cfg.take_profit_x - 1:
            db.set_paper_mark(row["address"], tp_at=now)
        hits = _hits(row)
        new = {_key(t): now for t in EXIT_TARGETS if _key(t) not in hits and ret >= t - 1}
        if new:
            db.set_paper_hits(row["address"], {**hits, **new})
        if row["paper_stop_at"] is None and ret <= -cfg.stop_loss_pct / 100:
            # Record the price actually seen: a rug fills far worse than the stop level.
            db.set_paper_mark(row["address"], stop_at=now, stop_return=ret)


def _key(target: float) -> str:
    return f"{target:g}"


def _hits(row) -> dict[str, float]:
    return json.loads(row["paper_hits"] or "{}")


def _held(row) -> float:
    return outcome_return(row["ref_price"], row["price"], row["liquidity"])


def _sell_at(target: float) -> Callable:
    """Sell everything at the target if it was reached, otherwise hold 24h."""
    def ret(row) -> float:
        return target - 1 if _key(target) in _hits(row) else _held(row)
    return ret


def _target_or_stop(target: float) -> Callable:
    """Sell at the target or the stop-loss, whichever came first; otherwise hold 24h."""
    def ret(row) -> float:
        hit_at, stop_at = _hits(row).get(_key(target)), row["paper_stop_at"]
        if hit_at is not None and (stop_at is None or hit_at <= stop_at):
            return target - 1
        if stop_at is not None:
            return row["paper_stop_return"]
        return _held(row)
    return ret


def _split(first: float, second: float) -> Callable:
    """Sell half at the first target and the rest at the second; whatever isn't sold is held 24h."""
    def ret(row) -> float:
        hits = _hits(row)
        if _key(first) not in hits:
            return _held(row)
        rest = second - 1 if _key(second) in hits else _held(row)
        return (first - 1) / 2 + rest / 2
    return ret


EXIT_STRATEGIES: dict[str, Callable] = {
    "Hold 24h": _held,
    "Sell at 1.5x": _sell_at(1.5),
    "Sell at 2x": _sell_at(2.0),
    "Sell at 3x": _sell_at(3.0),
    "1.5x or stop": _target_or_stop(1.5),
    "½ at 1.5x, ½ at 3x": _split(1.5, 3.0),
}


def _table(group, strategies: dict[str, Callable], cfg: PaperConfig) -> list[str]:
    fee = cfg.fee_pct / 100
    lines = ["<pre>", f"{'strategy':<19}{'P&L':>9}{'avg':>8}{'wins':>6}"]
    for name, fn in strategies.items():
        pnl = [cfg.stake_usd * (fn(r) - fee) for r in group]
        total = sum(pnl)
        wins = sum(p > 0 for p in pnl) / len(pnl) * 100
        lines.append(f"{escape(name[:19]):<19}{_money(total):>9}{_money(total / len(pnl)):>8}{wins:>5.0f}%")
    lines.append("</pre>")
    return lines


def _exit_targets_section(rows, db: Database, cfg: PaperConfig) -> list[str]:
    started = db.get_meta(TARGETS_STARTED_KEY)
    if started is None:
        return []
    group = [r for r in rows if r["decided_at"] >= float(started)]
    head = "<b>Exit targets</b>"
    if not group:
        return ["", head, "Comparing 1.5x, 2x and 3x targets for alerts from now on. "
                "The first results show 24h after the next alert."]
    reached = " · ".join(
        f"{t:g}x: {sum(_key(t) in _hits(r) for r in group)}/{len(group)}" for t in EXIT_TARGETS
    )
    return ["", f"{head} ({len(group)} alerts)", *_table(group, EXIT_STRATEGIES, cfg),
            f"Reached {reached}",
            escape(f"Stop is -{cfg.stop_loss_pct:g}%. A target not reached means holding to 24h; "
                   "for ½ and ½, the unsold half is held.").join(["<i>", "</i>"])]


def _strategy_returns(row, cfg: PaperConfig) -> dict[str, float]:
    held = outcome_return(row["ref_price"], row["price"], row["liquidity"])
    tp = cfg.take_profit_x - 1
    tp_at, stop_at = row["paper_tp_at"], row["paper_stop_at"]
    take_profit = tp if tp_at is not None else held
    if tp_at is not None and (stop_at is None or tp_at <= stop_at):
        both = tp
    elif stop_at is not None:
        both = row["paper_stop_return"]
    else:
        both = held
    return {"hold": held, "tp": take_profit, "both": both}


def report(db: Database, cfg: PaperConfig, since: float | None = None) -> str:
    """since: only alerts sent after this time (for /paper new); None = all tracked alerts."""
    started = float(db.get_meta(STARTED_KEY) or "inf")
    closed = [r for r in db.paper_closed(HOLD_CHECKPOINT) if r["ref_price"]]
    if since is not None:
        closed = [r for r in closed if r["decided_at"] >= since]
    # Only alerts whose whole window was tracked; older ones would look like pure holds.
    rows = [r for r in closed if r["decided_at"] >= started]
    skipped = len(closed) - len(rows)
    fee = cfg.fee_pct / 100
    head = (
        f"📒 <b>Paper trading</b>: ${cfg.stake_usd:g} on every alert, {cfg.fee_pct:g}% fees per trade"
    )
    if since is not None:
        head += "\nOnly alerts since the filter rules last changed"
    note = (
        f"\n{skipped} older alert(s) skipped: they were sent before paper tracking started."
        if skipped else ""
    )
    if not rows:
        return head + "\n\nNo closed trades yet. A trade closes 24h after its alert." + note

    names = {
        "hold": "Hold 24h",
        "tp": f"Sell at {cfg.take_profit_x:g}x",
        "both": f"{cfg.take_profit_x:g}x or stop -{cfg.stop_loss_pct:g}%",
    }
    lines = [head, f"Closed trades: {len(rows)} (alerts at least 24h old){note}"]
    sections = [("All alerts", rows)]
    near = [r for r in rows if r["kind"] == "near_bond"]
    if near:  # compare the two alert types once near-bond trades exist
        sections += [("Graduated", [r for r in rows if r["kind"] != "near_bond"]), ("⚡ Near bond", near)]
    for title, group in sections:
        if not group:
            continue
        if len(sections) > 1:
            lines.append(f"<b>{title}</b> ({len(group)})")
        lines.append("<pre>")
        lines.append(f"{'strategy':<17}{'P&L':>9}{'avg':>8}{'wins':>6}")
        for key, name in names.items():
            pnl = [cfg.stake_usd * (_strategy_returns(r, cfg)[key] - fee) for r in group]
            total = sum(pnl)
            wins = sum(p > 0 for p in pnl) / len(pnl) * 100
            lines.append(
                f"{name[:17]:<17}{_money(total):>9}{_money(total / len(pnl)):>8}{wins:>5.0f}%"
            )
        lines.append("</pre>")
    tp_hits = sum(r["paper_tp_at"] is not None for r in rows)
    stop_hits = sum(r["paper_stop_at"] is not None for r in rows)
    lines.append(
        f"Reached {cfg.take_profit_x:g}x: {tp_hits}/{len(rows)} · "
        f"Hit -{cfg.stop_loss_pct:g}%: {stop_hits}/{len(rows)}"
    )
    lines += _exit_targets_section(rows, db, cfg)
    lines.append(escape(
        "Prices are checked once a minute. Take-profits are counted at exactly the target; "
        "stop-losses at the price seen, so a rug counts as the full loss."
    ).join(["<i>", "</i>"]))
    return "\n".join(lines)


def _money(value: float) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"+${value:,.2f}"
