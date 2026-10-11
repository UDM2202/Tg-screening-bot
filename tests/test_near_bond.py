from datetime import datetime, timezone

from screener.config import NearBondConfig, Tier1Config
from screener.filters import near_bond_reasons, tier1
from screener.sources.pumpfun import bonding_progress, parse_coin
from screener.sources.rugcheck import parse_report

from .fixtures import CURVE_ACCOUNT, CURVE_TOKENS_ACCOUNT, curve_rug_report, pumpfun_coin

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def test_bonding_progress_from_reserves():
    assert bonding_progress({"real_token_reserves": 793_100_000 * 10**6}) == 0
    assert round(bonding_progress({"real_token_reserves": 79_310_000 * 10**6})) == 90
    assert bonding_progress({"real_token_reserves": 0}) == 100
    # Falls back to virtual reserves (which carry a 279.9M-token offset).
    assert round(bonding_progress({"virtual_token_reserves": (279_900_000 + 79_310_000) * 10**6})) == 90
    assert bonding_progress({}) is None


def test_bonding_progress_from_market_cap_in_sol():
    assert round(bonding_progress({"market_cap": 27.96})) == 0
    assert round(bonding_progress({"market_cap": 410.9})) == 100
    # Market cap wins over reserves that are zeroed or in another unit in the coin list.
    assert round(bonding_progress({"market_cap": 249.5, "real_token_reserves": 0})) == 90
    # A market cap that can't be in SOL (here, USD) is ignored in favour of the reserves.
    assert round(bonding_progress({"market_cap": 65_000, "real_token_reserves": 79_310_000 * 10**6})) == 90


def test_parse_coin_skips_graduated_coins():
    coin = parse_coin(pumpfun_coin())
    assert coin.symbol == "CURVE" and round(coin.progress_pct) == 90
    assert coin.curve_accounts == {CURVE_ACCOUNT, CURVE_TOKENS_ACCOUNT}
    assert abs(coin.price_usd - 65_000 / 1e9) < 1e-12
    assert parse_coin(pumpfun_coin(complete=True)) is None


def test_near_bond_market_filter():
    cfg = NearBondConfig()
    assert near_bond_reasons(parse_coin(pumpfun_coin()), cfg, NOW(), None) == []
    assert near_bond_reasons(parse_coin(pumpfun_coin(progress=60)), cfg, NOW(), None)
    assert near_bond_reasons(parse_coin(pumpfun_coin(age_min=3)), cfg, NOW(), None)
    assert near_bond_reasons(parse_coin(pumpfun_coin(mcap=400_000)), cfg, NOW(), None)
    reasons = near_bond_reasons(parse_coin(pumpfun_coin(mcap=60_000)), cfg, NOW(), 90_000)
    assert reasons == ["down 33% from its peak of $90,000"]


def test_rug_checks_adapted_for_the_curve():
    curve = {CURVE_ACCOUNT, CURVE_TOKENS_ACCOUNT}
    facts = parse_report(curve_rug_report(), extra_pool_accounts=curve)
    assert facts.max_holder_pct == 1.5  # the curve's 35% isn't a holder
    assert facts.lp_locked_pct is None
    on_curve = tier1(facts, None, None, Tier1Config(), on_curve=True)
    assert on_curve.passed, on_curve.rejects
    # The same coin judged as if it had a pool fails: no LP lock and an LP danger flag.
    codes = {c for c, _ in tier1(facts, None, None, Tier1Config()).rejects}
    assert {"lp_unlocked", "rugcheck_danger"} <= codes
    # Without excluding the curve account, its 35% would look like one huge holder.
    assert parse_report(curve_rug_report()).max_holder_pct == 35.0
