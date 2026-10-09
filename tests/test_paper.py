import re
import time

from screener import paper
from screener.config import PaperConfig
from screener.db import Database
from screener.sources.dexscreener import parse_market

from .fixtures import pair

CFG = PaperConfig()  # $5 stake, 5% fees, 2x take-profit, -50% stop


def market(addr, price, liq=20_000):
    return parse_market(addr, [pair(addr, price=price, liq=liq)])


def alert(db, addr, now, price=1.0):
    db.add_token(addr, "test", now)
    db.decide(addr, "alerted", price=price, mcap=100_000, liquidity=20_000, now=now)


def test_strategies_on_three_typical_coins():
    db = Database(":memory:")
    t0 = time.time() - 25 * 3600
    for a in ("pump", "rug", "fade"):
        alert(db, a, t0)

    # pump: 2.5x after 10 min, then fades to 1.2x at 24h.
    paper.record_marks(db, {"pump": market("pump", 2.5), "rug": market("rug", 0.9), "fade": market("fade", 0.8)},
                       CFG, t0 + 600)
    # rug: pool drained an hour in. fade: slides to -60%, then sits at 0.45x.
    paper.record_marks(db, {"pump": market("pump", 1.5), "fade": market("fade", 0.4)}, CFG, t0 + 3600)
    db.add_snapshot("pump", "24h", 1.2, 20_000, None)
    db.add_snapshot("rug", "24h", None, 0.0, None)
    db.add_snapshot("fade", "24h", 0.45, 5_000, None)

    report = paper.report(db, CFG)
    assert "Closed trades: 3" in report
    # Hold: +20% -100% -55% = -135% of $5, minus 3 x 5% fees = -$7.50
    assert re.search(r"Hold\ 24h\s+\-\$7\.50\s+\-\$2\.50\s+33%", report), report
    # Sell at 2x: +100% -100% -55% = -55% -> -$2.75 - $0.75
    assert re.search(r"Sell\ at\ 2x\s+\-\$3\.50\s+\-\$1\.17\s+33%", report), report
    # 2x or stop: +100%, rug counted at -100% (seen price), fade stopped at -60%
    assert re.search(r"2x\ or\ stop\ \-50%\s+\-\$3\.75\s+\-\$1\.25\s+33%", report), report
    assert "Reached 2x: 1/3 · Hit -50%: 2/3" in report


def test_stop_before_take_profit_counts_as_the_loss():
    db = Database(":memory:")
    t0 = time.time() - 25 * 3600
    alert(db, "a", t0)
    paper.record_marks(db, {"a": market("a", 0.45)}, CFG, t0 + 60)   # stop first
    paper.record_marks(db, {"a": market("a", 3.0)}, CFG, t0 + 120)   # then 3x
    db.add_snapshot("a", "24h", 3.0, 20_000, None)
    report = paper.report(db, CFG)
    assert re.search(r"Hold\ 24h\s+\+\$9\.75", report), report  # 200% - 5%
    assert re.search(r"Sell\ at\ 2x\s+\+\$4\.75", report), report  # 100% - 5%
    assert re.search(r"2x\ or\ stop\ \-50%\s+\-\$3\.00", report), report  # stopped at -55%, minus 5%


def test_no_closed_trades_yet():
    db = Database(":memory:")
    assert "No closed trades yet" in paper.report(db, CFG)
