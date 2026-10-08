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
