# pump.fun Call Analyzer

Grades Solana **pump.fun** memecoin "calls" posted in Telegram channels: how far did each
call run, how fast did it peak, and how much did it give back — measured from the **entry**
(market cap at the moment the call was posted, the real follower entry), not the launch floor.

A daily GitHub Action refreshes the data with **no API keys**, and a Streamlit "terminal"
dashboard visualizes it.

## Pipeline

| Stage | File | What it does |
|-------|------|--------------|
| Ingest | `scan_telegram.py` | Reads channel messages, detects Solana mints (base58 + pump.fun/dexscreener/solscan/birdeye URLs), validates them on GeckoTerminal, dedupes → `calls_detected.csv`. Two sources: `web` (public `t.me/s/` preview, **no key**) or `telethon` (user session, private channels + live mode). |
| Analyze | `pull_calls.py` | For each call, merges OHLCV across **all** of a token's pools (bonding-curve → migrated Raydium/PumpSwap), derives market cap (fixed 1e9 supply, fdv sanity-checked), and computes entry / peak / now / drawdown / minutes-to-peak → `calls_analysis.csv` + raw `ohlcv/{symbol}.csv`. |
| Visualize | `dashboard.py` | Streamlit dashboard: KPI cards, peak-multiple distribution, peak-over-time scatter, exit-timing (hold-N-hours vs hold-to-now) analysis, the full ledger with links, and a per-call price inspector with entry/peak markers. |

Daily runs only re-analyze **new** calls + calls from the **last 7 days** (cached results
for older calls), so they stay fast and within GeckoTerminal's free rate limit.

## Quick start (local)

```bash
pip install -r requirements.txt
python scan_telegram.py        # SOURCE="web" by default — no API key needed
streamlit run dashboard.py     # opens the dashboard at http://localhost:8501
```

Edit the `CONFIG` block at the top of `scan_telegram.py` to set `CHANNELS`,
`BACKFILL_LIMIT`, and `SOURCE`.

## Automated daily scan (GitHub Actions)

`.github/workflows/daily-scan.yml` runs the web scan once a day (06:00 UTC, also
manually triggerable) and commits refreshed `calls_detected.csv` / `calls_analysis.csv`
/ `last_updated.txt` back to the repo using the built-in `GITHUB_TOKEN` — no secrets to
configure. Change the schedule by editing the `cron:` line (instructions are inline).

## Deploy the dashboard (Streamlit Community Cloud — free)

1. Push this repo to GitHub (public).
2. Go to **https://share.streamlit.io** → sign in with GitHub → **New app**.
3. Pick this repo, branch `main`, main file `dashboard.py` → **Deploy**.

The deployed dashboard reads the CSVs the daily Action commits, so it stays fresh on its
own. Note: the raw `ohlcv/` files are git-ignored to keep the repo lean, so the **Exit
Timing** and **Call Inspector** panels show full price history only when run locally
(they degrade gracefully on the cloud). To enable them on the cloud too, remove `ohlcv/`
from `.gitignore` and commit it.

## Telethon source (optional — private channels / live mode)

Set `SOURCE = "telethon"` in `scan_telegram.py`, then create a `.env` (see
`.env.example`) with `API_ID` / `API_HASH` from https://my.telegram.org. The logged-in
account must already be a member of the target channels.

---
Data source: [GeckoTerminal](https://www.geckoterminal.com) public API. Not financial advice.
