# Solana memecoin screener (Telegram alerts)

A bot that watches new Solana memecoins, rejects the ones that show known rug patterns, and sends the rest to you on Telegram. **You decide whether to buy.** It never trades and never touches a wallet.

It also records the price of every coin it judged 1h, 24h and 7d later, for alerted and rejected coins alike, so you can see whether your filters actually work before you size up.

> Passing the filters doesn't make a coin safe. The checks catch known rug patterns (mint/freeze authority, unlocked LP, concentrated holders, bad dev history, honeypots). They can't catch a dev selling slowly through fresh wallets, coordinated insider dumps, or a coin that just dies, which is the most common outcome. Treat each alert as a shortlist, take 30 seconds to check it yourself, and only trade money you can afford to lose. This isn't financial advice.

## How it works

```
 Discovery ──► Tier 2 market filter ──► Tier 1 rug checks ──► Telegram alert
 (DexScreener     (DexScreener only,       (RugCheck, GoPlus,        │
  profiles/boosts, re-checked every         Jupiter sell quote)      ▼
  PumpPortal       cycle until it passes                         SQLite tracker
  migrations)      or turns 24h old)                             (price at 1h/24h/7d)
```

1. **Discovery.** Every 60s it pulls DexScreener's latest token profiles and boosts. It also streams pump.fun tokens that graduate to an AMM pool, from PumpPortal's free websocket. With a Helius API key, it also watches new pools on Raydium (AMM v4 and CPMM), PumpSwap and Meteora DLMM as they're created. New tokens go on a watchlist.
2. **Tier 2 market filter (cheap).** Uses DexScreener data only. Tokens that fail are re-checked every cycle until they pass or age out, since a 10-minute-old coin can look very different an hour later. Coins still under $2k liquidity after 2 hours are dropped early.
   - Liquidity ≥ $8k
   - Market cap $30k to $1.5M
   - Liquidity ≥ 5% of market cap
   - Age 15 min to 24 h
   - 24h volume ≥ 1× liquidity
3. **Tier 1 rug checks (hard rejects).** These run only on tokens that pass Tier 2. A token that fails here is rejected for good.
   - Mint authority and freeze authority revoked (RugCheck, with GoPlus as a second opinion)
   - ≥ 90% of LP locked or burned (liquidity-weighted across pools)
   - Top 10 holders ≤ 20%, no single wallet > 10%, dev wallet ≤ 5% (pool accounts excluded)
   - Creator has no rug history and isn't flagged as malicious
   - No RugCheck "danger" risks (configurable ignore list)
   - No Token-2022 traps: transfer fees, transfer hooks, non-transferable, closable, balances editable by an authority
   - **Sell simulation:** quotes a 0.03 SOL buy on Jupiter, then quotes selling the tokens back. Rejects if there's no sell route or the round trip loses > 15%.
4. **Socials (soft score, never a reject).** It checks for a website, X and Telegram, and fetches the Telegram member count. It warns when the website was reused from another token, when the X link is a post instead of an account, when the Telegram group is tiny, or when there are no socials at all.
5. **Alert.** You get the key stats, the passed checks, any warnings, and one-tap buttons: DexScreener, RugCheck, Solscan, X search and the token's own links.
6. **Tracker.** Every alerted *and* rejected coin gets its price recorded at 1h, 24h and 7d. `/stats` and the weekly summary compare the two groups and break the rejected coins down by the rule that rejected them. If coins rejected by some rule keep doing well, that rule may be too strict.

Every threshold lives in [`config.yaml`](config.yaml).

## Setup

You need Python 3.11+.

1. **Create the Telegram bot.** Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token. Then message your new bot once (it can't message you first). Get your chat id from [@userinfobot](https://t.me/userinfobot).
2. **Install:**
   ```bash
   git clone <this repo> && cd Tg-screening-bot
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env    # then put your Telegram token, chat id and Helius key in .env
   ```
3. **Run:**
   ```bash
   python -m screener            # run forever
   python -m screener --once     # one screening cycle, then exit (good for testing)
   python -m screener --stats    # print the tracker report
   ```
   Without a Telegram token, alerts print to the console instead. Without `HELIUS_API_KEY`, the Helius source is skipped and the rest still runs.

### Helius new-pool discovery

Get a key at [dashboard.helius.dev](https://dashboard.helius.dev) and put it in `.env` as `HELIUS_API_KEY`. The free plan is enough. The bot opens a websocket to Helius and uses `logsSubscribe` (standard Solana RPC) on each pool program. When a log shows a pool being created, it fetches that transaction with `getTransaction` to find the token's mint.

Each new pool costs about one credit for the transaction lookup. Pick which DEXes to watch with `discovery.helius_programs` in `config.yaml`. The API key is masked in log output.

### Telegram commands

| Command | What it does |
|---|---|
| `/status` | Watchlist size and decision counts |
| `/stats` | Alerted vs rejected performance, all time |
| `/week` | Same, last 7 days |
| `/recent` | Last 10 alerts |
| `/pause` / `/resume` | Stop or restart alerts. Screening and tracking keep running. |

The bot only answers the chat id in `.env`.

### Running 24/7 on a VPS ($5–10/month)

Any small Linux VPS works. Clone the repo to `/opt/tg-screening-bot`, set up the venv and `.env` as above, then use the systemd unit in [`deploy/screener.service`](deploy/screener.service). Its comments have the commands.

## Cost

Every data source used here has a free tier: DexScreener, RugCheck, GoPlus, Jupiter's lite API, PumpPortal and the Telegram Bot API. So the only cost is the VPS. If you hit rate limits as volume grows, the usual next step is a paid Solana RPC (e.g. Helius) and a paid Jupiter or Birdeye key, roughly $50–100/month. Check current pricing.

## Using it at $5 a trade

- Fees (network, priority, token account rent) can eat 3–6% of a $5 trade, so a coin has to move just for you to break even.
- Cap slippage around 5–10% in your wallet. Failed transactions still cost fees.
- After a few weeks, check `/stats`. Only raise your size if `pass` clearly beats `fail` at 24h and 7d.

## Known limits

- **Discovery coverage.** Without a Helius key, the bot only sees tokens with a paid DexScreener profile or boost, plus pump.fun graduations. Even with Helius, it only watches the four pool programs listed in `config.yaml`, so launches on other DEXes are missed.
- **The sell check is a quote, not an on-chain simulation.** It catches missing sell routes and heavy sell taxes. The freeze authority and Token-2022 checks cover the main ways a token blocks selling on-chain.
- **API shapes change.** The parsers are defensive, but if a source changes its response format, a check may start failing. Failures retry next cycle instead of letting a token through. Watch the logs after you first deploy.
- **Holder data comes from RugCheck's top-holder list.** Wallets that split a large bag across many small wallets won't trip the concentration rule.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run the whole pipeline (discovery → filters → alert → tracker) against mocked API responses, so they don't need network access.

```
screener/
  app.py          orchestration: loops, decisions, Telegram commands
  filters.py      Tier 2 and Tier 1 rules (pure functions)
  socials.py      socials soft score
  alerts.py       alert message formatting
  tracker.py      price checkpoints and the performance report
  db.py           SQLite storage
  telegram.py     Bot API client
  sources/        DexScreener, RugCheck, GoPlus, Jupiter, PumpPortal, Helius
```
