from datetime import datetime, timezone

from screener.config import Tier1Config, Tier2Config
from screener.filters import tier1, tier2
from screener.sources.dexscreener import parse_market
from screener.sources.goplus import parse_security
from screener.sources.jupiter import SellSim
from screener.sources.rugcheck import parse_report

from .fixtures import GOOD, goplus_result, pair, rug_report

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def market(**kw):
    return parse_market(GOOD, [pair(GOOD, **kw)])


def test_parse_market_combines_pools():
    old = pair(GOOD, age_min=300, liq=5_000, vol=1_000)
    deep = pair(GOOD, age_min=60, liq=20_000, mcap=250_000, vol=40_000)
    other_chain = {**pair(GOOD), "chainId": "base"}
    m = parse_market(GOOD, [old, deep, other_chain])
    assert m.liquidity_usd == 25_000
    assert m.volume_h24 == 41_000
    assert m.market_cap_usd == 250_000
    assert 299 < m.age_minutes(NOW()) < 301  # oldest pool decides age
    assert ("twitter", "https://x.com/goodtoken") in m.socials


def test_tier2_passes_healthy_market():
    r = tier2(market(), Tier2Config(), NOW())
    assert r.passed and not r.expired


def test_tier2_rejections():
    cfg = Tier2Config()
    assert not tier2(market(age_min=5), cfg, NOW()).passed
    assert not tier2(market(liq=7_000, mcap=100_000), cfg, NOW()).passed
    assert not tier2(market(mcap=2_000_000, liq=200_000, vol=300_000), cfg, NOW()).passed
    assert not tier2(market(liq=8_000, mcap=500_000), cfg, NOW()).passed  # 1.6% liq/mcap
    assert not tier2(market(vol=10_000), cfg, NOW()).passed  # volume < 1x liquidity
    expired = tier2(market(age_min=25 * 60), cfg, NOW())
    assert expired.expired and not expired.passed


def test_tier2_socials_optional():
    m = market(socials=False)
    assert tier2(m, Tier2Config(), NOW()).passed
    assert not tier2(m, Tier2Config(require_any_social=True), NOW()).passed


def test_rugcheck_parsing_ignores_pool_accounts():
    facts = parse_report(rug_report())
    assert facts.top10_pct == 20.0  # ten 2% wallets; the 25% pool account is skipped
    assert facts.max_holder_pct == 2.0
    assert facts.dev_pct == 1.0
    assert facts.lp_locked_pct == 100.0


def test_tier1_clean_token_passes():
    rug = parse_report(rug_report())
    gp = parse_security(goplus_result(GOOD)["result"][GOOD])
    r = tier1(rug, gp, SellSim(True, 4.0), Tier1Config())
    assert r.passed, r.rejects


def codes(**report_kw):
    return {c for c, _ in tier1(parse_report(rug_report(**report_kw)), None, SellSim(True, 4.0), Tier1Config()).rejects}


def test_tier1_hard_rejects():
    assert "mint_authority" in codes(mint_auth="someone")
    assert "freeze_authority" in codes(freeze_auth="someone")
    assert "lp_unlocked" in codes(lp_locked=40)
    assert "single_holder" in codes(holders=[{"address": "w", "owner": "w", "pct": 15.0}])
    assert "top10_holders" in codes(holders=[{"address": f"w{i}", "owner": f"w{i}", "pct": 5.0} for i in range(8)])
    assert "dev_holding" in codes(holders=[{"address": "d", "owner": "Creator1111111111111111111111111111111111111", "pct": 8.0}])
    assert "creator_history" in codes(risks=[{"name": "Creator history of rugged tokens", "level": "danger"}])
    assert "rugged" in codes(rugged=True)


def test_tier1_ignores_listed_danger_risks():
    assert "rugcheck_danger" not in codes(risks=[{"name": "Low Liquidity", "level": "danger"}])
    assert "rugcheck_danger" in codes(risks=[{"name": "Copycat token", "level": "danger"}])


def test_tier1_honeypot_and_token2022():
    rug = parse_report(rug_report())
    cfg = Tier1Config()
    assert not tier1(rug, None, SellSim(False, error="no sell route"), cfg).passed
    assert not tier1(rug, None, SellSim(True, 40.0), cfg).passed
    gp = parse_security(goplus_result(GOOD, transfer_fee={"current_fee_rate": {"fee_rate": "5"}})["result"][GOOD])
    assert gp.traps == ["transfer fee"]
    assert not tier1(rug, gp, SellSim(True, 4.0), cfg).passed


def test_goplus_mint_authority_is_respected():
    rug = parse_report(rug_report())
    gp = parse_security(goplus_result(GOOD, mintable={"status": "1"})["result"][GOOD])
    r = tier1(rug, gp, SellSim(True, 4.0), Tier1Config())
    assert ("mint_authority", "mint authority not revoked") in r.rejects


def test_goplus_normal_token_has_no_traps():
    # default_account_state "1" means Initialized (normal), not frozen.
    data = goplus_result(GOOD, default_account_state="1",
                         transfer_fee={"current_fee_rate": {"fee_rate": "0", "maximum_fee": "0"},
                                       "scheduled_fee_rate": [{"epoch": "0", "fee_rate": "0"}]})
    gp = parse_security(data["result"][GOOD])
    assert gp.traps == []


def test_goplus_real_traps_still_caught():
    frozen = parse_security(goplus_result(GOOD, default_account_state="2")["result"][GOOD])
    assert frozen.traps == ["new accounts frozen by default"]
    fee = parse_security(goplus_result(GOOD, transfer_fee={
        "current_fee_rate": {"fee_rate": "500", "maximum_fee": "1000"}})["result"][GOOD])
    assert fee.traps == ["transfer fee"]
    hook = parse_security(goplus_result(GOOD, transfer_hook=[{"address": "x"}])["result"][GOOD])
    assert hook.traps == ["transfer hook"]


def test_tier2_skips_coins_far_below_their_peak():
    m = market(mcap=100_000, liq=25_000, vol=100_000)
    cfg = Tier2Config()
    assert tier2(m, cfg, NOW()).passed
    assert tier2(m, cfg, NOW(), peak_mcap=180_000).passed  # 44% off the peak
    r = tier2(m, cfg, NOW(), peak_mcap=1_300_000)
    assert not r.passed and not r.expired
    assert "down 92% from its peak of $1,300,000" in r.reasons


def test_tier2_waits_out_an_active_dump():
    p = pair(GOOD)
    p["priceChange"]["m5"] = -33.0
    m = parse_market(GOOD, [p])
    r = tier2(m, Tier2Config(), NOW())
    assert not r.passed and not r.expired
    assert "dropped 33% in the last 5 minutes" in r.reasons
    p["priceChange"]["m5"] = -10.0
    assert tier2(parse_market(GOOD, [p]), Tier2Config(), NOW()).passed
