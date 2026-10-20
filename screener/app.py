"""Main loop: discover -> pre-filter -> rug checks -> alert, plus the tracker and commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape

import httpx

from . import socials as socials_mod
from . import paper, usage
from .alerts import age_text, format_alert, format_near_bond_alert, usd
from .config import Config
from .db import Database
from .filters import near_bond_reasons, pullback_reasons, tier1, tier2
from .http import ApiError, ClientError
from .sources.dexscreener import DexScreener, Market
from .sources.goplus import GoPlus
from .sources.helius import HeliusPools
from .sources.jupiter import Jupiter
from .sources.pumpfun import CurveCoin, PumpFun, bonding_progress, parse_coin
from .sources.pumpportal import stream_migrations
from .sources.rugcheck import RugCheck
from .telegram import Telegram
from .tracker import build_report, take_snapshots
from .watch import Watch, evaluate

log = logging.getLogger(__name__)

# Rejected after this many "unable to generate report" answers from RugCheck in a row.
MAX_REPORT_FAILURES = 5

WEEK = 7 * 86400
SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
HELP = (
    "/status – watchlist and decision counts\n"
    "/stats – how alerts performed vs rejected coins (all time)\n"
    "/stats new – same, only coins judged under the current filter rules\n"
    "/week – same, last 7 days\n"
    "/recent – last 10 alerts\n"
    "/rejects – which rug checks reject coins most, with examples\n"
    "/bought – reply to an alert (or add the address or $SYMBOL) to watch your position\n"
    "/sold – stop watching a coin (reply, address or $SYMBOL)\n"
    "/mute – stop follow-ups for an alert (reply to it)\n"
    "/positions – coins you're holding and how they're doing\n"
    "/paper – would $5 on every alert have made money? (after fees)\n"
    "/paper new – same, only alerts under the current filter rules\n"
    "/why – why a coin was or wasn't alerted (address, $SYMBOL, or reply)\n"
    "/nearbond – raw pump.fun data from the last near-bond scan (troubleshooting)\n"
    "/pause – stop sending alerts (screening and tracking continue)\n"
    "/resume – start sending alerts again"
)


def _near_bond_miss(reason: str) -> str:
    """Short label for why a curve coin didn't qualify, for the /status breakdown."""
    if "bonded" in reason:
        return "outside bonded range"
    if "market cap" in reason:
        return "market cap out of range"
    if "younger" in reason:
        return "too young"
    return "down from peak"


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
        # RugCheck answers HTTP 400 "unable to generate report" for some tokens, every time.
        # Count those per token so they're rejected instead of retried forever.
        self.report_failures: dict[str, int] = {}
        self.pumpfun = PumpFun(client, cfg.near_bond.api)
        # Highest market cap seen per bonding-curve coin, for the drop-from-peak rule.
        self.curve_peaks: dict[str, float] = {}

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
        pullbacks: list[Market] = []
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
            elif self.cfg.pullback.enabled and self._is_pullback(market, now):
                pullbacks.append(market)

        log.info("Watchlist %d, with pools %d, passed market filter %d, pullbacks %d",
                 len(addresses), len(markets), len(passed), len(pullbacks))
        await asyncio.gather(
            *(self.check_token(m) for m in passed),
            *(self.check_token(m, kind="pullback") for m in pullbacks),
        )

    def _is_pullback(self, market: Market, now: datetime) -> bool:
        """Failed only the drop-from-peak rule, and looks like a bounce off a higher low."""
        if not tier2(market, self.cfg.tier2, now, None).passed:
            return False
        history = self.db.price_history(market.address)
        return history is not None and not pullback_reasons(market, self.cfg.pullback, history, time.time())

    async def check_token(self, market: Market, kind: str | None = None) -> None:
        async with self.rug_check_slots:
            try:
                await self._check_token(market, kind)
            except ClientError as exc:
                if not self._give_up_on_report(market.address, exc, price=market.price_usd,
                                               mcap=market.market_cap_usd, liquidity=market.liquidity_usd):
                    log.warning("Rug checks unavailable for %s, retrying next cycle: %s", market.symbol, exc)
            except ApiError as exc:
                log.warning("Rug checks unavailable for %s, retrying next cycle: %s", market.symbol, exc)
            except Exception:
                log.exception("Unexpected error checking %s", market.address)

    def _give_up_on_report(self, address: str, exc: ClientError, **decision) -> bool:
        """After MAX_REPORT_FAILURES refusals from RugCheck, reject the token: it can't be checked."""
        if "rugcheck" not in str(exc) or exc.status != 400:
            return False
        failures = self.report_failures.get(address, 0) + 1
        self.report_failures[address] = failures
        if failures < MAX_REPORT_FAILURES:
            return False
        self.report_failures.pop(address, None)
        if self.db.get_token(address) is None:
            self.db.add_token(address, "pumpfun-curve")
        reason = f"RugCheck can't generate a report ({failures} tries), so it can't be checked"
        log.info("Rejected %s: %s", address, reason)
        self.db.decide(address, "rejected", reject_codes=["no_rug_report"], details={"reasons": [reason]}, **decision)
        return True

    async def _check_token(self, market: Market, kind: str | None = None) -> None:
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
            price=market.price_usd, mcap=market.market_cap_usd, liquidity=market.liquidity_usd, kind=kind
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
        headline = None
        if kind == "pullback":
            history = self.db.price_history(market.address)
            headline = (
                f"↩️ <b>PULLBACK</b> · peaked {usd(history['peak_mcap'])}, low {usd(history['trough_mcap'])}, "
                f"bouncing at {usd(market.market_cap_usd)}"
            )
        text, buttons = format_alert(
            market, rug, goplus, sell, socials, result, datetime.now(timezone.utc), headline=headline
        )
        self.db.decide(
            market.address,
            "alerted",
            details={
                "warnings": result.warnings + socials.warnings,
                "socials": socials.score,
                "age_minutes": market.age_minutes(datetime.now(timezone.utc)),
                "insiders": rug.insiders_detected,
                "top10_pct": rug.top10_pct,
                "max_holder_pct": rug.max_holder_pct,
                "dev_pct": rug.dev_pct,
                "holders": rug.total_holders,
                "bundle_wallets": rug.bundle_wallets,
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

    # --- near-bond alerts (pump.fun coins still on the curve) -------------

    def near_bond_status(self) -> str:
        if not self.cfg.near_bond.enabled:
            return "⚡ Near-bond alerts: off"
        error = self.db.get_meta("near_bond_error")
        last_ok = self.db.get_meta("near_bond_last_ok")
        if error:
            return f"⚡ Near-bond scan failing: {escape(error)}"
        if not last_ok:
            return "⚡ Near-bond scan: waiting for first check"
        line = f"⚡ Near-bond scan OK, last {age_text((time.time() - float(last_ok)) / 60)} ago"
        funnel = self.db.get_meta("near_bond_funnel")
        if funnel:
            line += f"\n  Last scan: {escape(funnel)}"
        row = self.db.conn.execute(
            "SELECT SUM(status = 'alerted') AS a, SUM(status = 'rejected') AS r FROM tokens WHERE kind = 'near_bond'"
        ).fetchone()
        line += f"\n  Near-bond so far: {row['a'] or 0} alerted, {row['r'] or 0} rejected by rug checks"
        return line

    def near_bond_debug(self, limit: int = 8) -> str:
        """The raw pump.fun fields behind the near-bond numbers, to check the bonded % maths."""
        raw = self.pumpfun.last_raw
        error = self.db.get_meta("near_bond_error")
        if error:
            return f"⚡ The near-bond scan is failing: {escape(error)}" + (
                "\n\nThe phone can't look up pump.fun's address. If other sites load, pump.fun may be "
                "blocked on this network or have moved its API; try another network or Private DNS."
                if "hostname" in error.lower() or "name resolution" in error.lower() else "")
        if not raw:
            return "No near-bond scan data yet."
        now = datetime.now(timezone.utc)
        # Skip the coins above the alert range: they're the top of pump.fun's list but never alert.
        in_range = [c for c in raw if (c.get("usd_market_cap") or 0) <= self.cfg.near_bond.max_market_cap_usd]
        # Most bonded first: the coins closest to qualifying.
        shown = sorted(in_range or raw, key=lambda c: bonding_progress(c) or 0, reverse=True)[:limit]
        caps = [c.get("usd_market_cap") for c in raw if isinstance(c.get("usd_market_cap"), (int, float))]
        span = f"market caps {usd(max(caps))} down to {usd(min(caps))}, " if caps else ""
        lines = [f"<b>Near-bond scan: {len(raw)} coins ({span}list position {self.pumpfun.start_offset}). "
                 f"{len(in_range)} at or under the max market cap. Most bonded {len(shown)}:</b>"]
        for c in shown:
            coin = parse_coin(c)
            if coin is None:
                verdict = "graduated or unreadable"
            else:
                reasons = near_bond_reasons(coin, self.cfg.near_bond, now, self.curve_peaks.get(coin.mint))
                verdict = f"{coin.progress_pct:.0f}% bonded: " + ("; ".join(reasons) if reasons else "qualifies")
            fields = " ".join(
                f"{key}={c.get(key)}"
                for key in ("usd_market_cap", "market_cap", "real_token_reserves", "virtual_token_reserves",
                            "virtual_sol_reserves", "complete")
            )
            lines.append(f"\n<b>${escape(str(c.get('symbol')))}</b> {escape(verdict)}\n<code>{escape(fields)}</code>")
        return "\n".join(lines)

    async def scan_near_bond(self) -> None:
        nb = self.cfg.near_bond
        try:
            coins = await self.pumpfun.unbonded_in_window(
                nb.fetch_limit, nb.fetch_pages, nb.scan_from_market_cap_usd, nb.min_market_cap_usd
            )
        except ApiError as exc:
            self.db.set_meta("near_bond_error", str(exc)[:200])
            log.warning("Near-bond scan failed: %s", exc)
            return
        self.db.set_meta("near_bond_error", "")
        self.db.set_meta("near_bond_last_ok", str(time.time()))
        if len(self.curve_peaks) > 5000:
            self.curve_peaks.clear()
        now = datetime.now(timezone.utc)
        judged = checked = 0
        misses: Counter[str] = Counter()
        for coin in coins:
            peak = max(self.curve_peaks.get(coin.mint, 0.0), coin.market_cap_usd)
            self.curve_peaks[coin.mint] = peak
            if self.db.status(coin.mint) not in (None, "watching"):
                judged += 1
                continue  # already alerted or judged, here or after graduating
            reasons = near_bond_reasons(coin, nb, now, peak)
            if reasons:
                misses[_near_bond_miss(reasons[0])] += 1
                continue
            checked += 1
            async with self.rug_check_slots:
                try:
                    await self._check_curve_coin(coin)
                except ClientError as exc:
                    if not self._give_up_on_report(coin.mint, exc, price=coin.price_usd,
                                                   mcap=coin.market_cap_usd, liquidity=None, kind="near_bond"):
                        log.warning("Rug checks unavailable for %s, retrying next scan: %s", coin.symbol, exc)
                except ApiError as exc:
                    log.warning("Rug checks unavailable for %s, retrying next scan: %s", coin.symbol, exc)
                except Exception:
                    log.exception("Unexpected error checking curve coin %s", coin.mint)
        parts = [f"{len(self.pumpfun.last_raw)} listed, {len(coins)} still on curve"]
        if coins:
            progress = sorted(c.progress_pct for c in coins)
            parts.append(f"bonded {progress[0]:.0f}-{progress[-1]:.0f}% (median {progress[len(progress) // 2]:.0f}%)")
        parts += [f"{n} {why}" for why, n in misses.most_common()]
        if judged:
            parts.append(f"{judged} already judged")
        parts.append(f"{checked} rug-checked")
        self.db.set_meta("near_bond_funnel", ", ".join(parts))

    async def _check_curve_coin(self, coin: CurveCoin) -> None:
        t1 = self.cfg.tier1
        rug = await self.rugcheck.report(coin.mint, extra_pool_accounts=coin.curve_accounts)
        goplus = None
        if t1.use_goplus:
            try:
                goplus = await self.goplus.security(coin.mint)
            except ApiError as exc:
                log.info("GoPlus unavailable for %s: %s", coin.symbol, exc)
        # No sell simulation: the pump.fun curve always lets holders sell.
        result = tier1(rug, goplus, None, t1, on_curve=True)
        self.db.add_token(coin.mint, "pumpfun-curve")
        self.db.touch(coin.mint, coin.symbol, coin.name, coin.market_cap_usd)
        decision = dict(price=coin.price_usd, mcap=coin.market_cap_usd, liquidity=None, kind="near_bond")
        if not result.passed:
            log.info("Rejected near-bond %s: %s", coin.symbol, "; ".join(m for _, m in result.rejects))
            self.db.decide(
                coin.mint, "rejected",
                reject_codes=sorted({code for code, _ in result.rejects}),
                details={"reasons": [m for _, m in result.rejects], "progress": coin.progress_pct},
                **decision,
            )
            return

        market = coin.as_market()
        socials = await socials_mod.assess(market, None, self.db, self.tg, self.cfg.socials)
        text, buttons = format_near_bond_alert(coin, rug, goplus, socials, result, datetime.now(timezone.utc))
        self.db.decide(
            coin.mint, "alerted",
            details={
                "warnings": result.warnings + socials.warnings,
                "socials": socials.score,
                "age_minutes": coin.age_minutes(datetime.now(timezone.utc)),
                "insiders": rug.insiders_detected,
                "top10_pct": rug.top10_pct,
                "max_holder_pct": rug.max_holder_pct,
                "dev_pct": rug.dev_pct,
                "holders": rug.total_holders,
                "bundle_wallets": rug.bundle_wallets,
                "progress": coin.progress_pct,
            },
            **decision,
        )
        if self.db.get_meta("paused") == "1":
            log.info("Paused, not sending near-bond alert for %s", coin.symbol)
            return
        log.info("NEAR-BOND ALERT %s (%s) at %.0f%%", coin.symbol, coin.mint, coin.progress_pct)
        message_id = await self.tg.send(text, buttons)
        watch = Watch(
            address=coin.mint,
            symbol=coin.symbol,
            started_at=time.time(),
            entry_price=coin.price_usd,
            entry_mcap=coin.market_cap_usd,
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
                + self.near_bond_status()
                + "\n\n"
                + usage.report()
            )
        if cmd == "/stats":
            if text.split()[1:2] == ["new"]:
                since = self.db.get_meta("rules_changed_at")
                if not since:
                    return "The bot hasn't recorded a rules change yet. Restart it once and try again."
                when = datetime.fromtimestamp(float(since), timezone.utc)
                return self.report(float(since), f"Since rules changed ({when:%b %d, %H:%M} UTC)")
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
        if row["status"] == "alerted":
            lines += ["", *self._alert_facts(row)]
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

    @staticmethod
    def _alert_facts(row) -> list[str]:
        """What the bot saw when it alerted: the numbers to compare with what happened next."""
        details = json.loads(row["details"] or "{}")
        lines = ["<b>At the alert:</b>"]
        if row["decided_at"]:
            lines.append(f"• Sent {age_text((time.time() - row['decided_at']) / 60)} ago")
        if row["ref_mcap"]:
            lines.append(f"• MC {usd(row['ref_mcap'])}")
        if details.get("age_minutes") is not None:
            lines.append(f"• Coin age {age_text(details['age_minutes'])}")
        holders = [
            f"{label} {details[key]:.1f}%"
            for label, key in (("Top 10", "top10_pct"), ("max wallet", "max_holder_pct"), ("dev", "dev_pct"))
            if details.get(key) is not None
        ]
        if holders:
            lines.append("• " + " · ".join(holders))
        if details.get("holders") is not None:
            lines.append(f"• Holders: {details['holders']:,}")
        if details.get("insiders") is not None:
            lines.append(f"• Insider wallets (RugCheck): {details['insiders']}")
        for warning in details.get("warnings", []):
            lines.append(f"• ⚠️ {escape(str(warning))}")
        return lines

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
            if arg.strip().lower() == "new":
                since = self.db.get_meta("rules_changed_at")
                if not since:
                    return "The bot hasn't recorded a rules change yet. Restart it once and try again."
                return paper.report(self.db, self.cfg.paper, since=float(since))
            return paper.report(self.db, self.cfg.paper)
        if cmd == "/why":
            return self.explain(self._resolve_coin(arg, reply_to))
        if cmd == "/nearbond":
            return self.near_bond_debug()
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

    def note_rules_change(self) -> bool:
        """Record when the filter rules last changed, so /stats new can compare fairly."""
        rules = json.dumps({"tier2": asdict(self.cfg.tier2), "tier1": asdict(self.cfg.tier1)}, sort_keys=True)
        digest = hashlib.sha256(rules.encode()).hexdigest()
        if self.db.get_meta("rules_hash") == digest:
            return False
        self.db.set_meta("rules_hash", digest)
        self.db.set_meta("rules_changed_at", str(time.time()))
        return True

    async def run(self) -> None:
        if self.note_rules_change():
            log.info("Filter rules changed: /stats new counts from now")
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
            *(
                [self._every(self.cfg.near_bond.poll_seconds, self.scan_near_bond, "Near-bond scan")]
                if self.cfg.near_bond.enabled else []
            ),
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
