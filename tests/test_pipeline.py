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
        self.liq = 20_000
        self.txns_m5 = (5, 5, 0.0)  # buys, sells, price change %
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
                    p = pair(GOOD, price=self.price, liq=self.liq, mcap=200_000 * self.price / 0.0002)
                    buys, sells, change = self.txns_m5
                    p["txns"] = {"m5": {"buys": buys, "sells": sells}}
                    p["priceChange"]["m5"] = change
                    pairs.append(p)
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
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 100 + len(self.sent)}})
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
        assert re.search(r"Alerts by insider wallets \[1h\]\n\s+n.*\n0-20 insiders\s+1\s+\+100%", report), report
        assert re.search(r"Alerts by market cap at alert \[1h\]\n\s+n.*\n(.*\n){1}\$100k-\$300k\s+1\s+\+100%", report), report
        assert re.search(r"24h volume vs liquidity \[1h\]\n\s+n.*\n.*\nvol 2-5x liq\s+1\s+\+100%", report), report
        assert re.search(r"price change in the hour before \[1h\]\n\s+n.*\n.*\nup 0-100%\s+1\s+\+100%", report), report
        assert "waiting for their checkpoint: 1h: 0 · 24h: 1 · 7d: 1" in report
        rejects = s.handle_command("/rejects")
        assert "Rejected coins: 1" in rejects
        assert "mint_authority: 1 (100%)" in rejects
        assert "$RUG: mint authority not revoked" in rejects


async def test_pause_still_logs_but_does_not_send(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        assert "paused" in s.handle_command("/pause")
        await s.run_cycle()
        assert db.status(GOOD) == "alerted"
        assert apis.sent == []
        assert "Alerted: 1" in s.handle_command("/status")
        status = s.handle_command("/status")
        assert "Last scan:" in status and "took" in status
        assert "Coin age at alert (last 1): median 2h 0m" in status
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
        assert "missed while the bot was offline: 1h: 1" in s.report()
        # Missed checkpoints are not counted as dead coins.
        assert re.search(r"\[1h\].*\npass\s+0", s.report()), s.report()


async def test_rug_warning_replies_to_alert_for_every_alerted_coin(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        await s.run_cycle()
        alert_id = 101  # first message sent
        apis.liq = 8_000  # liquidity pulled by 60%
        await s.check_watches()
        warning = apis.sent[-1]
        assert warning["text"].startswith("🚨 <b>$GOOD</b>: liquidity fell 60%")
        assert warning["reply_parameters"]["message_id"] == alert_id
        await s.check_watches()
        assert len(apis.sent) == 2  # sent once only


async def test_bought_by_reply_then_milestones_dip_and_sold(setup):
    apis, cfg, db = setup
    cfg.watch.watch_all_alerts = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        await s.run_cycle()
        alert_id = 101

        # Not bought yet: price moves only matter for coins you hold.
        apis.price = 0.0005
        await s.check_watches()
        assert len(apis.sent) == 1

        reply = await s.handle_message("/bought", reply_to=alert_id)
        assert reply.startswith("✅ Watching $GOOD from MC $500.0k")

        apis.price = 0.0016  # 3.2x from the /bought price
        await s.check_watches()
        assert "🚀 <b>$GOOD</b> hit 2x since you bought (MC $500.0k → $1.60M)" in apis.sent[-1]["text"]
        assert apis.sent[-1]["reply_parameters"]["message_id"] == alert_id

        apis.price = 0.0010  # 37.5% below the 0.0016 peak
        await s.check_watches()
        assert "⚠️ <b>$GOOD</b> is down 38% from its peak (MC $1.60M → $1.00M). Now 2.0x" in apis.sent[-1]["text"]
        count = len(apis.sent)
        await s.check_watches()
        assert len(apis.sent) == count  # same dip isn't repeated

        apis.txns_m5 = (4, 40, -20.0)
        await s.check_watches()
        assert "🔻 <b>$GOOD</b>: 40 sells vs 4 buys in the last 5 min, price -20%" in apis.sent[-1]["text"]

        positions = await s.handle_message("/positions")
        assert "$GOOD · 2.00x · MC $500.0k → $1.00M (peak $1.60M)" in positions

        assert await s.handle_message("/sold $good") == "Stopped watching $GOOD."
        apis.price = 0.01
        count = len(apis.sent)
        await s.check_watches()
        assert len(apis.sent) == count
        assert (await s.handle_message("/positions")).startswith("No positions")


async def test_bought_needs_a_coin(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        assert (await s.handle_message("/bought")).startswith("Which coin?")
        assert (await s.handle_message("/bought $NOPE")).startswith("Which coin?")
        # Coins the bot never alerted can still be watched by address.
        assert (await s.handle_message(f"/bought {GOOD}")).startswith("✅ Watching $GOOD")


async def test_every_alert_gets_follow_ups_by_default_and_mute_stops_them(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        await s.run_cycle()
        alert_id = 101

        apis.price = 0.0005  # 2.5x the alert price, never /bought
        await s.check_watches()
        text = apis.sent[-1]["text"]
        assert text == "🚀 <b>$GOOD</b> hit 2x since the alert (MC $200.0k → $500.0k)."
        assert apis.sent[-1]["reply_parameters"]["message_id"] == alert_id

        apis.price = 0.0003  # 40% off the peak
        await s.check_watches()
        assert "down 40% from its peak (MC $500.0k → $300.0k). Now 1.5x vs the alert price." in apis.sent[-1]["text"]

        assert await s.handle_message("/mute", reply_to=alert_id) == "🔕 Muted $GOOD."
        count = len(apis.sent)
        apis.price = 0.0030
        apis.liq = 1_000
        await s.check_watches()
        assert len(apis.sent) == count

        # /pause silences follow-ups on alerts too.
        db.conn.execute("UPDATE watches SET active = 1")
        s.handle_command("/pause")
        await s.check_watches()
        assert len(apis.sent) == count


async def test_coin_alerting_only_after_a_dump_is_skipped(setup):
    """GTA6 pattern: pumped on tiny volume, dumped 90%, and the dump's volume made it pass."""
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        apis.price = 0.0013  # MC $1.3M, but volume is too thin to pass
        real_pair = pair

        def thin(address, **kw):
            return real_pair(address, **{**kw, "vol": 5_000})

        import tests.test_pipeline as tp
        tp.pair = thin
        try:
            await s.run_cycle()
        finally:
            tp.pair = real_pair
        assert db.status(GOOD) == "watching"

        apis.price = 0.0001  # dumped to MC $100k, with plenty of volume now
        await s.run_cycle()
        assert db.status(GOOD) == "watching"
        assert not any(GOOD in m["text"] for m in apis.sent)


async def test_alert_warns_when_somewhat_below_peak(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        db.add_token(GOOD, "test")
        db.touch(GOOD, mcap=300_000)  # seen earlier at $300k, now $200k
        await s.run_cycle()
        alert = next(m["text"] for m in apis.sent if GOOD in m["text"])
        assert "⚠️ 33% below its peak MC of $300.0k" in alert


async def test_watch_loop_records_paper_marks_even_when_muted(setup):
    apis, cfg, db = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis)) as client:
        s = Screener(cfg, client, db)
        await s.run_cycle()
        await s.handle_message("/mute", reply_to=101)
        apis.price = 0.0005  # 2.5x the alert price
        await s.check_watches()
        row = db.conn.execute("SELECT paper_tp_at, paper_stop_at FROM tokens WHERE address = ?", (GOOD,)).fetchone()
        assert row["paper_tp_at"] is not None and row["paper_stop_at"] is None
        assert "No closed trades yet" in await s.handle_message("/paper")
