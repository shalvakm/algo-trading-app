# Algo Trading Bot (Zerodha Kite)

Trades NSE stocks on candle-close signals from one of several strategies (`STRATEGY` in `.env`).
The default is a moving average crossover: it buys when the fast MA crosses above the slow one
(golden cross) and exits (or shorts, if enabled) when it crosses below (death cross).

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
python backtest.py --symbol RELIANCE --strategy all   # compare every strategy
python main.py                                   # run the bot (paper mode by default)
```

## Files
| File | Purpose |
|---|---|
| `config.py` | Settings from `.env` |
| `auth.py` | Daily login, caches the access token in `state/` |
| `data.py` | Instrument lookup, historical candles |
| `strategy.py` | Strategy signals: MA crossover, MACD, RSI, Bollinger, Supertrend, Donchian (shared by backtest + live) |
| `broker.py` | Paper/live orders, position tracking, `logs/trades.csv` |
| `backtest.py` | Backtest with next-candle-open fills and costs |
| `main.py` | Live loop: runs at every candle close, MIS square-off |
| `ticker.py` | WebSocket live prices for instant stop-loss / target exits |
| `check_credentials.py` | Read-only check that your Kite login works |
| `news.py` | Morning news briefing analysed by Claude (advisory) |
| `tests/` | Strategy unit tests: `python -m unittest discover tests` |

## Strategies
Pick one with `STRATEGY` in `.env`. Each one gives a BUY or SELL signal on a candle close; the bot
acts on it at the next candle the same way for every strategy (BUY = go long / cover a short,
SELL = exit a long / short if `ALLOW_SHORT=true`). Signals fire once, on the candle where the
condition first becomes true. Stop-loss, target, money limits, market filter and news veto all apply as usual.

| `STRATEGY` | Style | BUY when | SELL when | Settings |
|---|---|---|---|---|
| `ma_cross` (default) | trend | fast MA crosses above slow MA | fast MA crosses below slow MA | `FAST_PERIOD` 9, `SLOW_PERIOD` 21, `MA_TYPE` EMA |
| `macd` | momentum | MACD line crosses above its signal line | MACD line crosses below it | `MACD_FAST` 12, `MACD_SLOW` 26, `MACD_SIGNAL` 9 |
| `rsi` | mean reversion | RSI rises back above `RSI_OVERSOLD` | RSI falls back below `RSI_OVERBOUGHT` | `RSI_PERIOD` 14, `RSI_OVERSOLD` 30, `RSI_OVERBOUGHT` 70 |
| `bollinger` | mean reversion | close moves back above the lower band | close moves back below the upper band | `BB_PERIOD` 20, `BB_STD` 2 |
| `supertrend` | trend (ATR) | Supertrend flips up | Supertrend flips down | `SUPERTREND_PERIOD` 10, `SUPERTREND_MULTIPLIER` 3 |
| `donchian` | breakout | close breaks above the highest high of the previous N candles | close breaks below the lowest low | `DONCHIAN_PERIOD` 20 |

Mean-reversion strategies (`rsi`, `bollinger`) can hold a position for a long time before the opposite
signal, so pair them with `STOP_LOSS_PCT` / `TARGET_PCT`.

Backtest any of them, overriding settings on the command line, or compare all of them on the same data:
```bash
python backtest.py --symbol INFY --strategy macd
python backtest.py --symbol INFY --strategy rsi --rsi-oversold 25 --rsi-overbought 75
python backtest.py --symbol INFY --strategy all --stop-loss-pct 1 --target-pct 2
```
`--strategy all` prints one row per strategy (trades, win rate, return, drawdown, net P&L) and saves each
strategy's trades to `logs/backtest_<SYMBOL>_<interval>_<strategy>.csv`. A strategy that wins on one stock
and period can lose on another, so check several before switching.

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
- index above its EMA = market UP: BUY signals may open longs
- index below its EMA = market DOWN: BUY signals are logged and skipped
- index data unavailable = UNKNOWN: no new entries that candle (fail-safe)
Exits are never blocked. The backtest applies the same rule: `--market-filter/--no-market-filter`, `--market-ma 50`.

## Stop-loss / target (live ticks)
Set `STOP_LOSS_PCT` / `TARGET_PCT` in `.env` (0 = off). While a position is open, the bot
watches live prices over Kite's WebSocket and exits the moment the price is that % against
or in favour of the entry. Entries still happen only on candle-close strategy signals.
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
- It waits for a *fresh* signal. It won't enter because, say, fast > slow at startup.
- No new positions after `NO_NEW_ENTRIES_AFTER` (default 14:30), so late signals don't open trades that get squared off minutes later. Exits still happen. The backtest applies the same rule.
