# MA Crossover Bot (Zerodha Kite)

Buys when the fast moving average crosses above the slow one (golden cross) and
exits (or shorts, if enabled) when it crosses below (death cross).

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in KITE_API_KEY / KITE_API_SECRET
```
In the Kite developer console, set your app's **Redirect URL** (e.g. `http://127.0.0.1`).
After logging in you'll land on that URL. Paste the full URL back into the terminal.

## Usage
```bash
python auth.py                                   # log in (once per day; token expires ~6 AM)
python backtest.py --symbol RELIANCE --days 180  # backtest first!
python main.py                                   # run the bot (paper mode by default)
```

## Files
| File | Purpose |
|---|---|
| `config.py` | Settings from `.env` |
| `auth.py` | Daily login, caches the access token in `state/` |
| `data.py` | Instrument lookup, historical candles |
| `strategy.py` | SMA/EMA crossover signals (shared by backtest + live) |
| `broker.py` | Paper/live orders, position tracking, `logs/trades.csv` |
| `backtest.py` | Backtest with next-candle-open fills and costs |
| `main.py` | Live loop: runs at every candle close, MIS square-off |
| `ticker.py` | WebSocket live prices for instant stop-loss / target exits |
| `check_credentials.py` | Read-only check that your Kite login works |
| `news.py` | Morning news briefing analysed by Claude (advisory) |

## Position sizing
With `RISK_PER_TRADE` set, each trade buys enough shares that hitting the stop-loss loses about
that many rupees: `shares = RISK_PER_TRADE / (price x STOP_LOSS_PCT%)`, capped by
`MAX_CAPITAL_PER_TRADE`. Before every entry the bot checks funds (live: Zerodha's margin for the
order vs your available funds; paper: `PAPER_FUNDS` minus open positions). If funds are short it
buys fewer shares, or skips the trade if not even 1 share fits.
Set `RISK_PER_TRADE=0` to trade a fixed `QUANTITY` instead.

## Money limits
Set in `.env` (0 = off). They apply to new positions only; exits always run.
| Setting | Limits |
|---|---|
| `MAX_CAPITAL_PER_TRADE` | the value of one position (shares x price) |
| `MAX_TOTAL_CAPITAL` | the value of all open positions together; the bot buys fewer shares or skips a trade to stay under it, in paper and live mode |
| `MAX_DAILY_LOSS` | stops new trades for the day once today's closed plus open losses reach it |
The backtester applies `MAX_CAPITAL_PER_TRADE`; the total and daily-loss limits apply to the live and paper bot.

## Market filter (follow the overall market)
With `MARKET_FILTER=true`, at every candle check the bot first fetches `MARKET_INDEX` (NIFTY 50)
and compares its close with its EMA(`MARKET_MA_PERIOD`) on the same candle interval:
- index above its EMA = market UP: golden crosses may open longs
- index below its EMA = market DOWN: golden crosses are logged and skipped
- index data unavailable = UNKNOWN: no new entries that candle (fail-safe)
Exits are never blocked. The backtest applies the same rule: `--market-filter/--no-market-filter`, `--market-ma 50`.

## Stop-loss / target (live ticks)
Set `STOP_LOSS_PCT` / `TARGET_PCT` in `.env` (0 = off). While a position is open, the bot
watches live prices over Kite's WebSocket and exits the moment the price is that % against
or in favour of the entry. Entries still happen only on candle-close crossovers.
If the WebSocket drops, exits are also checked at every candle close as a backup.
Backtest with the same levels: `python backtest.py --symbol RELIANCE --stop-loss-pct 1 --target-pct 2`

## Morning news briefing (Claude, via your subscription)
With `NEWS_MODE=advisory` or `veto`, the bot runs a briefing once per day at startup, in the background, so
trading isn't delayed. It collects:
- market headlines (ET Markets, ET Stocks, Livemint, Business Standard, Google News) and per-stock news
- a Kite snapshot: NIFTY 50 / BANK / IT and India VIX (today, 5d, 20d, vs 20/50-day EMAs) plus each stock
and passes them to the **Claude Code CLI** (`claude -p`), logged in with your Claude subscription. There's
no API key and no API billing; it counts toward your plan's usage limits. Claude runs with its tools
disabled, in an empty temp folder, and any `ANTHROPIC_API_KEY` is removed from its environment.
The results go to `logs/news_YYYY-MM-DD.md` (to read) and `.json` (for later review), with a short summary
in the bot log. **Advisory only: trading rules are unchanged.** A restart the same day reuses the saved
briefing. Run it by hand with `python news.py` (`--refresh` regenerates it).

**`NEWS_MODE=veto`**: the briefing can also **block new trades** for the day, but it never opens trades
and never blocks exits:
- results or major news today in a stock: no new trades in it (gaps can jump past stop-losses)
- bearish outlook: no longs in that stock; bullish outlook: no shorts in it
Vetoes apply once the briefing is ready (about 90s after startup) and only for that day. If the briefing
fails, the bot trades normally. Each blocked trade is **shadow-tracked** with the bot's exit rules and logged
to `logs/news_vetoes.csv`; run `python shadow.py` to see whether the vetoes saved or cost money so far.

One-time setup: install the Claude Code CLI, then run `claude` once in a terminal and log in with your
Claude account. If `claude` isn't on your PATH, set `CLAUDE_CLI_PATH` in `.env`.

## Going live
1. Paper trade for a few weeks and review `logs/trades.csv` and `logs/bot_*.log`.
2. Check Zerodha's current API trading requirements (e.g. static IP registration under SEBI's retail algo rules).
3. Set `PAPER_TRADING=false`. The bot asks you to type `LIVE` before starting.

Notes:
- Orders are limit orders at LTP ± `LIMIT_BUFFER_PCT`. Unfilled orders are cancelled after 30 seconds.
- The bot only manages positions it opened itself. Stocks in `PROTECTED_SYMBOLS` (DELHIVERY, BAJAJFINSV) can never be traded: startup fails if they are in `SYMBOLS`, and the broker refuses any order for them.
- It waits for a *fresh* crossover. It won't enter because fast > slow at startup.
- No new positions after `NO_NEW_ENTRIES_AFTER` (default 14:30), so late signals don't open trades that get squared off minutes later. Exits still happen. The backtest applies the same rule.
