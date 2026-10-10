import httpx

from screener import usage


async def test_http_downloads_are_counted_per_host():
    usage.downloaded.clear()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 5000))
    hooks = {"response": [usage.count_response]}
    async with httpx.AsyncClient(transport=transport, event_hooks=hooks) as client:
        resp = await client.get("https://api.dexscreener.com/tokens")
        assert len(resp.content) == 5000  # body still readable after the hook
        await client.get("https://api.dexscreener.com/tokens")
    usage.add("helius websocket", 3 * 1024 * 1024)
    assert usage.downloaded["api.dexscreener.com"] == 10000
    report = usage.report()
    assert report.startswith("Data used since start: 3.0 MB")
    assert "helius websocket: 3.0 MB" in report
    assert "api.dexscreener.com: 10 KB" in report


def test_stats_labels_are_escaped_for_telegram_html():
    from screener.db import Database
    from screener.tracker import build_report
    db = Database(":memory:")
    db.add_token("a", "t")
    db.decide("a", "alerted", price=1, mcap=50_000, liquidity=1, details={"vol_liq": 1.2})
    db.add_snapshot("a", "1h", 1.1, 5000, None)
    report = build_report(db, ["1h", "24h", "7d"])
    assert "vol &lt;2x liq" in report
    # Only the tags we mean to send may appear.
    import re
    assert set(re.findall(r"</?(\w+)", report)) <= {"b", "pre", "i"}


async def test_message_with_broken_html_is_resent_as_plain_text():
    import json
    from screener.telegram import Telegram
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if body.get("parse_mode") == "HTML":
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tg = Telegram(client, "1:a", "42")
        assert await tg.send("<b>hi</b> vol <2x liq &amp; more") == 7
    assert sent[-1]["text"] == "hi vol <2x liq & more"
    assert "parse_mode" not in sent[-1]
