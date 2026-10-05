"""End-to-end: discovery -> filters -> alert -> tracker, against mocked APIs."""

import asyncio
import json
import re
import time

import httpx
import pytest

from screener.app import Screener
from screener.config import Config
from screener.db import Database
from screener.tracker import take_snapshots

from .fixtures import GOOD, RUG, YOUNG, goplus_result, pair, rug_report


class FakeApis:
    def __init__(self):
        self.price = 0.0002
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "token-profiles/latest" in url:
            return httpx.Response(200, json=[
                {"chainId": "solana", "tokenAddress": addr, "links": []} for addr in (GOOD, RUG, YOUNG)
            ] + [{"chainId": "base", "tokenAddress": "0xabc"}])
        if "token-boosts/latest" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            addrs = url.rsplit("/", 1)[1].split(",")
            pairs = []
            for a in addrs:
                if a == GOOD:
                    pairs.append(pair(GOOD, price=self.price))
                elif a == RUG:
                    pairs.append(pair(RUG, symbol="RUG", price=self.price))
                elif a == YOUNG:
                    pairs.append(pair(YOUNG, symbol="YNG", age_min=3))
            return httpx.Response(200, json=pairs)
        if "rugcheck" in url:
            mint = re.search(r"/tokens/([^/]+)/report", url).group(1)
            return httpx.Response(200, json=rug_report(mint_auth="dev" if mint == RUG else None))
        if "gopluslabs" in url:
            return httpx.Response(200, json=goplus_result(request.url.params["contract_addresses"]))
        if "/swap/v1/quote" in url:
            amount = int(request.url.params["amount"])
            if request.url.params["inputMint"].startswith("So111"):
                return httpx.Response(200, json={"outAmount": str(amount * 1000)})
            return httpx.Response(200, json={"outAmount": str(int(amount / 1000 * 0.95))})
        if "api.telegram.org" in url:
            body = json.loads(request.content)
            if url.endswith("/sendMessage"):
                self.sent.append(body)
                return httpx.Response(200, json={"ok": True, "result": {}})
            if url.endswith("/getChatMemberCount"):
                return httpx.Response(200, json={"ok": True, "result": 1234})
        return httpx.Response(404)


@pytest.fixture
def setup():
    apis = FakeApis()
    cfg = Config()
    cfg.telegram_bot_token, cfg.telegram_chat_id = "123:abc", "42"
    db = Database(":memory:")
    return apis, cfg, db


async def test_full_cycle_alerts_only_the_clean_token(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        await s.run_cycle()

        assert db.status(GOOD) == "alerted"
        assert db.status(RUG) == "rejected"
        assert db.status(YOUNG) == "watching"  # too young, re-checked next cycle

        assert len(apis.sent) == 1
        alert = apis.sent[0]
        assert GOOD in alert["text"] and "$GOOD" in alert["text"]
        assert "TG (1,234)" in alert["text"]
        assert "round trip -5.0%" in alert["text"]
        labels = [b["text"] for row in alert["reply_markup"]["inline_keyboard"] for b in row]
        assert {"DexScreener", "RugCheck", "Solscan"} <= set(labels)

        # A second cycle must not alert the same token again.
        await s.run_cycle()
        assert len(apis.sent) == 1

        # Tracker: an hour later the price doubled.
        apis.price = 0.0004
        later = time.time() + 3601
        assert await take_snapshots(db, s.dex, cfg.tracker, now=later) == 2
        report = s.report()
        assert re.search(r"pass\s+1\s+\+100%", report), report
        assert "mint_authority" in report


async def test_pause_still_logs_but_does_not_send(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        assert "paused" in s.handle_command("/pause")
        await s.run_cycle()
        assert db.status(GOOD) == "alerted"
        assert apis.sent == []
        assert "Alerted: 1" in s.handle_command("/status")
        assert GOOD in s.handle_command("/recent")


async def test_api_outage_leaves_token_watching(setup, monkeypatch):
    apis, cfg, db = setup
    real_sleep = asyncio.sleep
    monkeypatch.setattr("screener.http.asyncio.sleep", lambda *_: real_sleep(0))  # skip backoff

    def flaky(request):
        if "rugcheck" in str(request.url):
            return httpx.Response(503)
        return apis(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        s = Screener(cfg, client, db)
        await s.run_cycle()
        assert db.status(GOOD) == "watching"
        assert apis.sent == []


async def test_late_checkpoints_are_marked_missed(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        await s.run_cycle()
        # Bot was offline: we're 25h past the decision. The 1h checkpoint is far too
        # late to be meaningful; the 24h one is within tolerance.
        assert await take_snapshots(db, s.dex, cfg.tracker, now=time.time() + 25 * 3600) == 2
        rows = db.conn.execute("SELECT checkpoint, missed FROM snapshots WHERE address = ?", (GOOD,))
        assert dict(rows.fetchall()) == {"1h": 1, "24h": 0}
        # Missed checkpoints are not counted as dead coins.
        assert re.search(r"\[1h\].*\npass\s+0", s.report()), s.report()
