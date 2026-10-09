"""Paper trading: what a fixed stake in every alert would have made, after fees.

For 24 hours after each alert the bot notes when the price first reached the
take-profit level and when it first fell to the stop-loss level (both relative to
the alert price). Closed trades are then scored three ways: hold for 24h, sell at
the take-profit, or sell at whichever of take-profit and stop-loss came first.
"""

from __future__ import annotations

from html import escape

from .config import PaperConfig
from .db import Database
from .sources.dexscreener import Market
from .tracker import DEAD_LIQUIDITY_USD, outcome_return

WINDOW_HOURS = 24
HOLD_CHECKPOINT = "24h"


def record_marks(db: Database, markets: dict[str, Market], cfg: PaperConfig, now: float) -> None:
    """Note the first take-profit and stop-loss hits for alerts in their 24h window."""
    for row in db.paper_open(now - WINDOW_HOURS * 3600):
        ref = row["ref_price"]
        if not ref:
            continue
        m = markets.get(row["address"])
        dead = m is None or not m.price_usd or m.liquidity_usd < DEAD_LIQUIDITY_USD
        ret = -1.0 if dead else m.price_usd / ref - 1
        if row["paper_tp_at"] is None and ret >= cfg.take_profit_x - 1:
            db.set_paper_mark(row["address"], tp_at=now)
        if row["paper_stop_at"] is None and ret <= -cfg.stop_loss_pct / 100:
            # Record the price actually seen: a rug fills far worse than the stop level.
            db.set_paper_mark(row["address"], stop_at=now, stop_return=ret)


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


def report(db: Database, cfg: PaperConfig) -> str:
    rows = [r for r in db.paper_closed(HOLD_CHECKPOINT) if r["ref_price"]]
    fee = cfg.fee_pct / 100
    head = (
        f"📒 <b>Paper trading</b>: ${cfg.stake_usd:g} on every alert, {cfg.fee_pct:g}% fees per trade"
    )
    if not rows:
        return head + "\n\nNo closed trades yet. A trade closes 24h after its alert."

    names = {
        "hold": "Hold 24h",
        "tp": f"Sell at {cfg.take_profit_x:g}x",
        "both": f"{cfg.take_profit_x:g}x or stop -{cfg.stop_loss_pct:g}%",
    }
    lines = [head, f"Closed trades: {len(rows)} (alerts at least 24h old)", "<pre>"]
    lines.append(f"{'strategy':<17}{'P&L':>9}{'avg':>8}{'wins':>6}")
    for key, name in names.items():
        pnl = [cfg.stake_usd * (_strategy_returns(r, cfg)[key] - fee) for r in rows]
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
    lines.append(escape(
        "Prices are checked once a minute. Take-profits are counted at exactly the target; "
        "stop-losses at the price seen, so a rug counts as the full loss."
    ).join(["<i>", "</i>"]))
    return "\n".join(lines)


def _money(value: float) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"+${value:,.2f}"
