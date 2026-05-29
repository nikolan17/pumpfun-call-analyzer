#!/usr/bin/env python3
"""
pull_calls.py

Measure the performance of Solana pump.fun memecoin "calls" using the free
GeckoTerminal public API (DexScreener as a fallback for metadata / current price).

For each call (mint + call_time) the script:
  - finds the main pool (highest 24h volume / reserve_in_usd),
  - pulls 1-minute USD OHLCV back to the call time (falling back to 5m, then 1h),
  - converts price -> market cap using pump.fun's fixed 1e9 supply
    (with a sanity check against fdv_usd),
  - computes entry / peak / now market caps, gains, drawdown, minutes-to-peak,
  - prints a table + aggregate stats,
  - writes calls_analysis.csv and ./ohlcv/{symbol}.csv.

No API key required. Run:  python pull_calls.py
"""

from __future__ import annotations

import csv
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Force UTF-8 stdout/stderr so output renders on consoles with legacy codepages
# (e.g. Windows cp1251/cp1252). Falls back silently if reconfigure is unavailable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# CONFIG  — add future calls here. (mint address, call time in UTC)
# Call time is 22:00 Europe/Berlin = 20:00 UTC.
# ---------------------------------------------------------------------------

CALLS = [
    # (symbol, mint, call_time_utc_iso)
    ("HOPPY",  "2RWndXkxWkaKhGjE7dZivVbK5qXtpwnCZJ1jpnxapump", "2026-05-25T20:00:00Z"),
    ("CHUBBY", "2kBH6UcR8TitebUbKNYNvjUkSFWaiFBJ7nAPTKQwpump", "2026-05-26T20:00:00Z"),
    ("FCM",    "Hkpi2SkNWm5LogyY1Bz4zYTq5REVvco2aYWd1tYppump", "2026-05-27T20:00:00Z"),
    ("250",    "BUG9jJ6MZcxbXaDVqq199HvWafMSP4pYHVFrZfLmpump", "2026-05-28T20:00:00Z"),
]

NETWORK = "solana"
PUMPFUN_SUPPLY = 1_000_000_000  # 1e9 fully-circulating supply

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS = {"Accept": "application/json;version=20230302"}
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"

GECKO_SLEEP = 2.0          # ~2s between GeckoTerminal calls (free tier ~30/min)
OHLCV_PAGE_LIMIT = 1000    # max candles per request
CSV_OUT = "calls_analysis.csv"
OHLCV_DIR = "ohlcv"

# Timeframe fallback order: (path, aggregate). Minute first, then 5m, then hour.
TIMEFRAMES = [
    ("minute", 1),
    ("minute", 5),
    ("hour", 1),
]

# ---------------------------------------------------------------------------
# HTTP session with retries
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = build_session()


def gecko_get(path: str, params: dict | None = None) -> dict | None:
    """GET against GeckoTerminal with the versioned Accept header + polite sleep."""
    url = f"{GECKO_BASE}{path}"
    try:
        resp = SESSION.get(url, headers=GECKO_HEADERS, params=params, timeout=30)
    except requests.RequestException as exc:
        print(f"  ! GeckoTerminal request failed ({url}): {exc}")
        time.sleep(GECKO_SLEEP)
        return None
    time.sleep(GECKO_SLEEP)
    if resp.status_code != 200:
        print(f"  ! GeckoTerminal {resp.status_code} for {url}")
        return None
    try:
        return resp.json()
    except ValueError:
        print(f"  ! GeckoTerminal returned non-JSON for {url}")
        return None


def dexscreener_get(mint: str) -> dict | None:
    url = f"{DEXSCREENER_BASE}/tokens/{mint}"
    try:
        resp = SESSION.get(url, timeout=30)
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_call_time(iso: str) -> datetime:
    """Parse an ISO-8601 (Z-suffixed) string into a tz-aware UTC datetime."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_money(value: float | None) -> str:
    """Human-readable money: $1.23K / $4.56M / $7.89B."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    abs_v = abs(value)
    if abs_v >= 1e9:
        return f"${value / 1e9:.2f}B"
    if abs_v >= 1e6:
        return f"${value / 1e6:.2f}M"
    if abs_v >= 1e3:
        return f"${value / 1e3:.2f}K"
    return f"${value:.2f}"


def fmt_pct(value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:+.1f}%"


def fmt_mult(value: float | None) -> str:
    """Express a gain pct as an x-multiple, e.g. +900% -> 10.0x."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value / 100 + 1:.1f}x"


# ---------------------------------------------------------------------------
# Pool selection
# ---------------------------------------------------------------------------

def _pool_score(pool: dict) -> tuple[float, float]:
    attrs = pool.get("attributes", {})
    vol = attrs.get("volume_usd", {}) or {}
    v24 = to_float(vol.get("h24")) or 0.0
    reserve = to_float(attrs.get("reserve_in_usd")) or 0.0
    return (v24, reserve)


def list_pools(mint: str) -> list[dict]:
    """
    Return ALL of a token's pools, sorted highest 24h volume first
    (tie-break reserve_in_usd). pump.fun tokens trade on a bonding-curve pool
    at launch then migrate to PumpSwap/Raydium, so the launch candles and the
    post-migration candles live on DIFFERENT pools — we need them all.
    """
    data = gecko_get(f"/networks/{NETWORK}/tokens/{mint}/pools")
    if not data or "data" not in data or not data["data"]:
        return []
    return sorted(data["data"], key=_pool_score, reverse=True)


def pick_best_pool(mint: str) -> dict | None:
    """Highest 24h volume pool — used for fdv_usd / supply sanity checks."""
    pools = list_pools(mint)
    return pools[0] if pools else None


def pool_address(pool: dict) -> str:
    attrs = pool.get("attributes", {})
    return attrs.get("address") or pool.get("id", "").split("_")[-1]


# ---------------------------------------------------------------------------
# OHLCV pulling (paginate backward to cover call_time -> now)
# ---------------------------------------------------------------------------

def pull_ohlcv_for_timeframe(pool_addr: str, timeframe: str, aggregate: int) -> list[list[float]]:
    """
    Pull OHLCV rows for one timeframe, paginating backward via before_timestamp
    all the way to the pool's FIRST candle (genesis) — i.e. until a short page
    or no further progress. Capturing genesis is what lets us see the true
    launch floor once pools are merged.
    Returns rows [ts, open, high, low, close, volume] sorted ascending.
    """
    collected: dict[int, list[float]] = {}
    before_ts = int(now_utc().timestamp())
    path = f"/networks/{NETWORK}/pools/{pool_addr}/ohlcv/{timeframe}"

    while True:
        params = {
            "aggregate": aggregate,
            "limit": OHLCV_PAGE_LIMIT,
            "currency": "usd",
            "before_timestamp": before_ts,
        }
        data = gecko_get(path, params=params)
        if not data:
            break
        ohlcv_list = (
            data.get("data", {})
                .get("attributes", {})
                .get("ohlcv_list", [])
        )
        if not ohlcv_list:
            break

        page_min_ts = None
        for row in ohlcv_list:
            if not row or len(row) < 6:
                continue
            ts = int(row[0])
            collected[ts] = [
                ts,
                to_float(row[1]) or 0.0,
                to_float(row[2]) or 0.0,
                to_float(row[3]) or 0.0,
                to_float(row[4]) or 0.0,
                to_float(row[5]) or 0.0,
            ]
            page_min_ts = ts if page_min_ts is None else min(page_min_ts, ts)

        if page_min_ts is None:
            break

        if len(ohlcv_list) < OHLCV_PAGE_LIMIT:
            # Fewer than a full page => we've hit the pool's first candle (genesis).
            break

        next_before = page_min_ts
        if next_before >= before_ts:  # no progress, avoid infinite loop
            break
        before_ts = next_before

    rows = [collected[ts] for ts in sorted(collected.keys())]
    return rows


def pull_ohlcv(pool_addr: str, call_time: datetime) -> tuple[list[list[float]], str]:
    """
    Pull one pool's full history. Try minute(1) -> minute(5) -> hour(1):
    accept the first timeframe whose genesis reaches back to (or before) the
    call time; otherwise keep whichever timeframe returned the most candles.
    Returns (rows, label).
    """
    call_unix = int(call_time.timestamp())
    best_rows: list[list[float]] = []
    best_label = "none"

    for timeframe, aggregate in TIMEFRAMES:
        label = f"{timeframe}/{aggregate}"
        rows = pull_ohlcv_for_timeframe(pool_addr, timeframe, aggregate)
        if not rows:
            continue
        if len(rows) > len(best_rows):
            best_rows, best_label = rows, label
        if rows[0][0] <= call_unix:
            # This pool's history reaches the call time — good enough.
            return rows, label

    return best_rows, best_label


def is_base_token_pool(pool: dict, mint: str) -> bool:
    """
    True if our mint is the BASE token of the pool. GeckoTerminal's
    ohlcv?currency=usd prices the pool's BASE token, so a pool where our mint is
    the QUOTE side (e.g. 'PEPE / OURTOKEN') would inject a DIFFERENT token's
    price into the merge — those must be excluded.
    """
    base_id = (pool.get("relationships", {})
                   .get("base_token", {}).get("data", {}).get("id", ""))
    return base_id == f"{NETWORK}_{mint}"


def pull_ohlcv_merged(pools: list[dict], call_time: datetime, mint: str
                      ) -> tuple[list[list[float]], list[float] | None, list[dict]]:
    """
    Pull OHLCV from every pool that prices OUR token (base == mint) and merge
    into ONE continuous series keyed by timestamp, so a token's bonding-curve
    launch pool is stitched to its migrated PumpSwap/Raydium pool.

    Selection rule: pools are ranked by overall liquidity (24h volume, then
    reserve) and each minute is claimed by the HIGHEST-RANKED pool that traded
    it; lower-ranked pools only fill minutes the dominant pool lacks (i.e. the
    pre-migration launch candles and gaps). This is a deliberate refinement of
    "higher volume wins per minute": ranking by per-CANDLE volume let a thin pool
    win a quiet minute with a single garbage-priced trade (a 150x wick), so we
    rank by per-POOL volume instead — robust against micro-liquidity wicks while
    still extending history backward across the migration.

    Returns (merged_rows_ascending, primary_pool_rows, per_pool_info).
    primary_pool_rows is the highest-volume pool's series (authoritative for the
    current/live price).
    """
    base_pools = [p for p in pools if is_base_token_pool(p, mint)]
    dropped = len(pools) - len(base_pools)
    if dropped:
        print(f"  (skipping {dropped} pool(s) where {mint[:6]}.. is the quote token)")
    base_pools.sort(key=_pool_score, reverse=True)  # dominant pool first

    merged: dict[int, list[float]] = {}
    primary_rows: list[list[float]] | None = None
    info: list[dict] = []

    for rank, pool in enumerate(base_pools):
        attrs = pool.get("attributes", {})
        addr = pool_address(pool)
        name = attrs.get("name", "?")
        v24 = to_float((attrs.get("volume_usd") or {}).get("h24")) or 0.0
        print(f"  · pool {name} ({addr})  24h vol {fmt_money(v24)} — pulling OHLCV ...")
        rows, label = pull_ohlcv(addr, call_time)
        if rank == 0:
            primary_rows = rows
        first_dt = (datetime.fromtimestamp(rows[0][0], timezone.utc).isoformat()
                    if rows else "n/a")
        added = 0
        for r in rows:
            ts = r[0]
            if ts not in merged:        # dominant pool already claimed this minute
                merged[ts] = r
                added += 1
        info.append({
            "addr": addr, "name": name, "label": label,
            "n": len(rows), "v24": v24, "first_dt": first_dt, "added": added,
        })
        print(f"    [{label}] {len(rows)} candles (first {first_dt}); "
              f"contributed {added} new minute(s)")

    rows = [merged[ts] for ts in sorted(merged.keys())]
    return rows, primary_rows, info


# ---------------------------------------------------------------------------
# Supply / market-cap resolution
# ---------------------------------------------------------------------------

def resolve_supply(pool: dict, latest_close: float | None) -> tuple[float, str]:
    """
    pump.fun = fixed 1e9 supply. Sanity-check fdv_usd ≈ latest_close * 1e9.
    If it diverges >15%, derive supply = fdv_usd / latest_close.
    `latest_close` must come from the PRIMARY (live) pool, not the merged series
    (a dead micro-pool can carry a later, garbage-priced final candle).
    Returns (supply, note).
    """
    attrs = pool.get("attributes", {}) if pool else {}
    fdv = to_float(attrs.get("fdv_usd"))

    if fdv and latest_close and latest_close > 0:
        implied = fdv / latest_close
        ratio = implied / PUMPFUN_SUPPLY
        if ratio < 0.85 or ratio > 1.15:
            return implied, (
                f"supply derived from fdv ({implied:,.0f}; "
                f"fdv≈{fmt_money(fdv)} vs 1e9*close≈{fmt_money(latest_close*PUMPFUN_SUPPLY)})"
            )
        return PUMPFUN_SUPPLY, f"1e9 supply confirmed (fdv check ratio {ratio:.2f})"
    return PUMPFUN_SUPPLY, "1e9 supply (no fdv sanity data)"


# ---------------------------------------------------------------------------
# Per-call analysis
# ---------------------------------------------------------------------------

@dataclass
class Result:
    symbol: str
    mint: str
    call_time: datetime
    channel: str = ""       # source channel (populated by scan_telegram.py)
    msg_id: int | str = ""  # source message id (populated by scan_telegram.py)
    pool_addr: str | None = None
    timeframe: str = "none"
    supply: float = float(PUMPFUN_SUPPLY)
    supply_note: str = ""
    earliest_mc: float | None = None      # MC at merged-series first candle
    earliest_ts: datetime | None = None    # (earliest price GeckoTerminal has indexed)
    entry_mc: float | None = None
    peak_mc: float | None = None
    peak_ts: datetime | None = None
    now_mc: float | None = None
    peak_gain_pct: float | None = None
    now_gain_pct: float | None = None
    drawdown_pct: float | None = None
    minutes_to_peak: float | None = None
    error: str = ""
    raw_rows: list = field(default_factory=list)


def analyze_call(symbol: str, mint: str, call_iso: str,
                 channel: str = "", msg_id: int | str = "") -> Result:
    call_time = parse_call_time(call_iso)
    res = Result(symbol=symbol, mint=mint, call_time=call_time,
                 channel=channel, msg_id=msg_id)
    print(f"\n=== {symbol}  ({mint}) ===")
    print(f"  call time (UTC): {call_time.isoformat()}")

    pools = list_pools(mint)
    if not pools:
        res.error = "no pool found"
        print(f"  ! WARNING: no pool found for {symbol}; skipping.")
        return res

    primary = pools[0]  # highest-volume pool: holds current fdv_usd for supply check
    print(f"  found {len(pools)} pool(s); merging full history across token's pools")

    rows, primary_rows, info = pull_ohlcv_merged(pools, call_time, mint)
    merged_pools = [i for i in info if i["added"] > 0]
    res.pool_addr = ";".join(i["addr"] for i in merged_pools)
    labels = sorted({i["label"] for i in merged_pools})
    res.timeframe = "+".join(labels) if labels else "none"
    res.raw_rows = rows
    if not rows:
        res.error = "no OHLCV data"
        print(f"  ! WARNING: no OHLCV data for {symbol}; skipping.")
        return res

    # Live price comes from the primary (highest-volume) pool's latest close,
    # never the merged tail (a dead micro-pool may carry a later junk candle).
    now_close = primary_rows[-1][4] if primary_rows else rows[-1][4]

    supply, supply_note = resolve_supply(primary, now_close)
    res.supply = supply
    res.supply_note = supply_note
    print(f"  {supply_note}")

    # Merged-series genesis = earliest price GeckoTerminal has indexed for this
    # token. NOT necessarily the bonding-curve floor (Gecko's history may start
    # later) — the call-time entry below is the number that matters.
    earliest_ts = datetime.fromtimestamp(rows[0][0], timezone.utc)
    earliest_mc = rows[0][1] * supply  # open of first-ever candle
    res.earliest_mc = earliest_mc
    res.earliest_ts = earliest_ts
    print(f"  merged {len(merged_pools)} pool(s), {len(rows)} candles | "
          f"EARLIEST OBSERVED MC {fmt_money(earliest_mc)} @ {earliest_ts.isoformat()} "
          f"(open of first candle)")

    call_unix = int(call_time.timestamp())
    after = [r for r in rows if r[0] >= call_unix]
    if not after:
        res.error = (
            f"OHLCV starts {datetime.fromtimestamp(rows[0][0], timezone.utc).isoformat()} "
            f"— after call time; cannot compute entry"
        )
        print(f"  ! WARNING: {res.error}")
        return res

    # entry_mc = MC at first candle ts >= call_time (open)
    entry_row = after[0]
    res.entry_mc = entry_row[1] * supply

    # peak_mc, peak_ts = max(high) over candles ts >= call_time
    peak_row = max(after, key=lambda r: r[2])
    res.peak_mc = peak_row[2] * supply
    res.peak_ts = datetime.fromtimestamp(peak_row[0], timezone.utc)

    # now_mc = MC at primary pool's latest close (live price)
    res.now_mc = now_close * supply

    if res.entry_mc and res.entry_mc > 0:
        res.peak_gain_pct = (res.peak_mc / res.entry_mc - 1) * 100
        res.now_gain_pct = (res.now_mc / res.entry_mc - 1) * 100
    if res.peak_mc and res.peak_mc > 0:
        res.drawdown_pct = (res.now_mc / res.peak_mc - 1) * 100
    res.minutes_to_peak = (peak_row[0] - call_unix) / 60.0

    print(f"  entry MC {fmt_money(res.entry_mc)} | ATH {fmt_money(res.peak_mc)} "
          f"({fmt_mult(res.peak_gain_pct)}) | now {fmt_money(res.now_mc)} "
          f"({fmt_pct(res.now_gain_pct)}) | dd {fmt_pct(res.drawdown_pct)} "
          f"| {res.minutes_to_peak:.0f}m to peak")
    return res


# ---------------------------------------------------------------------------
# Output: table, aggregates, CSV
# ---------------------------------------------------------------------------

def print_table(results: list[Result]) -> None:
    header = (
        f"{'SYMBOL':<8} {'ENTRY MC':>11} {'ATH':>11} {'PEAK':>8} "
        f"{'NOW MC':>11} {'DRAWDN':>8} {'MIN2PK':>7} {'TF':>9}"
    )
    print("\n" + "=" * len(header))
    print("CALL PERFORMANCE")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in results:
        if r.error and r.entry_mc is None:
            print(f"{r.symbol:<8} {'— ' + r.error:<60}")
            continue
        mins = f"{r.minutes_to_peak:.0f}" if r.minutes_to_peak is not None else "n/a"
        print(
            f"{r.symbol:<8} {fmt_money(r.entry_mc):>11} {fmt_money(r.peak_mc):>11} "
            f"{fmt_mult(r.peak_gain_pct):>8} {fmt_money(r.now_mc):>11} "
            f"{fmt_pct(r.drawdown_pct):>8} {mins:>7} {r.timeframe:>9}"
        )
    print("-" * len(header))


def print_aggregates(results: list[Result]) -> None:
    valid = [r for r in results if r.peak_gain_pct is not None]
    if not valid:
        print("\nNo valid results for aggregates.")
        return

    peak_gains = [r.peak_gain_pct for r in valid]
    drawdowns = [r.drawdown_pct for r in valid if r.drawdown_pct is not None]
    # multiples relative to entry: peak_mc/entry_mc = peak_gain_pct/100 + 1
    mults = [g / 100 + 1 for g in peak_gains]

    n_2x = sum(1 for m in mults if m >= 2)
    n_10x = sum(1 for m in mults if m >= 10)
    n_50x = sum(1 for m in mults if m >= 50)

    print("\nAGGREGATES")
    print("-" * 40)
    print(f"  calls analyzed       : {len(valid)} / {len(results)}")
    print(f"  mean peak gain       : {fmt_pct(statistics.mean(peak_gains))} "
          f"({statistics.mean(mults):.1f}x)")
    print(f"  median peak gain     : {fmt_pct(statistics.median(peak_gains))} "
          f"({statistics.median(mults):.1f}x)")
    print(f"  hit >= 2x            : {n_2x} / {len(valid)}")
    print(f"  hit >= 10x           : {n_10x} / {len(valid)}")
    print(f"  hit >= 50x           : {n_50x} / {len(valid)}")
    if drawdowns:
        print(f"  mean drawdown        : {fmt_pct(statistics.mean(drawdowns))}")
    print("-" * 40)


def write_csv(results: list[Result]) -> None:
    fields = [
        "symbol", "mint", "channel", "msg_id", "call_time_utc", "pool_addr",
        "timeframe", "supply",
        "earliest_mc", "earliest_ts_utc",
        "entry_mc", "peak_mc", "peak_ts_utc", "now_mc",
        "peak_gain_pct", "now_gain_pct", "drawdown_pct", "minutes_to_peak",
        "error",
    ]
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for r in results:
            writer.writerow([
                r.symbol,
                r.mint,
                r.channel,
                r.msg_id,
                r.call_time.isoformat(),
                r.pool_addr or "",
                r.timeframe,
                f"{r.supply:.0f}",
                f"{r.earliest_mc:.2f}" if r.earliest_mc is not None else "",
                r.earliest_ts.isoformat() if r.earliest_ts else "",
                f"{r.entry_mc:.2f}" if r.entry_mc is not None else "",
                f"{r.peak_mc:.2f}" if r.peak_mc is not None else "",
                r.peak_ts.isoformat() if r.peak_ts else "",
                f"{r.now_mc:.2f}" if r.now_mc is not None else "",
                f"{r.peak_gain_pct:.2f}" if r.peak_gain_pct is not None else "",
                f"{r.now_gain_pct:.2f}" if r.now_gain_pct is not None else "",
                f"{r.drawdown_pct:.2f}" if r.drawdown_pct is not None else "",
                f"{r.minutes_to_peak:.1f}" if r.minutes_to_peak is not None else "",
                r.error,
            ])
    print(f"\nWrote {CSV_OUT}")


def write_raw_ohlcv(results: list[Result]) -> None:
    os.makedirs(OHLCV_DIR, exist_ok=True)
    for r in results:
        if not r.raw_rows:
            continue
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in r.symbol) or r.mint[:8]
        path = os.path.join(OHLCV_DIR, f"{safe}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ts_unix", "datetime_utc", "open", "high", "low", "close", "volume"])
            for row in r.raw_rows:
                dt = datetime.fromtimestamp(row[0], timezone.utc).isoformat()
                writer.writerow([row[0], dt, row[1], row[2], row[3], row[4], row[5]])
    print(f"Wrote raw OHLCV to ./{OHLCV_DIR}/")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _opt_float(value) -> float | None:
    try:
        if value in (None, "", "nan"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def result_from_row(row: dict) -> Result:
    """Reconstruct a Result from a calls_analysis.csv row (cached prior analysis).
    Tolerates the older 'launch_mc/launch_ts_utc' header as well as the current
    'earliest_mc/earliest_ts_utc'. raw_rows stays empty (we don't re-pull OHLCV)."""
    r = Result(
        symbol=row.get("symbol", "") or "",
        mint=row.get("mint", "") or "",
        call_time=_opt_dt(row.get("call_time_utc")) or now_utc(),
        channel=row.get("channel", "") or "",
        msg_id=row.get("msg_id", "") or "",
    )
    r.pool_addr = row.get("pool_addr") or None
    r.timeframe = row.get("timeframe") or "none"
    r.supply = _opt_float(row.get("supply")) or float(PUMPFUN_SUPPLY)
    r.earliest_mc = _opt_float(row.get("earliest_mc") or row.get("launch_mc"))
    r.earliest_ts = _opt_dt(row.get("earliest_ts_utc") or row.get("launch_ts_utc"))
    r.entry_mc = _opt_float(row.get("entry_mc"))
    r.peak_mc = _opt_float(row.get("peak_mc"))
    r.peak_ts = _opt_dt(row.get("peak_ts_utc"))
    r.now_mc = _opt_float(row.get("now_mc"))
    r.peak_gain_pct = _opt_float(row.get("peak_gain_pct"))
    r.now_gain_pct = _opt_float(row.get("now_gain_pct"))
    r.drawdown_pct = _opt_float(row.get("drawdown_pct"))
    r.minutes_to_peak = _opt_float(row.get("minutes_to_peak"))
    r.error = row.get("error", "") or ""
    return r


def load_results_csv(path: str = CSV_OUT) -> list[Result]:
    """Load previously-computed results from calls_analysis.csv (empty if absent)."""
    if not os.path.exists(path):
        return []
    out: list[Result] = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out.append(result_from_row(row))
    except (OSError, csv.Error):
        return []
    return out


def analyze_calls(calls: list) -> list[Result]:
    """
    Analyze a list of calls and print table + aggregates, returning Result objects.
    Does NOT write any CSV (callers decide how to persist — e.g. an incremental
    merge with cached prior results). `calls` items may be either:
      - a 3-tuple (symbol, mint, call_iso), or
      - a dict with keys: symbol, mint, call_iso, and optional channel, msg_id.
    """
    print(f"pull_calls.py — analyzing {len(calls)} call(s) via GeckoTerminal")
    print(f"run time (UTC): {now_utc().isoformat()}")

    results: list[Result] = []
    for call in calls:
        if isinstance(call, dict):
            symbol = call.get("symbol") or call.get("mint", "")[:8]
            mint = call["mint"]
            call_iso = call["call_iso"]
            channel = call.get("channel", "")
            msg_id = call.get("msg_id", "")
        else:
            symbol, mint, call_iso = call
            channel, msg_id = "", ""
        try:
            results.append(analyze_call(symbol, mint, call_iso, channel, msg_id))
        except Exception as exc:  # never let one bad token kill the run
            print(f"  ! ERROR analyzing {symbol}: {exc}")
            r = Result(symbol=symbol, mint=mint, call_time=parse_call_time(call_iso),
                       channel=channel, msg_id=msg_id)
            r.error = f"exception: {exc}"
            results.append(r)

    print_table(results)
    print_aggregates(results)
    return results


def run_analysis(calls: list) -> list[Result]:
    """Analyze `calls` and write calls_analysis.csv + raw OHLCV. Importable entry
    point used by pull_calls standalone; scan_telegram does an incremental merge."""
    results = analyze_calls(calls)
    write_csv(results)
    write_raw_ohlcv(results)
    return results


def main() -> int:
    run_analysis(list(CALLS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
