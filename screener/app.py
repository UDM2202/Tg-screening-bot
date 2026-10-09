"""Main loop: discover -> pre-filter -> rug checks -> alert, plus the tracker and commands."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from html import escape

import httpx

from . import socials as socials_mod
from . import paper, usage
from .alerts import age_text, format_alert, usd
from .config import Config
from .db import Database
from .filters import tier1, tier2
from .http import ApiError
from .sources.dexscreener import DexScreener, Market
from .sources.goplus import GoPlus
from .sources.helius import HeliusPools
from .sources.jupiter import Jupiter
from .sources.pumpportal import stream_migrations
from .sources.rugcheck import RugCheck
from .telegram import Telegram
from .tracker import build_report, take_snapshots
from .watch import Watch, evaluate

log = logging.getLogger(__name__)

WEEK = 7 * 86400
SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
HELP = (
    "/status – watchlist and decision counts\n"
    "/stats – how alerts performed vs rejected coins (all time)\n"
    "/week – same, last 7 days\n"
    "/recent – last 10 alerts\n"
    "/rejects – which rug checks reject coins most, with examples\n"
    "/bought – reply to an alert (or add the address or $SYMBOL) to watch your position\n"
    "/sold – stop watching a coin (reply, address or $SYMBOL)\n"
    "/mute – stop follow-ups for an alert (reply to it)\n"
    "/positions – coins you're holding and how they're doing\n"
    "/paper – would $5 on every alert have made money? (after fees)\n"
    "/why – why a coin was or wasn't alerted (address, $SYMBOL, or reply)\n"
    "/pause – stop sending alerts (screening and tracking continue)\n"
    "/resume – start sending alerts again"
)


class Screener:
    def __init__(self, cfg: Config, client: httpx.AsyncClient, db: Database):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.dex = DexScreener(client, cfg.apis.dexscreener)
        self.rugcheck = RugCheck(client, cfg.apis.rugcheck)
        self.goplus = GoPlus(client, cfg.apis.goplus)
        self.jupiter = Jupiter(client, cfg.apis.jupiter, cfg.jupiter_api_key)
        self.tg = Telegram(client, cfg.telegram_bot_token, cfg.telegram_chat_id)
        # Profile links (socials) seen at discovery, keyed by token address.
        self.profile_links: dict[str, list[dict]] = {}
        self.rug_check_slots = asyncio.Semaphore(3)

    # --- discovery -------------------------------------------------------

    async def discover(self) -> int:
        found = 0
        sources = []
        if self.cfg.discovery.dexscreener_profiles:
            sources.append(("profile", self.dex.latest_profiles))
        if self.cfg.discovery.dexscreener_boosts:
            sources.append(("boost", self.dex.latest_boosts))
        for name, fetch in sources:
            try:
                items = await fetch()
            except ApiError as exc:
                log.warning("Discovery via %s failed: %s", name, exc)
                continue
            for item in items:
                addr = item["tokenAddress"]
                if item.get("links"):
                    if len(self.profile_links) > 5000:
                        self.profile_links.clear()
                    self.profile_links[addr] = item["links"]
                found += self.db.add_token(addr, name)
        return found

    async def on_migration(self, mint: str) -> None:
        if self.db.add_token(mint, "pumpportal"):
            log.debug("New migration: %s", mint)

    async def on_new_pool(self, mint: str, dex: str) -> None:
        if self.db.add_token(mint, f"helius:{dex}"):
            log.debug("New %s pool: %s", dex, mint)

    # --- screening -------------------------------------------------------

    async def screen_watchlist(self) -> None:
        now = datetime.now(timezone.utc)
        max_age = self.cfg.tier2.max_age_hours * 3600
        self.db.expire_unseen(time.time() - max_age)

        addresses = self.db.watching(self.cfg.discovery.max_watchlist)
        if not addresses:
            return
        markets = await self.dex.markets(addresses)

        passed: list[Market] = []
        for addr in addresses:
            market = markets.get(addr)
            if market is None:
                continue  # no pool yet; retried until expire_unseen drops it
            peak = self.db.touch(addr, market.symbol, market.name, market.market_cap_usd)
            result = tier2(market, self.cfg.tier2, now, peak)
            self.db.set_last_reasons(addr, result.reasons)
            if result.expired:
                self.db.set_expired(addr)
            elif result.passed:
                passed.append(market)

        log.info("Watchlist %d, with pools %d, passed market filter %d", len(addresses), len(markets), len(passed))
        await asyncio.gather(*(self.check_token(m) for m in passed))

    async def check_token(self, market: Market) -> None:
        async with self.rug_check_slots:
            try:
                await self._check_token(market)
            except ApiError as exc:
                log.warning("Rug checks unavailable for %s, retrying next cycle: %s", market.symbol, exc)
            except Exception:
                log.exception("Unexpected error checking %s", market.address)

    async def _check_token(self, market: Market) -> None:
        t1 = self.cfg.tier1
        rug = await self.rugcheck.report(market.address)
        goplus = None
        if t1.use_goplus:
            try:
                goplus = await self.goplus.security(market.address)
            except ApiError as exc:
                # GoPlus is a second opinion; RugCheck already covered the core checks.
                log.info("GoPlus unavailable for %s: %s", market.symbol, exc)
        sell = None
        if t1.require_sell_route:
            sell = await self.jupiter.round_trip(market.address, t1.sell_sim_sol_amount, t1.sell_sim_slippage_bps)

        result = tier1(rug, goplus, sell, t1)
        decision = dict(
            price=market.price_usd, mcap=market.market_cap_usd, liquidity=market.liquidity_usd
        )
        if not result.passed:
            log.info("Rejected %s: %s", market.symbol, "; ".join(msg for _, msg in result.rejects))
            self.db.decide(
                market.address,
                "rejected",
                reject_codes=sorted({code for code, _ in result.rejects}),
                details={"reasons": [msg for _, msg in result.rejects]},
                **decision,
            )
            return

        peak = self.db.touch(market.address, mcap=market.market_cap_usd)
        if peak and market.market_cap_usd and market.market_cap_usd < peak * 0.8:
            drop = (1 - market.market_cap_usd / peak) * 100
            result.warnings.append(f"{drop:.0f}% below its peak MC of {usd(peak)}")

        socials = await socials_mod.assess(
            market, self.profile_links.pop(market.address, None), self.db, self.tg, self.cfg.socials
        )
        text, buttons = format_alert(market, rug, goplus, sell, socials, result, datetime.now(timezone.utc))
        self.db.decide(
            market.address,
            "alerted",
            details={
                "warnings": result.warnings + socials.warnings,
                "socials": socials.score,
                "age_minutes": market.age_minutes(datetime.now(timezone.utc)),
                "insiders": rug.insiders_detected,
                "change_h1": market.price_change_h1,
                "vol_liq": market.volume_h24 / market.liquidity_usd if market.liquidity_usd else None,
            },
            **decision,
        )
        if self.db.get_meta("paused") == "1":
            log.info("Paused, not sending alert for %s", market.symbol)
        else:
            log.info("ALERT %s (%s)", market.symbol, market.address)
            message_id = await self.tg.send(text, buttons)
            # Rug-only watch on every alert; /bought upgrades it to the full watch.
            watch = Watch(
                address=market.address,
                symbol=market.symbol,
                started_at=time.time(),
                entry_price=market.price_usd,
                entry_mcap=market.market_cap_usd,
                max_liquidity=market.liquidity_usd,
                alert_message_id=message_id,
            )
            self.db.save_watch(watch.to_values())

    # --- tracker and reports --------------------------------------------

    def report(self, since: float = 0, title: str = "Tracker (all time)") -> str:
        checkpoints = list(self.cfg.tracker.checkpoints)
        report = build_report(self.db, checkpoints, since, title)
        progress = self.db.alert_progress(checkpoints, since)
        waiting = " · ".join(f"{cp}: {progress[cp][0]}" for cp in checkpoints)
        lines = [report, "", f"Alerts still waiting for their checkpoint: {waiting}"]
        missed = " · ".join(f"{cp}: {progress[cp][1]}" for cp in checkpoints if progress[cp][1])
        if missed:
            lines.append(f"Checkpoints missed while the bot was offline: {missed}")
        return "\n".join(lines)

    async def track(self) -> None:
        await take_snapshots(self.db, self.dex, self.cfg.tracker)
        if not self.cfg.tracker.weekly_summary:
            return
        now = time.time()
        last = float(self.db.get_meta("last_weekly_summary", "0") or 0)
        if last == 0:
            self.db.set_meta("last_weekly_summary", str(now))  # first summary a week after first run
        elif now - last >= WEEK:
            await self.tg.send(self.report(now - WEEK, "Weekly summary"))
            self.db.set_meta("last_weekly_summary", str(now))

    # --- commands --------------------------------------------------------

    def handle_command(self, text: str) -> str:
        cmd = text.split()[0].split("@")[0].lower() if text.strip() else ""
        if cmd == "/status":
            counts = self.db.counts()
            paused = self.db.get_meta("paused") == "1"
            return (
                f"{'⏸ Paused' if paused else '▶️ Running'}\n"
                f"Watching: {counts.get('watching', 0)}\n"
                f"Alerted: {counts.get('alerted', 0)}\n"
                f"Rejected by rug checks: {counts.get('rejected', 0)}\n"
                f"Aged out: {counts.get('expired', 0)}\n\n"
                + self.speed_report()
                + "\n\n"
                + usage.report()
            )
        if cmd == "/stats":
            return self.report()
        if cmd == "/week":
            return self.report(time.time() - WEEK, "Last 7 days")
        if cmd == "/recent":
            rows = self.db.recent_alerts()
            if not rows:
                return "No alerts yet."
            return "\n".join(
                f"${escape(r['symbol'] or '?')} · MC {usd(r['ref_mcap'])} · "
                f"{datetime.fromtimestamp(r['decided_at'], timezone.utc):%m-%d %H:%M} UTC\n"
                f"<code>{r['address']}</code>"
                for r in rows
            )
        if cmd == "/rejects":
            counts, total, latest = self.db.reject_summary()
            if not total:
                return "No coins rejected yet."
            lines = [f"<b>Rejected coins: {total}</b>", "A coin can fail several rules.", ""]
            for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"{code}: {n} ({n / total * 100:.0f}%)")
            lines += ["", "<b>Latest rejections</b>"]
            for r in latest:
                reasons = json.loads(r["details"] or "{}").get("reasons", [])
                lines.append(f"${escape(r['symbol'] or '?')}: " + escape("; ".join(reasons)))
            return "\n".join(lines)
        if cmd == "/pause":
            self.db.set_meta("paused", "1")
            return "⏸ Alerts paused. Screening and tracking continue."
        if cmd == "/resume":
            self.db.set_meta("paused", "0")
            return "▶️ Alerts resumed."
        return HELP

    # --- position watch ------------------------------------------------

    async def check_watches(self) -> None:
        wc = self.cfg.watch
        now = time.time()
        watches = [
            Watch.from_row(r)
            for r in self.db.active_watches(wc.alert_watch_hours, wc.position_watch_hours, now)
        ]
        # Paper trading follows every recent alert, including muted ones.
        paper_open = [r["address"] for r in self.db.paper_open(now - paper.WINDOW_HOURS * 3600)]
        addresses = list(dict.fromkeys([w.address for w in watches] + paper_open))
        if not addresses:
            return
        markets = await self.dex.markets(addresses)
        paper.record_marks(self.db, markets, self.cfg.paper, now)
        paused = self.db.get_meta("paused") == "1"
        for w in watches:
            messages = evaluate(w, markets.get(w.address), wc, now)
            self.db.save_watch(w.to_values())
            if paused and not w.bought:
                continue  # paused silences alerts and their follow-ups, never coins you hold
            for text in messages:
                if not w.alert_message_id:
                    text += f"\n<code>{w.address}</code>"
                await self.tg.send(text, reply_to=w.alert_message_id)

    def explain(self, address: str | None) -> str:
        """Plain-language account of where a coin stands and why."""
        if not address:
            return "Which coin? Send /why followed by the token address or $SYMBOL, or reply /why to an alert."
        row = self.db.get_token(address)
        if not row:
            return "I haven't seen that coin. It may not have come through any discovery source."
        name = f"${escape(row['symbol'] or '?')}"
        status = {
            "watching": "👀 On the watchlist, re-checked every minute",
            "alerted": "🟢 Alerted",
            "rejected": "❌ Rejected by the rug checks",
            "expired": "⌛ Aged out (stopped watching)",
        }.get(row["status"], row["status"])
        lines = [f"<b>{name}</b>", f"<code>{address}</code>", status]
        if row["last_checked"]:
            ago = time.time() - row["last_checked"]
            lines.append(f"Last checked {age_text(ago / 60)} ago")
        if row["peak_mcap"]:
            lines.append(f"Highest MC seen: {usd(row['peak_mcap'])}")
        if row["status"] == "rejected":
            reasons = json.loads(row["details"] or "{}").get("reasons", [])
            lines += ["", "<b>Rug checks failed:</b>"] + [f"• {escape(r)}" for r in reasons]
        elif row["status"] in ("watching", "expired"):
            reasons = json.loads(row["last_reasons"] or "null")
            if reasons is None:
                lines += ["", "No market data yet: DexScreener hasn't listed a pool for it."]
            elif reasons:
                lines += ["", "<b>Latest market check failed:</b>"] + [f"• {escape(r)}" for r in reasons]
            else:
                lines += ["", "Passed the market check; rug checks are pending or temporarily unavailable."]
        return "\n".join(lines)

    def _resolve_coin(self, arg: str, reply_to: int | None) -> str | None:
        if reply_to:
            row = self.db.watch_for_message(reply_to)
            if row:
                return row["address"]
        arg = arg.strip().lstrip("$")
        if SOLANA_ADDRESS.match(arg):
            return arg
        if not arg:
            return None
        return self.db.alerted_by_symbol(arg) or self.db.token_by_symbol(arg)

    async def handle_message(self, text: str, reply_to: int | None = None) -> str:
        """Commands that need live data; everything else goes to handle_command."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "/paper":
            return paper.report(self.db, self.cfg.paper)
        if cmd == "/why":
            return self.explain(self._resolve_coin(arg, reply_to))
        if cmd not in ("/bought", "/sold", "/mute", "/positions"):
            return self.handle_command(text)

        if cmd == "/positions":
            rows = self.db.positions()
            if not rows:
                return "No positions. Reply /bought to an alert after you buy."
            markets = await self.dex.markets([r["address"] for r in rows])
            lines = ["<b>Your positions</b>"]
            for r in rows:
                m = markets.get(r["address"])
                now_mcap = m.market_cap_usd if m else None
                multiple = (
                    f"{m.price_usd / r['entry_price']:.2f}x"
                    if m and m.price_usd and r["entry_price"] else "?"
                )
                lines.append(
                    f"${escape(r['symbol'] or '?')} · {multiple} · MC {usd(r['entry_mcap'])} → "
                    f"{usd(now_mcap)} (peak {usd(r['peak_mcap'])})"
                )
            return "\n".join(lines)

        address = self._resolve_coin(arg, reply_to)
        if not address:
            return (
                f"Which coin? Reply {cmd} to the alert, or send {cmd} followed by the "
                "token address or $SYMBOL."
            )

        if cmd in ("/sold", "/mute"):
            row = self.db.get_watch(address)
            if not row or not row["active"]:
                return "I wasn't watching that coin."
            self.db.stop_watch(address)
            symbol = escape(row["symbol"] or "?")
            return f"🔕 Muted ${symbol}." if cmd == "/mute" else f"Stopped watching ${symbol}."

        market = (await self.dex.markets([address])).get(address)
        if not market or not market.price_usd:
            return "Couldn't get a price for that coin right now. Try again in a minute."
        row = self.db.get_watch(address)
        watch = Watch.from_row(row) if row else Watch(address=address, symbol=market.symbol, started_at=time.time())
        watch.start_position(market, time.time())
        self.db.save_watch(watch.to_values())
        wc = self.cfg.watch
        milestones = "/".join(f"{m:g}x" for m in sorted(wc.milestones))
        return (
            f"✅ Watching ${escape(market.symbol)} from MC {usd(market.market_cap_usd)}.\n"
            f"I'll message you at {milestones}, a {wc.dip_from_peak_pct:g}% drop from its peak, "
            f"heavy selling, or liquidity being pulled. Send /sold when you exit."
        )

    async def command_loop(self) -> None:
        if not self.tg.enabled:
            return
        offset = 0
        while True:
            try:
                for update in await self.tg.updates(offset):
                    offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    # Only answer the configured chat.
                    if str((msg.get("chat") or {}).get("id")) != self.cfg.telegram_chat_id:
                        continue
                    if (msg.get("text") or "").startswith("/"):
                        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
                        await self.tg.send(await self.handle_message(msg["text"], reply_to))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(5)

    # --- loops -----------------------------------------------------------

    async def run_cycle(self) -> None:
        started = time.time()
        new = await self.discover()
        if new:
            log.info("Discovered %d new tokens", new)
        await self.screen_watchlist()
        took = time.time() - started
        self.db.set_meta("last_cycle_at", str(time.time()))
        self.db.set_meta("last_cycle_seconds", f"{took:.1f}")
        if took > self.cfg.discovery.poll_interval_seconds:
            log.warning("Screening cycle took %.0fs, longer than the %ss interval",
                        took, self.cfg.discovery.poll_interval_seconds)

    def speed_report(self) -> str:
        lines = []
        last_at = self.db.get_meta("last_cycle_at")
        if last_at:
            ago = time.time() - float(last_at)
            took = float(self.db.get_meta("last_cycle_seconds", "0") or 0)
            interval = self.cfg.discovery.poll_interval_seconds
            lines.append(f"Last scan: {ago:.0f}s ago, took {took:.0f}s (runs every {interval}s)")
            if took > interval:
                lines.append("⚠️ Scans take longer than the interval, so the bot is falling behind")
            if ago > interval + took + 120:
                lines.append("⚠️ No scan for a while. Check the logs")
        else:
            lines.append("Last scan: none yet")
        ages = self.db.recent_alert_ages()
        if ages:
            ages.sort()
            lines.append(
                f"Coin age at alert (last {len(ages)}): median {age_text(ages[len(ages) // 2])}, "
                f"youngest {age_text(ages[0])}"
            )
        return "\n".join(lines)

    async def _every(self, seconds: float, fn, name: str) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s failed", name)
            await asyncio.sleep(seconds)

    async def run(self) -> None:
        requeued = self.db.undo_token2022_rejections()
        if requeued:
            log.info("Re-checking %d coins wrongly rejected as Token-2022 traps", requeued)
        if not self.tg.enabled:
            log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set: alerts print to the console")
        else:
            await self.tg.send("🤖 Screener started.\n\n" + HELP)
        tasks = [
            self._every(self.cfg.discovery.poll_interval_seconds, self.run_cycle, "Screening cycle"),
            self._every(self.cfg.tracker.interval_seconds, self.track, "Tracker"),
            self._every(self.cfg.watch.interval_seconds, self.check_watches, "Position watch"),
            self.command_loop(),
        ]
        if self.cfg.discovery.pumpportal_migrations:
            tasks.append(stream_migrations(self.cfg.apis.pumpportal_ws, self.on_migration))
        if self.cfg.discovery.helius_new_pools:
            if self.cfg.helius_api_key:
                helius = HeliusPools(
                    self.client,
                    self.cfg.helius_api_key,
                    self.cfg.apis.helius_ws,
                    self.cfg.apis.helius_rpc,
                    self.on_new_pool,
                    self.cfg.discovery.helius_programs,
                )
                tasks.append(helius.run())
            else:
                log.warning("HELIUS_API_KEY not set: skipping Helius new-pool discovery")
        await asyncio.gather(*tasks)
