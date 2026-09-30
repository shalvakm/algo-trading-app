"""Morning market briefing: latest news + market snapshot, analysed by Claude.

Runs once per day when the bot boots (a restart the same day reuses the saved
briefing). It is ADVISORY ONLY - it never changes what the bot trades.

    python news.py            # generate (or show) today's briefing
    python news.py --refresh  # force a new one

Output: logs/news_YYYY-MM-DD.md (to read) and .json (for later review).

Claude runs through the Claude Code CLI (`claude -p`), logged in with your Claude
subscription - no API key, no API billing; it counts toward your plan's usage limits.
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time as _time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests

from config import IST, LOG_DIR, Settings, load_settings
from data import fetch_history, resolve_index_token, with_retry

log = logging.getLogger("news")

CLI_TIMEOUT = 300  # seconds for one `claude -p` run
# Built-in Claude Code tools, all disabled: the briefing must only use the data we give it
DISALLOWED_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch",
                    "NotebookEdit", "Task", "TodoWrite"]
MAX_HEADLINE_AGE = timedelta(hours=36)
USER_AGENT = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126 Safari/537.36"}

MARKET_FEEDS = {
    "ET Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "Livemint Markets": "https://www.livemint.com/rss/markets",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
}
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
MARKET_QUERY = "Sensex Nifty stock market when:1d"

# Company names for per-stock news searches; "|" separates alternatives (override/extend with NEWS_NAMES in .env)
COMPANY_NAMES = {
    "RELIANCE": "Reliance Industries", "INFY": "Infosys", "TCS": "TCS|Tata Consultancy Services",
    "HDFCBANK": "HDFC Bank", "ICICIBANK": "ICICI Bank", "AXISBANK": "Axis Bank",
    "SBIN": "State Bank of India|SBI shares", "KOTAKBANK": "Kotak Mahindra Bank", "TMPV": "Tata Motors",
    "TMCV": "Tata Motors commercial vehicles", "DIXON": "Dixon Technologies", "DLF": "DLF",
    "ETERNAL": "Eternal Ltd|Zomato", "ITC": "ITC", "LT": "Larsen & Toubro", "WIPRO": "Wipro",
    "HCLTECH": "HCLTech", "BHARTIARTL": "Bharti Airtel", "MARUTI": "Maruti Suzuki",
    "BAJFINANCE": "Bajaj Finance", "SUNPHARMA": "Sun Pharma", "TATASTEEL": "Tata Steel",
    "HINDUNILVR": "Hindustan Unilever", "ASIANPAINT": "Asian Paints", "ADANIENT": "Adani Enterprises",
}
CONTEXT_INDICES = ["NIFTY 50", "NIFTY BANK", "NIFTY IT", "INDIA VIX"]

SYSTEM_PROMPT = """You are a market analyst preparing a pre-trade briefing for an automated \
intraday trading bot on India's NSE. The bot trades a moving-average crossover strategy on \
short candles. {direction} Your briefing is advisory: \
a human reads it, and nothing you say places or blocks trades.

Work only from the headlines and market data provided. Do not invent news, numbers or events. \
Headlines can be stale, duplicated, promotional or irrelevant (IPOs, crypto, personal finance) - \
ignore those. Overnight news is usually already reflected in prices by the open, so weigh the \
market data (index levels, trends, India VIX) at least as heavily as the headlines, and say so \
when they disagree.

Be calibrated: use "neutral" or "mixed" and low confidence when evidence is thin or conflicting. \
Flag event risks that make intraday moves less predictable (company results or board meetings \
today, RBI policy, major data releases, central bank decisions abroad, index changes, large \
block deals). For each stock, use "no_news" when nothing relevant appeared, and quote the \
headlines you relied on exactly as given."""

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "market_mood": {"type": "string", "enum": ["bullish", "bearish", "neutral", "mixed"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string", "description": "3-5 sentences on today's likely market backdrop"},
        "key_drivers": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "driver": {"type": "string"},
                "direction": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                "evidence": {"type": "string", "description": "headline or data point this is based on"},
            },
            "required": ["driver", "direction", "evidence"], "additionalProperties": False}},
        "event_risks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "event": {"type": "string"},
                "affects": {"type": "string", "description": "'market' or the affected symbols"},
                "note": {"type": "string"},
            },
            "required": ["event", "affects", "note"], "additionalProperties": False}},
        "stocks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "outlook": {"type": "string", "enum": ["bullish", "bearish", "neutral", "no_news"]},
                "flags": {"type": "array", "items": {"type": "string", "enum": [
                    "results_today", "results_soon", "major_news", "corporate_action",
                    "regulatory", "large_move_yesterday"]}},
                "note": {"type": "string"},
                "headlines": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["symbol", "outlook", "flags", "note", "headlines"], "additionalProperties": False}},
        "caution": {"type": "string", "description": "one line: what to watch out for today"},
    },
    "required": ["market_mood", "confidence", "summary", "key_drivers", "event_risks", "stocks", "caution"],
    "additionalProperties": False,
}


# ---------- news ----------
def _fetch_rss(url: str, source: str, limit: int) -> list[dict]:
    r = requests.get(url, headers=USER_AGENT, timeout=10)
    r.raise_for_status()
    cutoff = datetime.now(IST) - MAX_HEADLINE_AGE
    out = []
    for item in ET.fromstring(r.content).findall(".//item"):
        title = re.sub(r"\s+", " ", (item.findtext("title") or "")).strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(IST)
        except (TypeError, ValueError):
            continue
        if not title or published < cutoff:
            continue
        publisher = item.findtext("source") or source
        if source.startswith("Google") and " - " in title:
            title = title.rsplit(" - ", 1)[0]  # Google appends " - Publisher"
        out.append({"title": title, "source": publisher, "time": published.strftime("%d-%b %H:%M")})
        if len(out) >= limit:
            break
    return out


def collect_headlines(symbols: list[str], names: dict[str, str]) -> dict:
    """Recent market-wide and per-stock headlines. Failed feeds are skipped, not fatal."""
    market, seen, failed = [], set(), []
    sources = dict(MARKET_FEEDS)
    sources["Google News"] = GOOGLE_NEWS.format(q=quote_plus(MARKET_QUERY))
    for source, url in sources.items():
        try:
            for h in _fetch_rss(url, source, limit=25):
                key = h["title"].lower()
                if key not in seen:
                    seen.add(key)
                    market.append(h)
        except Exception as e:
            failed.append(source)
            log.warning("News feed %s failed: %s", source, type(e).__name__)

    per_stock = {}
    for sym in symbols:
        alternatives = [n.strip() for n in names.get(sym, sym).split("|") if n.strip()]
        q = " OR ".join(f'"{n}"' for n in alternatives) + " when:2d"
        try:
            per_stock[sym] = _fetch_rss(GOOGLE_NEWS.format(q=quote_plus(q)), "Google News", limit=8)
        except Exception as e:
            per_stock[sym] = []
            failed.append(f"Google News ({sym})")
            log.warning("News search for %s failed: %s", sym, type(e).__name__)
        _time.sleep(0.3)
    return {"market": market, "stocks": per_stock, "failed_sources": failed}


# ---------- market snapshot (Kite) ----------
def _pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if b else None


def _daily_stats(kite, token: int, days: int = 120) -> dict:
    end = datetime.now(IST)
    df = fetch_history(kite, token, "day", end - timedelta(days=days), end)
    if len(df) < 25:
        return {}
    close = df["close"]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    return {
        "last_daily_close": round(float(close.iloc[-1]), 2),
        "change_1d_pct": _pct(close.iloc[-1], close.iloc[-2]),
        "change_5d_pct": _pct(close.iloc[-1], close.iloc[-6]),
        "change_20d_pct": _pct(close.iloc[-1], close.iloc[-21]),
        "vs_20d_ema": "above" if close.iloc[-1] > ema20 else "below",
        "vs_50d_ema": "above" if close.iloc[-1] > ema50 else "below",
        "avg_20d_close": round(float(close.iloc[-20:].mean()), 2),
    }


def market_snapshot(kite, s: Settings, instruments: dict[str, dict]) -> dict:
    now = datetime.now(IST)
    keys = [f"{s.exchange}:{x}" for x in CONTEXT_INDICES + s.symbols]
    quotes = with_retry(kite.quote, keys)
    snap = {"as_of": now.strftime("%a %d-%b-%Y %H:%M IST"),
            "session": "pre-open (prices are previous close or pre-open)" if now.time() < datetime.strptime("09:15", "%H:%M").time()
            else "market open", "indices": {}, "stocks": {}}

    def live(q):
        last, prev = q.get("last_price"), (q.get("ohlc") or {}).get("close")
        return {"last": last, "prev_close": prev, "change_pct": _pct(last, prev) if last and prev else None}

    for name in CONTEXT_INDICES:
        q = quotes.get(f"{s.exchange}:{name}")
        if not q:
            continue
        entry = live(q)
        try:
            entry.update(_daily_stats(kite, resolve_index_token(kite, s.exchange, name)))
        except Exception as e:
            log.warning("Daily history for %s failed: %s", name, type(e).__name__)
        snap["indices"][name] = entry
    for sym in s.symbols:
        q = quotes.get(f"{s.exchange}:{sym}")
        if not q:
            continue
        entry = live(q)
        try:
            entry.update(_daily_stats(kite, instruments[sym]["instrument_token"]))
        except Exception as e:
            log.warning("Daily history for %s failed: %s", sym, type(e).__name__)
        snap["stocks"][sym] = entry
    return snap


# ---------- Claude ----------
def _prompt(snapshot: dict, headlines: dict, symbols: list[str]) -> str:
    def lines(items):
        return "\n".join(f"- [{h['time']}] {h['title']} ({h['source']})" for h in items) or "- (none found)"

    stock_news = "\n\n".join(f"### {sym}\n{lines(headlines['stocks'].get(sym, []))}" for sym in symbols)
    failed = ", ".join(headlines["failed_sources"]) or "none"
    return (f"Prepare today's briefing for these stocks: {', '.join(symbols)}.\n\n"
            f"## Market data (from the broker)\n```json\n{json.dumps(snapshot, indent=1)}\n```\n\n"
            f"## Market headlines (last 36h)\n{lines(headlines['market'])}\n\n"
            f"## Stock headlines (last 2 days)\n{stock_news}\n\n"
            f"News sources that failed to load: {failed}.\n"
            f"Return one entry in `stocks` for every symbol listed above.")


def find_claude_cli() -> str | None:
    """Path to the Claude Code CLI (CLAUDE_CLI_PATH in .env overrides the search)."""
    # the VS Code extension bundles one; pick the newest installed version
    vscode = sorted(Path.home().glob(".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"),
                    key=lambda p: [int(x) if x.isdigit() else 0
                                   for x in re.findall(r"\d+", p.parts[-4].split("claude-code-")[-1])])
    candidates = [os.getenv("CLAUDE_CLI_PATH", "").strip(), shutil.which("claude"),
                  str(Path.home() / ".local/bin/claude"), str(Path.home() / ".claude/local/claude"),
                  "/opt/homebrew/bin/claude", "/usr/local/bin/claude", *[str(p) for p in reversed(vscode)]]
    return next((c for c in candidates if c and Path(c).is_file() and os.access(c, os.X_OK)), None)


def _extract_json(text: str) -> dict:
    """The answer should be bare JSON; tolerate code fences or stray text around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in Claude's answer")
    return json.loads(text[start:end + 1])


def _check_schema(value, schema: dict, path: str = "analysis") -> None:
    """Minimal validation for the schema above (types, enums, required keys)."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} should be an object")
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                _check_schema(value[key], sub, f"{path}.{key}")
    elif t == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} should be a list")
        for i, item in enumerate(value):
            _check_schema(item, schema["items"], f"{path}[{i}]")
    elif t == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} should be text")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path}={value!r} is not one of {schema['enum']}")


def _run_cli(cli: str, prompt: str) -> dict:
    env = dict(os.environ)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(var, None)  # never fall back to (paid) API billing - use the subscription login
    with tempfile.TemporaryDirectory(prefix="briefing_") as empty_dir:  # no project files in reach
        proc = subprocess.run(
            [cli, "-p", "--output-format", "json", "--disallowedTools", *DISALLOWED_TOOLS],
            input=prompt, capture_output=True, text=True, timeout=CLI_TIMEOUT, cwd=empty_dir, env=env,
        )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude CLI returned an error: {str(out.get('result'))[:300]}")
    return out


def analyse(snapshot: dict, headlines: dict, symbols: list[str]) -> tuple[dict, dict]:
    """One Claude run through the Claude Code CLI. Returns (analysis, usage). Raises on failure."""
    cli = find_claude_cli()
    if not cli:
        raise FileNotFoundError("Claude Code CLI not found - install it and run `claude` once to log in "
                                "(or set CLAUDE_CLI_PATH in .env)")
    s = load_settings()
    direction = ("It opens longs when NIFTY 50 is in an uptrend and shorts when NIFTY 50 is in a downtrend."
                 if s.allow_short else "It only opens longs, and only when NIFTY 50 is in an uptrend.")
    prompt = (f"{SYSTEM_PROMPT.format(direction=direction)}\n\n{_prompt(snapshot, headlines, symbols)}\n\n"
              f"Respond with ONLY a JSON object that matches this JSON Schema - no prose, no code fences:\n"
              f"{json.dumps(ANALYSIS_SCHEMA)}")
    last_error = None
    for attempt in (1, 2):
        out = _run_cli(cli, prompt if attempt == 1 else
                       f"{prompt}\n\nYour previous answer was invalid ({last_error}). Return valid JSON only.")
        try:
            analysis = _extract_json(str(out.get("result", "")))
            _check_schema(analysis, ANALYSIS_SCHEMA)
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            log.warning("News briefing: Claude's answer wasn't valid JSON (%s)%s", e,
                        ", retrying once" if attempt == 1 else "")
    else:
        raise RuntimeError(f"no valid briefing after 2 attempts: {last_error}")
    u = out.get("usage") or {}
    usage = {"model": ", ".join(out.get("modelUsage", {}).keys()) or "Claude Code (subscription)",
             "input_tokens": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
             + u.get("cache_creation_input_tokens", 0),
             "output_tokens": u.get("output_tokens", 0), "request_id": out.get("session_id", "-"),
             "duration_s": round((out.get("duration_ms") or 0) / 1000)}
    return analysis, usage


# ---------- output ----------
def render_markdown(day: str, snapshot: dict, analysis: dict, headlines: dict, usage: dict) -> str:
    a = analysis
    out = [f"# Market briefing - {day}", "",
           f"*Generated {snapshot['as_of']} ({snapshot['session']}) by {usage.get('model')}. "
           f"Advisory only - the bot's trading rules are unchanged.*", "",
           f"## Mood: **{a['market_mood'].upper()}** (confidence: {a['confidence']})", "",
           a["summary"], "", f"**Caution:** {a['caution']}", "", "## Market data", "",
           "| Index | Last | Chg % | 5d % | 20d % | vs 20d EMA | vs 50d EMA |", "|---|---|---|---|---|---|---|"]
    for name, d in snapshot["indices"].items():
        out.append(f"| {name} | {d.get('last')} | {d.get('change_pct')} | {d.get('change_5d_pct')} | "
                   f"{d.get('change_20d_pct')} | {d.get('vs_20d_ema', '-')} | {d.get('vs_50d_ema', '-')} |")
    out += ["", "## Key drivers", ""]
    icon = {"positive": "+", "negative": "-", "neutral": "~"}
    out += [f"- ({icon[k['direction']]}) **{k['driver']}** - {k['evidence']}" for k in a["key_drivers"]] or ["- none"]
    out += ["", "## Event risks today", ""]
    out += [f"- **{e['event']}** ({e['affects']}): {e['note']}" for e in a["event_risks"]] or ["- none identified"]
    out += ["", "## Stocks", "", "| Stock | Outlook | Flags | Chg % | 5d % | Note |", "|---|---|---|---|---|---|"]
    for st in a["stocks"]:
        d = snapshot["stocks"].get(st["symbol"], {})
        out.append(f"| {st['symbol']} | {st['outlook']} | {', '.join(st['flags']) or '-'} | "
                   f"{d.get('change_pct')} | {d.get('change_5d_pct')} | {st['note']} |")
    out += ["", "### Headlines used", ""]
    for st in a["stocks"]:
        if st["headlines"]:
            out.append(f"**{st['symbol']}**")
            out += [f"- {h}" for h in st["headlines"]]
            out.append("")
    if headlines["failed_sources"]:
        out += [f"*Sources that failed to load: {', '.join(headlines['failed_sources'])}*", ""]
    out += [f"*Claude Code (subscription): {usage.get('input_tokens')} in / {usage.get('output_tokens')} out "
            f"tokens, {usage.get('duration_s')}s. Session: {usage.get('request_id')}*"]
    return "\n".join(out) + "\n"


def log_summary(analysis: dict) -> None:
    a = analysis
    log.info("News briefing: market %s (confidence %s) - %s", a["market_mood"].upper(), a["confidence"], a["caution"])
    for e in a["event_risks"]:
        log.info("News briefing: event risk - %s (%s)", e["event"], e["affects"])
    for st in a["stocks"]:
        if st["outlook"] != "no_news" or st["flags"]:
            log.info("News briefing: %s %s%s - %s", st["symbol"], st["outlook"],
                     f" [{', '.join(st['flags'])}]" if st["flags"] else "", st["note"])


VETO_EVENT_FLAGS = {"results_today", "major_news"}


def build_vetoes(analysis: dict) -> dict[str, dict[str, str]]:
    """Per-stock blocks for NEW positions from a briefing: {sym: {"long": reason, "short": reason}}.

    Rule 1 (event): results or major news today -> block both directions.
    Rule 2 (direction): bearish outlook -> block longs; bullish outlook -> block shorts.
    """
    vetoes = {}
    for st in analysis.get("stocks", []):
        sym, note = st["symbol"].upper(), st.get("note", "")[:120]
        events = VETO_EVENT_FLAGS.intersection(st.get("flags", []))
        block = {}
        if events:
            reason = f"news event today ({', '.join(sorted(events))}): {note}"
            block = {"long": reason, "short": reason}
        elif st.get("outlook") == "bearish":
            block = {"long": f"bearish news outlook: {note}"}
        elif st.get("outlook") == "bullish":
            block = {"short": f"bullish news outlook: {note}"}
        if block:
            vetoes[sym] = block
    return vetoes


def names_for(s: Settings) -> dict[str, str]:
    names = dict(COMPANY_NAMES)
    for pair in os.getenv("NEWS_NAMES", "").split(","):
        if ":" in pair:
            sym, name = pair.split(":", 1)
            names[sym.strip().upper()] = name.strip()
    return names


def run_briefing(kite, s: Settings, instruments: dict[str, dict], refresh: bool = False) -> dict | None:
    """Builds today's briefing once. Never raises - trading must not depend on it."""
    day = datetime.now(IST).strftime("%Y-%m-%d")
    json_path, md_path = LOG_DIR / f"news_{day}.json", LOG_DIR / f"news_{day}.md"
    try:
        if json_path.exists() and not refresh:
            saved = json.loads(json_path.read_text())
            log.info("News briefing: already generated today, see %s", md_path.name)
            log_summary(saved["analysis"])
            return saved["analysis"]
        if not find_claude_cli():
            log.warning("News briefing skipped: Claude Code CLI not found - install it and log in once "
                        "(see README), or set CLAUDE_CLI_PATH in .env")
            return None
        started = _time.time()
        log.info("News briefing: fetching news and market data...")
        headlines = collect_headlines(s.symbols, names_for(s))
        snapshot = market_snapshot(kite, s, instruments)
        log.info("News briefing: %d market + %d stock headlines, asking Claude (subscription)...",
                 len(headlines["market"]), sum(len(v) for v in headlines["stocks"].values()))
        analysis, usage = analyse(snapshot, headlines, s.symbols)
        json_path.write_text(json.dumps({"date": day, "snapshot": snapshot, "headlines": headlines,
                                         "analysis": analysis, "usage": usage}, indent=2))
        md_path.write_text(render_markdown(day, snapshot, analysis, headlines, usage))
        log.info("News briefing ready in %.0fs - %s (%d in / %d out tokens)", _time.time() - started,
                 md_path.name, usage["input_tokens"], usage["output_tokens"])
        log_summary(analysis)
        return analysis
    except subprocess.TimeoutExpired:
        log.warning("News briefing skipped: Claude took longer than %ds", CLI_TIMEOUT)
    except Exception as e:
        log.warning("News briefing skipped: %s: %s", type(e).__name__, e)
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description="Generate today's market briefing")
    p.add_argument("--refresh", action="store_true", help="regenerate even if today's briefing exists")
    args = p.parse_args()
    from auth import get_kite
    from data import resolve_instruments
    settings = load_settings()
    k = get_kite(settings)
    if run_briefing(k, settings, resolve_instruments(k, settings), refresh=args.refresh):
        print(f"\nSee {LOG_DIR / ('news_' + datetime.now(IST).strftime('%Y-%m-%d') + '.md')}")
