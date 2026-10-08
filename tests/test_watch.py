from screener.config import WatchConfig
from screener.sources.dexscreener import parse_market
from screener.watch import Watch, evaluate

from .fixtures import GOOD, pair

CFG = WatchConfig()


def market(price, liq=20_000, buys=5, sells=5, change=0.0):
    p = pair(GOOD, price=price, liq=liq, mcap=200_000 * price / 0.0002)
    p["txns"] = {"m5": {"buys": buys, "sells": sells}}
    p["priceChange"]["m5"] = change
    return parse_market(GOOD, [p])


def position():
    w = Watch(address=GOOD, symbol="GOOD", started_at=0)
    w.start_position(market(0.0002), now=0)
    return w


def test_jump_past_several_milestones_announces_only_the_highest():
    w = position()
    msgs = evaluate(w, market(0.0012), CFG, now=60)  # 6x
    assert len(msgs) == 1 and "hit 5x" in msgs[0]
    assert evaluate(w, market(0.0013), CFG, now=120) == []
    assert "hit 10x" in evaluate(w, market(0.0021), CFG, now=180)[0]


def test_dip_rearms_only_after_a_new_high():
    w = position()
    evaluate(w, market(0.0010), CFG, now=60)  # peak 5x
    assert "down 40% from its peak" in evaluate(w, market(0.0006), CFG, now=120)[0]
    assert evaluate(w, market(0.0005), CFG, now=180) == []  # still the same dip
    evaluate(w, market(0.0013), CFG, now=240)  # new high, 30% above the warned peak
    assert any("down" in m for m in evaluate(w, market(0.0009), CFG, now=300))


def test_sell_pressure_needs_volume_ratio_and_price_drop_and_cools_down():
    w = position()
    assert evaluate(w, market(0.0002, buys=1, sells=8, change=-20), CFG, now=60) == []  # too few sells
    assert evaluate(w, market(0.0002, buys=10, sells=20, change=-20), CFG, now=60) == []  # ratio 2
    assert evaluate(w, market(0.0002, buys=2, sells=30, change=-5), CFG, now=60) == []  # price barely down
    assert "30 sells vs 2 buys" in evaluate(w, market(0.0002, buys=2, sells=30, change=-15), CFG, now=60)[0]
    assert evaluate(w, market(0.0002, buys=2, sells=30, change=-15), CFG, now=600) == []  # cooldown
    assert evaluate(w, market(0.0002, buys=2, sells=30, change=-15), CFG, now=60 + 1800)


def test_rug_warning_for_unbought_watch_and_vanished_pool():
    w = Watch(address=GOOD, symbol="GOOD", started_at=0, max_liquidity=20_000)
    assert evaluate(w, market(0.0002, liq=15_000), CFG, now=60) == []
    assert evaluate(w, market(0.0009), CFG, now=60) == []  # price moves ignored when not bought
    msgs = evaluate(w, None, CFG, now=120)
    assert msgs and msgs[0].startswith("🚨 <b>$GOOD</b>: liquidity fell 100%")
    assert evaluate(w, None, CFG, now=180) == []
