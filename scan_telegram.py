#!/usr/bin/env python3
"""
scan_telegram.py

Telegram ingestion + Solana call-detection that feeds pull_calls.py.

Two ingestion paths, selected by the SOURCE config flag:
  - SOURCE="web"      : scrape the public web preview at https://t.me/s/{channel}.
                        No API key. PUBLIC channels only. Backfill only (no live mode).
  - SOURCE="telethon" : Telethon user session. Private channels + realtime LIVE mode.
                        Needs API_ID/API_HASH from my.telegram.org in a .env file.

Flow (both paths):
  1. Read recent messages from the configured channels.
  2. Detect "calls" = messages containing a Solana mint (base58 + pump.fun/dexscreener/
     solscan/birdeye URLs, including mints hidden inside <a href> links).
  3. Validate each candidate against GeckoTerminal; drop false positives.
  4. Dedupe (one call per channel+mint, keep earliest) across runs, write calls_detected.csv.
  5. Run pull_calls.run_analysis() on the detected rows -> calls_analysis.csv + enriched
     table, plus a PER-CHANNEL summary.

Setup:
  pip install requests beautifulsoup4 python-dotenv     # web path
  pip install telethon                                  # telethon path only
  For telethon: create a .env (see .env.example) with API_ID, API_HASH, SESSION_NAME;
  the logged-in account must already be a member of the target channels.

Run:  python scan_telegram.py
"""

from __future__ import annotations

import asyncio
import csv
import html as html_lib
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# Telethon is optional: only the SOURCE="telethon" path needs it. The web path
# (SOURCE="web") works with no API key, so don't hard-fail if it's missing.
try:
    from telethon import TelegramClient, events
    from telethon.errors import FloodWaitError
    TELETHON_AVAILABLE = True
except ImportError:
    TelegramClient = events = FloodWaitError = None
    TELETHON_AVAILABLE = False

# Reuse the analysis engine + rate-limited GeckoTerminal client from pull_calls.py
import pull_calls
from pull_calls import gecko_get, run_analysis, NETWORK

# ---------------------------------------------------------------------------
# CONFIG — edit channels / limits here.
# ---------------------------------------------------------------------------

SOURCE = "web"               # "web" (no API key, public channels, backfill only)
                             #   or "telethon" (user session, private channels, live mode)
CHANNELS = ["devcabal"]      # @usernames or t.me links
BACKFILL_LIMIT = 1000        # recent messages to scan per channel
LIVE = False                 # telethon-only: also listen for new messages in real time

DETECTED_CSV = "calls_detected.csv"
LAST_UPDATED_FILE = "last_updated.txt"  # data-refresh stamp shown by the dashboard
CHANNEL_SLEEP = 3.0          # seconds to sleep between channels (rate-limit courtesy)
REFRESH_WINDOW_DAYS = 7      # re-analyze new calls + any call from the last N days;
                             #   older calls reuse their cached calls_analysis.csv result

# Web preview (t.me/s/) settings
WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
WEB_PAGE_SLEEP = 1.5         # seconds between t.me/s/ page fetches

# ---------------------------------------------------------------------------
# .env / credentials
# ---------------------------------------------------------------------------

load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "scan_session")

# ---------------------------------------------------------------------------
# Mint detection
# ---------------------------------------------------------------------------

# Base58 alphabet excludes 0 (zero), O (capital o), I (capital i), l (lower L).
BASE58 = r"[1-9A-HJ-NP-Za-km-z]"
# A standalone Solana mint: 32-44 base58 chars, not glued to other word chars.
MINT_RE = re.compile(rf"(?<![1-9A-HJ-NP-Za-km-z]){BASE58}{{32,44}}(?![1-9A-HJ-NP-Za-km-z])")

# Mint embedded in a known explorer / launchpad URL path.
URL_MINT_RE = re.compile(
    r"(?:pump\.fun/(?:coin/)?|"
    r"dexscreener\.com/solana/|"
    r"(?:www\.)?solscan\.io/(?:token|account)/|"
    r"birdeye\.so/token/|"
    r"(?:www\.)?gmgn\.ai/sol/token/)"
    rf"({BASE58}{{32,44}})",
    re.IGNORECASE,
)


def extract_mints(text: str) -> list[str]:
    """Return unique Solana mint candidates from a message, URL hits first."""
    if not text:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    # URL-embedded mints are highest-confidence — collect them first.
    for m in URL_MINT_RE.findall(text):
        if m not in seen:
            seen.add(m)
            candidates.append(m)
    # Then any bare base58 string of mint length.
    for m in MINT_RE.findall(text):
        if m not in seen:
            seen.add(m)
            candidates.append(m)
    # Stable sort so "...pump" suffixed (pump.fun) candidates are tried first,
    # preserving original discovery order within each group.
    order = {c: i for i, c in enumerate(candidates)}
    candidates.sort(key=lambda c: (not c.endswith("pump"), order[c]))
    return candidates


# ---------------------------------------------------------------------------
# Validation against GeckoTerminal (drops false-positive base58 strings)
# ---------------------------------------------------------------------------

_validation_cache: dict[str, str | None] = {}


def validate_mint(mint: str) -> str | None:
    """
    Confirm a candidate resolves as a Solana token on GeckoTerminal.
    Returns the token symbol if valid, else None. Results are cached per run.
    gecko_get() already sleeps ~2s between calls (free-tier friendly).
    """
    if mint in _validation_cache:
        return _validation_cache[mint]

    symbol: str | None = None
    data = gecko_get(f"/networks/{NETWORK}/tokens/{mint}")
    if data and data.get("data"):
        attrs = data["data"].get("attributes", {})
        symbol = attrs.get("symbol") or attrs.get("name") or mint[:6]

    _validation_cache[mint] = symbol
    return symbol


# ---------------------------------------------------------------------------
# Detected-calls CSV (dedupe store across runs)
# ---------------------------------------------------------------------------

DETECTED_FIELDS = ["channel", "msg_id", "mint", "symbol", "call_time_utc", "msg_text"]


def load_detected() -> dict[tuple[str, str], dict]:
    """Load existing detections keyed by (channel, mint)."""
    store: dict[tuple[str, str], dict] = {}
    if not os.path.exists(DETECTED_CSV):
        return store
    with open(DETECTED_CSV, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["channel"], row["mint"])
            store[key] = row
    return store


def save_detected(store: dict[tuple[str, str], dict]) -> None:
    """Write the dedup store back to disk, sorted by channel then time."""
    rows = sorted(store.values(), key=lambda r: (r["channel"], r["call_time_utc"]))
    with open(DETECTED_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DETECTED_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {DETECTED_CSV} ({len(rows)} detected call(s))")


def merge_detection(store: dict[tuple[str, str], dict], channel: str, msg_id,
                    mint: str, symbol: str, call_time: datetime, text: str) -> bool:
    """
    Add/update a detection. One call per (channel, mint): keep the EARLIEST
    message. Returns True if this was new or replaced an older record.
    """
    key = (channel, mint)
    call_iso = call_time.astimezone(timezone.utc).isoformat()
    existing = store.get(key)
    if existing and existing["call_time_utc"] <= call_iso:
        return False  # already have an earlier (or equal) sighting
    store[key] = {
        "channel": channel,
        "msg_id": str(msg_id),
        "mint": mint,
        "symbol": symbol,
        "call_time_utc": call_iso,
        "msg_text": (text or "").replace("\n", " ").strip()[:500],
    }
    return True


# ---------------------------------------------------------------------------
# Telethon helpers
# ---------------------------------------------------------------------------

def normalize_channel(name: str) -> str:
    """Accept @username, plain username, or t.me/... links."""
    name = name.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):]
            break
    return name.strip("/")


def scan_message(text: str, store, channel, msg_id, call_time) -> int:
    """Detect + validate mints in one message; merge hits. Returns # new calls.
    Pure/sync (validation is a blocking HTTP call) so both the Telethon and web
    ingestion paths can share it."""
    new = 0
    for mint in extract_mints(text):
        symbol = validate_mint(mint)
        if not symbol:
            continue  # false positive — didn't resolve on GeckoTerminal
        if merge_detection(store, channel, msg_id, mint, symbol, call_time, text):
            new += 1
            print(f"  + [{channel}] {symbol} ({mint}) @ "
                  f"{call_time.astimezone(timezone.utc).isoformat()} (msg {msg_id})")
    return new


async def backfill_channel(client: TelegramClient, channel: str, store) -> int:
    """Scan the most recent BACKFILL_LIMIT messages of one channel."""
    print(f"\n--- scanning @{channel} (last {BACKFILL_LIMIT} msgs) ---")
    # Collect first, then process oldest-first so 'earliest sighting' wins naturally.
    messages = []
    try:
        async for msg in client.iter_messages(channel, limit=BACKFILL_LIMIT):
            messages.append(msg)
    except FloodWaitError as e:
        print(f"  ! FloodWait: sleeping {e.seconds}s")
        await asyncio.sleep(e.seconds)
        async for msg in client.iter_messages(channel, limit=BACKFILL_LIMIT):
            messages.append(msg)
    except Exception as exc:
        print(f"  ! ERROR reading @{channel}: {exc}")
        return 0

    messages.sort(key=lambda m: m.date)  # oldest first; m.date is tz-aware UTC
    new = 0
    for msg in messages:
        # msg.message is the CURRENT (edited) text; msg.date is the ORIGINAL post date.
        text = msg.message or getattr(msg, "raw_text", "") or ""
        if not text:
            continue
        new += scan_message(text, store, channel, msg.id,
                            msg.date.astimezone(timezone.utc))
    print(f"  {new} new call(s) from @{channel}")
    return new


# ---------------------------------------------------------------------------
# Web-preview ingestion (t.me/s/{channel}) — no API key, PUBLIC channels only.
# Backfill only (the web preview has no realtime stream).
# ---------------------------------------------------------------------------

def _parse_web_page(html_text: str) -> list[tuple[int, datetime, str]]:
    """
    Parse one t.me/s/ page into [(msg_id, datetime_utc, text)] sorted ascending.
    The CA may live in the visible text OR inside an <a href> (pump.fun/dexscreener
    links), so detection text = visible text + every href in the message block.
    """
    from bs4 import BeautifulSoup  # local import: web path is optional for telethon users

    soup = BeautifulSoup(html_text, "html.parser")
    out: list[tuple[int, datetime, str]] = []
    for block in soup.select("div.tgme_widget_message"):
        post = block.get("data-post", "")          # "channel/12345"
        try:
            msg_id = int(post.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            continue
        # datetime lives in the <time datetime="..."> inside the post-date link.
        tnode = (block.select_one("a.tgme_widget_message_date time[datetime]")
                 or block.select_one("time[datetime]"))
        if not tnode or not tnode.get("datetime"):
            continue
        try:
            dt = datetime.fromisoformat(tnode["datetime"].replace("Z", "+00:00"))
        except ValueError:
            continue
        dt = dt.astimezone(timezone.utc)

        parts: list[str] = []
        txt_node = block.select_one("div.tgme_widget_message_text")
        if txt_node:
            parts.append(txt_node.get_text(" ", strip=True))
        # hrefs (mint may be in a link, not the visible text)
        for a in block.select("a[href]"):
            parts.append(html_lib.unescape(a.get("href", "")))
        out.append((msg_id, dt, " ".join(p for p in parts if p)))

    out.sort(key=lambda x: x[0])
    return out


def backfill_channel_web(channel: str, store) -> int:
    """Scan the most recent BACKFILL_LIMIT messages of a PUBLIC channel via t.me/s/."""
    print(f"\n--- scanning t.me/s/{channel} (web preview, up to {BACKFILL_LIMIT} msgs) ---")
    session = requests.Session()
    session.headers.update({"User-Agent": WEB_USER_AGENT})

    collected: dict[int, tuple[datetime, str]] = {}   # msg_id -> (dt, text)
    before: int | None = None

    while len(collected) < BACKFILL_LIMIT:
        params = {"before": before} if before is not None else None
        try:
            r = session.get(f"https://t.me/s/{channel}", params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  ! ERROR fetching t.me/s/{channel}: {exc}")
            break
        if r.status_code != 200:
            print(f"  ! HTTP {r.status_code} for t.me/s/{channel}")
            break

        page = _parse_web_page(r.text)
        if not page:
            if before is None:
                print("  ! no messages — channel may be private or have no web preview.")
            break

        page_ids = [m[0] for m in page]
        page_min = min(page_ids)
        added = 0
        for msg_id, dt, text in page:
            if msg_id not in collected:
                collected[msg_id] = (dt, text)
                added += 1
        print(f"  fetched {len(page)} msgs (ids {page_min}..{max(page_ids)}); "
              f"total {len(collected)}")

        if before is not None and page_min >= before:
            break          # no backward progress
        if added == 0:
            break          # nothing new — reached the start of history
        before = page_min  # next page: messages older than this id
        time.sleep(WEB_PAGE_SLEEP)

    # Process oldest-first so the earliest sighting of a mint wins (dedupe rule).
    new = 0
    for msg_id in sorted(collected):
        dt, text = collected[msg_id]
        if text:
            new += scan_message(text, store, channel, msg_id, dt)
    print(f"  {new} new call(s) from t.me/s/{channel}")
    return new


# ---------------------------------------------------------------------------
# Per-channel summary (built from pull_calls Result objects)
# ---------------------------------------------------------------------------

def print_channel_summary(results: list) -> None:
    by_channel: dict[str, list] = {}
    for r in results:
        if r.peak_gain_pct is None:
            continue
        by_channel.setdefault(r.channel or "(unknown)", []).append(r)

    if not by_channel:
        print("\nNo valid results for per-channel summary.")
        return

    print("\n" + "=" * 78)
    print("PER-CHANNEL SUMMARY")
    print("=" * 78)
    header = (f"{'CHANNEL':<18} {'N':>3} {'>=2x':>6} {'>=10x':>6} {'>=50x':>6} "
              f"{'MED PEAK':>10} {'MED DD':>9}")
    print(header)
    print("-" * len(header))
    for channel, rows in sorted(by_channel.items()):
        gains = [r.peak_gain_pct for r in rows]
        mults = [g / 100 + 1 for g in gains]
        dds = [r.drawdown_pct for r in rows if r.drawdown_pct is not None]
        n = len(rows)
        p2 = sum(1 for m in mults if m >= 2) / n * 100
        p10 = sum(1 for m in mults if m >= 10) / n * 100
        p50 = sum(1 for m in mults if m >= 50) / n * 100
        med_peak = statistics.median(gains)
        med_dd = statistics.median(dds) if dds else float("nan")
        print(f"{channel:<18} {n:>3} {p2:>5.0f}% {p10:>5.0f}% {p50:>5.0f}% "
              f"{med_peak:>+9.1f}% {med_dd:>+8.1f}%")
    print("-" * len(header))


# ---------------------------------------------------------------------------
# Analysis bridge
# ---------------------------------------------------------------------------

def analyze_detected(store: dict[tuple[str, str], dict]) -> None:
    """
    Incrementally analyze detected calls and write calls_analysis.csv.

    To stay fast and within GeckoTerminal's rate limits on a daily schedule, we
    only (re)analyze calls that are NEW (no cached result yet) or RECENT (call
    time within REFRESH_WINDOW_DAYS) — those are still moving. Calls older than
    the window keep their previously-computed results from calls_analysis.csv.
    """
    if not store:
        print("\nNo detected calls to analyze.")
        write_last_updated()
        return

    # Load previously-computed results (keyed by channel+mint) as the cache.
    prior = {(r.channel, r.mint): r for r in pull_calls.load_results_csv()}
    now = datetime.now(timezone.utc)

    to_analyze: list[dict] = []
    kept: list = []
    for row in sorted(store.values(), key=lambda r: r["call_time_utc"]):
        key = (row["channel"], row["mint"])
        call_dt = datetime.fromisoformat(row["call_time_utc"].replace("Z", "+00:00"))
        age_days = (now - call_dt).total_seconds() / 86400.0
        is_new = key not in prior
        is_recent = age_days <= REFRESH_WINDOW_DAYS
        if is_new or is_recent:
            to_analyze.append({
                "symbol": row.get("symbol") or row["mint"][:6],
                "mint": row["mint"],
                "call_iso": row["call_time_utc"],
                "channel": row["channel"],
                "msg_id": row["msg_id"],
            })
        else:
            kept.append(prior[key])

    new_count = sum(1 for c in to_analyze if (c["channel"], c["mint"]) not in prior)
    print(f"\nIncremental analysis: {len(to_analyze)} call(s) to (re)analyze "
          f"({new_count} new + {len(to_analyze) - new_count} within "
          f"{REFRESH_WINDOW_DAYS}d), {len(kept)} kept from cache.")

    new_results = pull_calls.analyze_calls(to_analyze) if to_analyze else []

    # Merge cached + freshly-analyzed (fresh wins on key), then persist.
    merged: dict[tuple[str, str], object] = {}
    for r in kept:
        merged[(r.channel, r.mint)] = r
    for r in new_results:
        merged[(r.channel, r.mint)] = r
    results = list(merged.values())

    pull_calls.write_csv(results)
    pull_calls.write_raw_ohlcv(new_results)  # only re-pulled OHLCV; cached files untouched
    print_channel_summary(results)
    write_last_updated()


def write_last_updated() -> None:
    """Stamp the data-refresh time so the dashboard can show 'last updated'."""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with open(LAST_UPDATED_FILE, "w", encoding="utf-8") as f:
            f.write(ts + "\n")
        print(f"Stamped {LAST_UPDATED_FILE}: {ts}")
    except OSError as exc:
        print(f"  ! could not write {LAST_UPDATED_FILE}: {exc}")


# ---------------------------------------------------------------------------
# Live listening
# ---------------------------------------------------------------------------

def register_live_handlers(client: TelegramClient, channels: list[str], store) -> None:
    @client.on(events.NewMessage(chats=channels))
    async def on_new(event):
        msg = event.message
        text = msg.message or ""
        if scan_message(text, store, _chat_label(event), msg.id,
                        msg.date.astimezone(timezone.utc)):
            save_detected(store)

    @client.on(events.MessageEdited(chats=channels))
    async def on_edit(event):
        # Edited message: keep ORIGINAL post date (msg.date), read edited text.
        msg = event.message
        text = msg.message or ""
        if scan_message(text, store, _chat_label(event), msg.id,
                        msg.date.astimezone(timezone.utc)):
            save_detected(store)


def _chat_label(event) -> str:
    chat = event.chat
    if chat is not None and getattr(chat, "username", None):
        return chat.username
    return str(event.chat_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_web(channels: list[str], store) -> int:
    """SOURCE='web': scrape public channel previews. No API key, backfill only."""
    if LIVE:
        print("  note: LIVE mode is not supported by the web preview — backfill only.")
    total_new = 0
    for i, channel in enumerate(channels):
        total_new += backfill_channel_web(channel, store)
        if i < len(channels) - 1:
            time.sleep(CHANNEL_SLEEP)
    print(f"\nBackfill complete: {total_new} new call(s) detected.")
    save_detected(store)
    analyze_detected(store)
    return 0


async def run_telethon(channels: list[str], store) -> int:
    """SOURCE='telethon': user session. Private channels + optional live mode."""
    if not TELETHON_AVAILABLE:
        print("ERROR: telethon not installed. `pip install telethon` or set SOURCE='web'.")
        return 1
    if not API_ID or not API_HASH:
        print("ERROR: API_ID / API_HASH not set. Create a .env file (see .env.example),")
        print("       or set SOURCE='web' to scrape public channels with no API key.")
        return 1

    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    await client.start()  # prompts for phone/code on first run (interactive)

    total_new = 0
    for i, channel in enumerate(channels):
        total_new += await backfill_channel(client, channel, store)
        if i < len(channels) - 1:
            await asyncio.sleep(CHANNEL_SLEEP)  # be gentle between channels

    print(f"\nBackfill complete: {total_new} new call(s) detected.")
    save_detected(store)
    analyze_detected(store)

    if LIVE:
        print("\nLIVE mode — listening for new/edited messages. Ctrl+C to stop.")
        register_live_handlers(client, channels, store)
        await client.run_until_disconnected()
    else:
        await client.disconnect()
    return 0


def main() -> int:
    channels = [normalize_channel(c) for c in CHANNELS]
    print(f"scan_telegram.py — source: {SOURCE}  channels: {channels}  "
          f"backfill: {BACKFILL_LIMIT}  live: {LIVE}")
    print(f"run time (UTC): {datetime.now(timezone.utc).isoformat()}")

    store = load_detected()
    print(f"Loaded {len(store)} previously-detected call(s) from {DETECTED_CSV}")

    try:
        if SOURCE == "web":
            return run_web(channels, store)
        elif SOURCE == "telethon":
            return asyncio.run(run_telethon(channels, store))
        else:
            print(f"ERROR: unknown SOURCE={SOURCE!r}; use 'web' or 'telethon'.")
            return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
