import httpx

from screener.config import Tier2Config
from screener.filters import tier2
from screener.sources.dexscreener import parse_market
from screener.sources.helius import HeliusPools, is_pool_creation, new_token_mints

from .fixtures import GOOD, pair

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP52"
WSOL = "So11111111111111111111111111111111111111112"
LP_MINT = "LpMint1111111111111111111111111111111111111"
KEY = "secret-key-123"


def pool_tx(err=None):
    return {
        "meta": {
            "err": err,
            "preTokenBalances": [{"mint": GOOD}, {"mint": WSOL}],
            "postTokenBalances": [{"mint": GOOD}, {"mint": WSOL}, {"mint": LP_MINT}],
        }
    }


def test_pool_creation_log_matching():
    assert is_pool_creation(PUMPSWAP, ["Program log: Instruction: CreatePool"])
    assert not is_pool_creation(PUMPSWAP, ["Program log: Instruction: Buy"])
    assert is_pool_creation(CPMM, ["Program log: Instruction: Initialize"])
    # Token program logs inside the same transaction must not match.
    assert not is_pool_creation(CPMM, ["Program log: Instruction: InitializeAccount3"])


def test_new_token_mints_skips_quote_and_lp_mints():
    assert new_token_mints(pool_tx()) == [GOOD]
    assert new_token_mints(pool_tx(err={"InstructionError": [0, "x"]})) == []
    assert new_token_mints({}) == []


async def test_notification_to_watchlist():
    found = []

    async def on_token(mint, dex):
        found.append((mint, dex))

    def rpc(request):
        assert KEY in str(request.url)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": pool_tx()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc)) as client:
        h = HeliusPools(client, KEY, "wss://x", "https://x", on_token, ["pumpswap"])
        assert h.programs == [PUMPSWAP]
        h._pending[1] = PUMPSWAP
        await h.handle_message({"jsonrpc": "2.0", "id": 1, "result": 77})
        assert h.subscriptions == {77: PUMPSWAP}

        def note(logs, err=None):
            return {"method": "logsNotification", "params": {"subscription": 77, "result": {
                "value": {"signature": "sig1", "err": err, "logs": logs}}}}

        await h.handle_message(note(["Program log: Instruction: Buy"]))
        await h.handle_message(note(["Program log: Instruction: CreatePool"], err={"x": 1}))
        assert h.queue.empty()

        await h.handle_message(note(["Program log: Instruction: CreatePool"]))
        sig, program = h.queue.get_nowait()
        await h.resolve(sig, program)
        assert found == [(GOOD, "pumpswap")]


def test_api_key_is_redacted_from_logs():
    h = HeliusPools(None, KEY, "wss://x", "https://x", None)
    assert KEY not in h._redact(f"server rejected wss://x/?api-key={KEY}")


def test_tier2_gives_up_on_thin_old_pools():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    thin = parse_market(GOOD, [pair(GOOD, age_min=150, liq=1_000, mcap=10_000)])
    assert tier2(thin, Tier2Config(), now).expired
    young_thin = parse_market(GOOD, [pair(GOOD, age_min=30, liq=1_000, mcap=10_000)])
    assert not tier2(young_thin, Tier2Config(), now).expired
