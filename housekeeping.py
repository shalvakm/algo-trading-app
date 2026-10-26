"""Remove log files and CSV rows older than KEEP_DAYS (run once at bot startup).

Only touches files in logs/:
  - dated files: bot_YYYY-MM-DD.log, news_YYYY-MM-DD.md/.json, watchlist_YYYY-MM-DD.csv, scan_YYYY-MM-DD_HHMM.csv
  - undated outputs by last-modified time: backtest_*.csv, trades.backup*.csv
  - rows older than the cutoff in trades.csv and news_vetoes.csv (the files themselves are kept)
Never touches state/ (positions, Kite login, caches).
"""
import logging
import os
import re
from datetime import datetime, timedelta

from config import IST, LOG_DIR

log = logging.getLogger("housekeeping")
DATED = re.compile(r"^(bot|news|watchlist|scan)_(\d{4}-\d{2}-\d{2})")
BY_MTIME = ("backtest_", "trades.backup")
ROW_FILES = ("trades.csv", "news_vetoes.csv")


def _prune_rows(path, cutoff: str) -> int:
    """Drop rows whose first column (an ISO date/time) is before cutoff; keeps the header and day gaps."""
    lines = open(path, newline="").read().splitlines()
    if len(lines) < 2:
        return 0
    header, body = lines[0], lines[1:]
    keep = [ln for ln in body if not ln.strip() or ln[:10] >= cutoff]
    removed = sum(1 for ln in body if ln.strip()) - sum(1 for ln in keep if ln.strip())
    if not removed:
        return 0
    out, prev_blank = [], True  # also drops a blank line right after the header
    for ln in keep:
        blank = not ln.strip()
        if not (blank and prev_blank):
            out.append("" if blank else ln)
        prev_blank = blank
    while out and not out[-1]:
        out.pop()
    tmp = f"{path}.tmp"
    with open(tmp, "w", newline="") as f:
        f.write("\r\n".join([header] + out) + "\r\n")
    os.replace(tmp, path)
    return removed


def cleanup(keep_days: int) -> None:
    if keep_days <= 0:
        return
    now = datetime.now(IST)
    cutoff = (now.date() - timedelta(days=keep_days)).isoformat()
    cutoff_ts = (now - timedelta(days=keep_days)).timestamp()
    files = 0
    for f in LOG_DIR.iterdir():
        if not f.is_file() or f.name in ROW_FILES:
            continue
        m = DATED.match(f.name)
        old = (m and m.group(2) < cutoff) or (f.name.startswith(BY_MTIME) and f.stat().st_mtime < cutoff_ts)
        if old:
            try:
                f.unlink()
                files += 1
            except OSError as e:
                log.warning("Couldn't delete %s: %s", f.name, e)
    rows = {}
    for name in ROW_FILES:
        p = LOG_DIR / name
        if p.exists():
            try:
                rows[name] = _prune_rows(p, cutoff)
            except Exception as e:
                log.warning("Couldn't prune %s: %s", name, e)
    if files or any(rows.values()):
        log.info("Housekeeping: removed %d log file(s) and %s older than %s (KEEP_DAYS=%d)", files,
                 ", ".join(f"{n} {name}" for name, n in rows.items() if n) or "no CSV rows", cutoff, keep_days)
