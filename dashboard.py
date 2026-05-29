#!/usr/bin/env python3
"""
dashboard.py — "Quant Terminal" for the pump.fun call analyzer.

Reads the CSVs the pipeline already produces (calls_analysis.csv + calls_detected.csv
+ raw ohlcv/*.csv); it does NOT re-scrape or hit any API. Run with:

    streamlit run dashboard.py

Design: an institutional data-terminal aesthetic — warm-ink palette, hairline-ruled
panels, IBM Plex Mono tabular numerals, a single brass accent. All times UTC.
"""

from __future__ import annotations

import math
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ANALYSIS_CSV = "calls_analysis.csv"
DETECTED_CSV = "calls_detected.csv"
LAST_UPDATED_FILE = "last_updated.txt"
OHLCV_DIR = "ohlcv"

NUMERIC_COLS = [
    "supply", "entry_mc", "peak_mc", "now_mc",
    "peak_gain_pct", "now_gain_pct", "drawdown_pct", "minutes_to_peak",
]

# ---------------------------------------------------------------------------
# Palette (the "Quant Terminal" system)
# ---------------------------------------------------------------------------
BG       = "#0B0C0E"   # warm near-black
PANEL    = "#14161A"   # surface
PANEL2   = "#1B1E24"   # raised surface
HAIR     = "#262A31"   # hairline rule
GRID     = "#1E2127"   # chart gridlines
INK      = "#E6E3DB"   # primary text (warm off-white)
MUTE     = "#8A8F99"   # muted text
DIM      = "#5A6069"   # dim text
BRASS    = "#E0A436"   # signature accent
POS      = "#5BB98C"   # gains (muted green)
NEG      = "#D2726B"   # losses (muted brick)

MONO = "'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, monospace"
SANS = "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif"

HOURS_GRID = [0.5, 1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 48, 72]


# ---------------------------------------------------------------------------
# Global CSS — the look lives here.
# ---------------------------------------------------------------------------
def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

        .stApp {{ background:
            radial-gradient(1200px 600px at 80% -10%, #16181d 0%, {BG} 55%) fixed; }}
        html, body, [class*="css"], .stMarkdown, p, span, label, div {{
            font-family: {SANS}; color: {INK};
        }}
        .block-container {{ padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1320px; }}
        #MainMenu, footer, header [data-testid="stToolbar"] {{ visibility: hidden; }}

        /* ---- Masthead ---- */
        .mast {{ border-bottom: 1px solid {HAIR}; padding-bottom: 14px; margin-bottom: 4px; }}
        .mast-row {{ display:flex; align-items:baseline; justify-content:space-between; gap:16px; flex-wrap:wrap; }}
        .mast-title {{ font-family:{MONO}; font-weight:600; font-size:25px; letter-spacing:.02em;
            color:{INK}; margin:0; }}
        .mast-title .tick {{ color:{BRASS}; }}
        .mast-sub {{ color:{MUTE}; font-size:13px; margin-top:6px; max-width:760px; line-height:1.5; }}
        .mast-meta {{ font-family:{MONO}; font-size:11px; color:{DIM}; text-align:right;
            text-transform:uppercase; letter-spacing:.12em; line-height:1.9; white-space:nowrap; }}
        .mast-meta b {{ color:{MUTE}; font-weight:500; }}

        /* ---- Section header ---- */
        .sec {{ display:flex; align-items:center; gap:14px; margin:30px 0 14px; }}
        .sec-k {{ font-family:{MONO}; font-size:11px; letter-spacing:.22em; text-transform:uppercase;
            color:{BRASS}; white-space:nowrap; }}
        .sec-rule {{ flex:1; height:1px; background:{HAIR}; }}
        .sec-d {{ font-size:11px; color:{DIM}; white-space:nowrap; }}

        /* ---- KPI grid ---- */
        .kgrid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(168px,1fr));
            gap:1px; background:{HAIR}; border:1px solid {HAIR}; border-radius:3px; overflow:hidden; }}
        .kpi {{ background:{PANEL}; padding:16px 18px 15px; }}
        .kpi-l {{ font-family:{MONO}; font-size:10px; letter-spacing:.14em; text-transform:uppercase;
            color:{MUTE}; margin-bottom:9px; }}
        .kpi-v {{ font-family:{MONO}; font-weight:600; font-size:27px; line-height:1;
            font-variant-numeric:tabular-nums; color:{INK}; }}
        .kpi-v.pos {{ color:{POS}; }} .kpi-v.neg {{ color:{NEG}; }} .kpi-v.acc {{ color:{BRASS}; }}
        .kpi-s {{ font-family:{MONO}; font-size:10.5px; color:{DIM}; margin-top:8px;
            font-variant-numeric:tabular-nums; }}

        /* ---- Sidebar ---- */
        section[data-testid="stSidebar"] {{ background:{PANEL}; border-right:1px solid {HAIR}; }}
        section[data-testid="stSidebar"] .stMarkdown p {{ color:{MUTE}; }}

        /* ---- Tables ---- */
        [data-testid="stDataFrame"] {{ font-family:{MONO}; }}
        [data-testid="stDataFrame"] * {{ font-variant-numeric:tabular-nums; }}

        /* ---- Buttons ---- */
        .stButton > button {{ font-family:{MONO}; font-size:12px; letter-spacing:.06em;
            text-transform:uppercase; border:1px solid {HAIR}; background:{PANEL2}; color:{INK};
            border-radius:3px; }}
        .stButton > button:hover {{ border-color:{BRASS}; color:{BRASS}; }}

        /* metrics fallback (unused — we render custom KPIs) */
        [data-testid="stMetricValue"] {{ font-family:{MONO}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def section(key: str, desc: str = "") -> None:
    d = f'<span class="sec-d">{desc}</span>' if desc else ""
    st.markdown(
        f'<div class="sec"><span class="sec-k">{key}</span>'
        f'<span class="sec-rule"></span>{d}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_mc(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.2f}M"
    if a >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def fmt_mult(gain_pct) -> str:
    if gain_pct is None or (isinstance(gain_pct, float) and math.isnan(gain_pct)):
        return "—"
    return f"{float(gain_pct)/100 + 1:.1f}x"


def fmt_pct(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return f"{float(value):+.1f}%"


def mult_bucket(gain_pct) -> str:
    if gain_pct is None or (isinstance(gain_pct, float) and math.isnan(gain_pct)):
        return "<2x"
    m = float(gain_pct)/100 + 1
    if m < 2:
        return "<2x"
    if m < 10:
        return "2-10x"
    if m < 50:
        return "10-50x"
    return "50x+"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_data(_bust: int = 0):
    """Returns (analyzed_df | None, full_df, failed_count)."""
    if not os.path.exists(ANALYSIS_CSV):
        return None, pd.DataFrame(), 0
    try:
        df = pd.read_csv(ANALYSIS_CSV)
    except (pd.errors.EmptyDataError, OSError):
        return None, pd.DataFrame(), 0
    if df.empty:
        return None, pd.DataFrame(), 0

    for col in ["symbol", "mint", "channel", "msg_id", "call_time_utc",
                "peak_ts_utc", "error"] + NUMERIC_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["call_time_utc"] = pd.to_datetime(df["call_time_utc"], utc=True, errors="coerce")
    df["peak_ts_utc"] = pd.to_datetime(df["peak_ts_utc"], utc=True, errors="coerce")

    if os.path.exists(DETECTED_CSV):
        try:
            det = pd.read_csv(DETECTED_CSV)
            if not det.empty and {"channel", "mint"}.issubset(det.columns):
                keep = [c for c in ["channel", "mint", "msg_text"] if c in det.columns]
                det = det[keep].drop_duplicates(subset=["channel", "mint"])
                df = df.merge(det, on=["channel", "mint"], how="left")
        except (pd.errors.EmptyDataError, OSError):
            pass
    if "msg_text" not in df.columns:
        df["msg_text"] = pd.NA

    err = df["error"].fillna("").astype(str).str.strip()
    failed = int((err != "").sum())
    analyzed = df[(err == "") & df["peak_gain_pct"].notna()].copy()
    return analyzed, df, failed


def get_last_updated():
    if os.path.exists(LAST_UPDATED_FILE):
        try:
            with open(LAST_UPDATED_FILE, encoding="utf-8") as f:
                ts = pd.to_datetime(f.read().strip(), utc=True)
            if pd.notna(ts):
                return ts
        except (ValueError, OSError):
            pass
    if os.path.exists(ANALYSIS_CSV):
        try:
            return pd.to_datetime(os.path.getmtime(ANALYSIS_CSV), unit="s", utc=True)
        except (ValueError, OSError):
            return None
    return None


def build_links(row: pd.Series):
    channel, msg_id, mint = row.get("channel"), row.get("msg_id"), row.get("mint")
    tg = ""
    if pd.notna(channel) and pd.notna(msg_id) and str(channel).strip():
        mid = str(msg_id).strip()
        if mid.endswith(".0"):
            mid = mid[:-2]
        tg = f"https://t.me/{str(channel).strip()}/{mid}"
    coin = f"https://pump.fun/coin/{str(mint).strip()}" if pd.notna(mint) else ""
    return tg, coin


def ohlcv_path(symbol, mint) -> str | None:
    """Mirror pull_calls.write_raw_ohlcv filename sanitisation, with mint fallback."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(symbol)) or str(mint)[:8]
    for cand in (safe, str(mint)[:8]):
        p = os.path.join(OHLCV_DIR, f"{cand}.csv")
        if os.path.exists(p):
            return p
    return None


@st.cache_data(show_spinner=False)
def load_ohlcv(symbol, mint, _bust: int = 0):
    p = ohlcv_path(symbol, mint)
    if not p:
        return None
    try:
        df = pd.read_csv(p)
    except (pd.errors.EmptyDataError, OSError):
        return None
    if df.empty or "ts_unix" not in df.columns:
        return None
    df["ts_unix"] = pd.to_numeric(df["ts_unix"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["ts_unix", "close"]).sort_values("ts_unix")


# ---------------------------------------------------------------------------
# Plotly terminal styling
# ---------------------------------------------------------------------------
def terminal_layout(fig: go.Figure, height: int = 300) -> go.Figure:
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=MONO, size=11, color=MUTE),
        margin=dict(l=10, r=10, t=10, b=10),
        hoverlabel=dict(font=dict(family=MONO, size=11), bgcolor=PANEL2, bordercolor=HAIR),
        legend=dict(font=dict(size=10, color=MUTE), bgcolor="rgba(0,0,0,0)"),
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=HAIR, linecolor=HAIR,
                     tickfont=dict(color=DIM), title_font=dict(color=MUTE, size=11))
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=HAIR, linecolor=HAIR,
                     tickfont=dict(color=DIM), title_font=dict(color=MUTE, size=11))
    return fig


# ===========================================================================
# APP
# ===========================================================================
st.set_page_config(page_title="Call Analyzer Terminal", layout="wide", page_icon="◆")
inject_css()

if "refresh" not in st.session_state:
    st.session_state.refresh = 0

analyzed, full_df, failed = load_data(st.session_state.refresh)
updated = get_last_updated()
n_detected = len(full_df) if full_df is not None else 0

# ---- Masthead ----
upd_str = updated.strftime("%Y-%m-%d %H:%M") if updated is not None else "—"
st.markdown(
    f"""
    <div class="mast"><div class="mast-row">
      <div>
        <h1 class="mast-title">CALL ANALYZER <span class="tick">▌</span> TERMINAL</h1>
        <div class="mast-sub">Grading Solana pump.fun memecoin calls. <b style="color:{INK}">Entry</b>
        is the market cap at the moment the call was posted — the real follower entry, not the
        launch floor. Peak / drawdown measured from that entry. All times UTC.</div>
      </div>
      <div class="mast-meta">
        <b>Updated</b> {upd_str} UTC<br>
        <b>Detected</b> {n_detected} calls<br>
        <b>Source</b> calls_analysis.csv
      </div>
    </div></div>
    """,
    unsafe_allow_html=True,
)

if analyzed is None:
    st.warning(
        f"**No analyzed calls found.** `{ANALYSIS_CSV}` is missing or empty. "
        "Run `python scan_telegram.py` first, then reload."
    )
    st.stop()
if analyzed.empty:
    st.info(f"`{ANALYSIS_CSV}` has rows but none analyzed ({failed} failed). Nothing to chart yet.")
    st.stop()

# ---- Sidebar filters ----
with st.sidebar:
    st.markdown(f"<div class='sec-k' style='margin-bottom:14px'>FILTERS</div>", unsafe_allow_html=True)
    channels = sorted([c for c in analyzed["channel"].dropna().unique() if str(c).strip()])
    chosen = st.multiselect("Channel", channels, default=channels)
    view = analyzed[analyzed["channel"].isin(chosen)].copy() if chosen else analyzed.copy()

    vd = view["call_time_utc"].dropna()
    if not vd.empty:
        lo, hi = vd.min().date(), vd.max().date()
        dr = st.date_input("Call date range (UTC)", value=(lo, hi), min_value=lo, max_value=hi)
        if isinstance(dr, (tuple, list)) and len(dr) == 2:
            s, e = dr
            m = (view["call_time_utc"].dt.date >= s) & (view["call_time_utc"].dt.date <= e)
            view = view[m | view["call_time_utc"].isna()].copy()

    st.markdown(f"<div style='font-family:{MONO};font-size:12px;color:{BRASS};margin-top:8px'>"
                f"{len(view)} calls in view</div>", unsafe_allow_html=True)
    if failed:
        st.caption(f"⚠ {failed} row(s) failed to analyze (excluded).")
    st.divider()
    if st.button("↻ Refresh data"):
        st.session_state.refresh += 1
        load_data.clear()
        load_ohlcv.clear()
        st.rerun()

if view.empty:
    st.info("No calls match the current filters.")
    st.stop()

# ---- KPI grid ----
n = len(view)
peak, now, dd = view["peak_gain_pct"], view["now_gain_pct"], view["drawdown_pct"]
pct_2x = (peak >= 100).mean()*100
pct_10x = (peak >= 900).mean()*100
med_mult = peak.median()/100 + 1 if peak.notna().any() else float("nan")
med_dd = dd.median() if dd.notna().any() else float("nan")
pct_below = (now < 0).mean()*100 if now.notna().any() else float("nan")
mean_now = now.mean() if now.notna().any() else float("nan")


def kpi(label, value, sub="", tone=""):
    return (f'<div class="kpi"><div class="kpi-l">{label}</div>'
            f'<div class="kpi-v {tone}">{value}</div>'
            f'<div class="kpi-s">{sub}</div></div>')


cards = "".join([
    kpi("Calls analyzed", f"{n}", f"of {n_detected} detected"),
    kpi("Hit ≥ 2x", f"{pct_2x:.0f}%", f"{int((peak>=100).sum())} of {n} calls",
        "pos" if pct_2x >= 50 else ""),
    kpi("Hit ≥ 10x", f"{pct_10x:.0f}%", f"{int((peak>=900).sum())} of {n} calls",
        "acc" if pct_10x > 0 else ""),
    kpi("Median peak", f"{med_mult:.1f}x" if not math.isnan(med_mult) else "—", "from entry",
        "pos" if med_mult >= 2 else ""),
    kpi("Median drawdown", fmt_pct(med_dd), "from peak → now", "neg"),
    kpi("Now below entry", f"{pct_below:.0f}%" if not math.isnan(pct_below) else "—",
        "underwater vs call", "neg" if pct_below >= 50 else ""),
    kpi("Hold-all to now", fmt_pct(mean_now), "mean, equal-weight",
        "pos" if (not math.isnan(mean_now) and mean_now >= 0) else "neg"),
])
if failed:
    cards += kpi("Failed", f"{failed}", "could not analyze")
st.markdown(f'<div class="kgrid">{cards}</div>', unsafe_allow_html=True)

# ---- Distribution + scatter ----
section("DISTRIBUTION", "how many calls actually ran")
c1, c2 = st.columns([1, 1.25])

with c1:
    order = ["<2x", "2-10x", "10-50x", "50x+"]
    cnt = view["peak_gain_pct"].apply(mult_bucket).value_counts().reindex(order, fill_value=0)
    colors = {"<2x": NEG, "2-10x": "#B98B4A", "10-50x": POS, "50x+": BRASS}
    fig = go.Figure(go.Bar(
        x=order, y=cnt.values, marker_color=[colors[b] for b in order],
        text=cnt.values, textposition="outside",
        textfont=dict(family=MONO, color=INK),
        hovertemplate="%{x}: %{y} calls<extra></extra>",
    ))
    fig.update_layout(yaxis_title="calls")
    st.plotly_chart(terminal_layout(fig, 300), width="stretch")
    duds = int((view["peak_gain_pct"] < 100).sum())
    st.caption(f"{duds} of {n} never doubled from entry — a dud (<2x).")

with c2:
    sc = view.copy()
    sc["mult"] = sc["peak_gain_pct"]/100 + 1
    sc = sc.dropna(subset=["call_time_utc", "mult"])
    fig = go.Figure()
    if not sc.empty:
        fig.add_trace(go.Scatter(
            x=sc["call_time_utc"], y=sc["mult"], mode="markers",
            marker=dict(size=9, color=BRASS, line=dict(width=1, color=BG), opacity=0.85),
            customdata=sc[["symbol", "channel"]],
            hovertemplate="<b>%{customdata[0]}</b> · %{customdata[1]}<br>"
                          "%{x|%Y-%m-%d %H:%M} UTC<br>peak %{y:.1f}x<extra></extra>",
        ))
        fig.add_hline(y=1, line_dash="dot", line_color=DIM,
                      annotation_text="entry 1x", annotation_font_color=DIM)
    fig.update_layout(xaxis_title="call time (UTC)", yaxis_title="peak multiple (x)")
    st.plotly_chart(terminal_layout(fig, 300), width="stretch")
    st.caption("Each point is one call. Hover for the ticker.")

# ---- Hold-strategy analysis ----
section("EXIT TIMING", "would taking profit after N hours beat holding to now?")

@st.cache_data(show_spinner=False)
def build_hold(symbols_mints, refresh):
    series = []
    for sym, mint, entry, supply, call_unix in symbols_mints:
        if not entry or entry <= 0 or not supply:
            continue
        df = load_ohlcv(sym, mint, refresh)
        if df is None:
            continue
        d = df[df["ts_unix"] >= call_unix]
        if d.empty:
            continue
        h = (d["ts_unix"] - call_unix) / 3600.0
        gain = (d["close"] * supply / entry - 1) * 100
        dd = pd.DataFrame({"h": h.values, "g": gain.values})
        row = {}
        for H in HOURS_GRID:
            sub = dd[dd["h"] <= H]
            if not sub.empty:
                row[H] = float(sub["g"].iloc[-1])
        if row:
            series.append(row)
    out = []
    for H in HOURS_GRID:
        vals = [s[H] for s in series if H in s]
        if vals:
            s = pd.Series(vals)
            out.append((H, s.median(), s.quantile(.25), s.quantile(.75), len(vals)))
    return pd.DataFrame(out, columns=["h", "med", "p25", "p75", "n"]), len(series)

keys = tuple((r.symbol, r.mint, float(r.entry_mc) if pd.notna(r.entry_mc) else 0.0,
              float(r.supply) if pd.notna(r.supply) else 0.0,
              r.call_time_utc.timestamp() if pd.notna(r.call_time_utc) else 0.0)
             for r in view.itertuples())
hold, n_series = build_hold(keys, st.session_state.refresh)

if hold.empty:
    st.info("Raw OHLCV not available for these calls (the `ohlcv/` files aren't present in "
            "this deployment). This panel works locally after running the scan.")
else:
    hc1, hc2 = st.columns([1.4, 1])
    with hc1:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=hold["h"], y=hold["p75"], mode="lines", line=dict(width=0),
            hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scatter(
            x=hold["h"], y=hold["p25"], mode="lines", line=dict(width=0),
            fill="tonexty", fillcolor="rgba(224,164,54,0.10)",
            hoverinfo="skip", name="25–75%"))
        fig.add_trace(go.Scatter(
            x=hold["h"], y=hold["med"], mode="lines+markers",
            line=dict(color=BRASS, width=2), marker=dict(size=5, color=BRASS),
            name="median", hovertemplate="hold %{x}h → median %{y:+.0f}%<extra></extra>"))
        if not math.isnan(mean_now):
            fig.add_hline(y=now.median(), line_dash="dot", line_color=POS,
                          annotation_text=f"hold-to-now median {now.median():+.0f}%",
                          annotation_font_color=POS)
        fig.add_hline(y=0, line_color=HAIR)
        fig.update_layout(xaxis_title="hours held after call", yaxis_title="gain from entry (%)")
        fig.update_xaxes(type="log", tickvals=HOURS_GRID, ticktext=[str(h) for h in HOURS_GRID])
        st.plotly_chart(terminal_layout(fig, 320), width="stretch")
    with hc2:
        best = hold.loc[hold["med"].idxmax()]
        nowmed = now.median()
        st.markdown(
            f"<div style='font-family:{MONO};font-size:12.5px;line-height:2.0;color:{MUTE}'>"
            f"<span style='color:{DIM}'>BASIS</span> {n_series} calls w/ price history<br>"
            f"<span style='color:{DIM}'>BEST FIXED EXIT</span> "
            f"<span style='color:{BRASS}'>+{best['med']:.0f}%</span> at "
            f"<span style='color:{INK}'>{best['h']:g}h</span><br>"
            f"<span style='color:{DIM}'>HOLD TO NOW</span> "
            f"<span style='color:{POS if nowmed>=0 else NEG}'>{nowmed:+.0f}%</span> median"
            f"</div>", unsafe_allow_html=True)
        delta = best["med"] - nowmed
        st.markdown(
            f"<div style='font-size:12.5px;color:{MUTE};margin-top:14px;line-height:1.6'>"
            f"Taking profit at <b style='color:{INK}'>{best['h']:g}h</b> would have beaten "
            f"holding to now by <b style='color:{BRASS}'>{delta:+.0f}pp</b> (median call). "
            f"Memecoin calls decay fast — the curve usually peaks early then bleeds.</div>",
            unsafe_allow_html=True)

# ---- Ledger ----
section("LEDGER", "every call, newest first")
tbl = view.sort_values("call_time_utc", ascending=False, na_position="last").copy()
lk = tbl.apply(build_links, axis=1, result_type="expand")
tbl["tg_url"], tbl["coin_url"] = lk[0], lk[1]

disp = pd.DataFrame({
    "Symbol": tbl["symbol"],
    "Channel": tbl["channel"],
    "Call (UTC)": tbl["call_time_utc"].dt.strftime("%Y-%m-%d %H:%M"),
    "Entry": tbl["entry_mc"].apply(fmt_mc),
    "Peak": tbl["peak_gain_pct"].apply(fmt_mult),
    "Peak %": tbl["peak_gain_pct"],
    "ATH": tbl["peak_mc"].apply(fmt_mc),
    "Now": tbl["now_mc"].apply(fmt_mc),
    "Now %": tbl["now_gain_pct"],
    "Drawdown %": tbl["drawdown_pct"],
    "→Peak (m)": tbl["minutes_to_peak"],
    "TG": tbl["tg_url"],
    "Coin": tbl["coin_url"],
})


def color_sign(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return f"color:{DIM};"
    return f"color:{POS};" if v >= 0 else f"color:{NEG};"


styler = disp.style.map(color_sign, subset=["Now %", "Drawdown %", "Peak %"]).format(
    {"Peak %": "{:+.0f}%", "Now %": "{:+.1f}%", "Drawdown %": "{:+.1f}%", "→Peak (m)": "{:.0f}"},
    na_rep="—",
)
st.dataframe(
    styler, width="stretch", hide_index=True,
    column_config={
        "TG": st.column_config.LinkColumn("TG", display_text="open ↗"),
        "Coin": st.column_config.LinkColumn("Coin", display_text="pump ↗"),
    },
)

# ---- Per-call detail ----
section("CALL INSPECTOR", "price path with entry & peak markers")
labels = {f"{r.symbol}  ·  {r.call_time_utc:%Y-%m-%d}  ·  {fmt_mult(r.peak_gain_pct)}":
          (r.symbol, r.mint) for r in tbl.itertuples()}
pick = st.selectbox("Select a call", list(labels.keys()))
sym, mint = labels[pick]
row = tbl[(tbl["symbol"] == sym) & (tbl["mint"] == mint)].iloc[0]
od = load_ohlcv(sym, mint, st.session_state.refresh)

di1, di2 = st.columns([3, 1])
with di2:
    now_col = POS if (pd.notna(row["now_gain_pct"]) and row["now_gain_pct"] >= 0) else NEG
    mtp = (f"{row['minutes_to_peak']:.0f}m" if pd.notna(row["minutes_to_peak"]) else "—")
    st.markdown(
        f"<div style='font-family:{MONO};font-size:12.5px;line-height:2.0'>"
        f"<span style='color:{DIM}'>SYMBOL</span> <b style='color:{INK}'>{sym}</b><br>"
        f"<span style='color:{DIM}'>ENTRY</span> {fmt_mc(row['entry_mc'])}<br>"
        f"<span style='color:{DIM}'>ATH</span> {fmt_mc(row['peak_mc'])} "
        f"<span style='color:{BRASS}'>{fmt_mult(row['peak_gain_pct'])}</span><br>"
        f"<span style='color:{DIM}'>NOW</span> {fmt_mc(row['now_mc'])} "
        f"<span style='color:{now_col}'>{fmt_pct(row['now_gain_pct'])}</span><br>"
        f"<span style='color:{DIM}'>DRAWDOWN</span> "
        f"<span style='color:{NEG}'>{fmt_pct(row['drawdown_pct'])}</span><br>"
        f"<span style='color:{DIM}'>→PEAK</span> {mtp}"
        f"</div>", unsafe_allow_html=True)
    txt = row.get("msg_text")
    if pd.notna(txt) and str(txt).strip():
        st.markdown(f"<div style='font-size:11.5px;color:{MUTE};margin-top:12px;"
                    f"border-top:1px solid {HAIR};padding-top:10px'>{str(txt)[:240]}</div>",
                    unsafe_allow_html=True)

with di1:
    if od is None or od.empty:
        st.info(f"No raw OHLCV file for {sym} (ohlcv/ not present here). Run the scan locally.")
    else:
        supply = float(row["supply"]) if pd.notna(row["supply"]) else 1e9
        t = pd.to_datetime(od["ts_unix"], unit="s", utc=True)
        mc = od["close"] * supply
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=t, y=mc, mode="lines", line=dict(color=BRASS, width=1.4),
            fill="tozeroy", fillcolor="rgba(224,164,54,0.06)",
            hovertemplate="%{x|%Y-%m-%d %H:%M} UTC<br>MC %{y:$,.0f}<extra></extra>"))
        ct = row["call_time_utc"]
        if pd.notna(ct):
            # add_shape/annotation instead of add_vline: the latter does internal
            # Timestamp arithmetic that breaks under pandas 3.0.
            fig.add_shape(type="line", x0=ct, x1=ct, y0=0, y1=1, yref="paper",
                          line=dict(color=INK, dash="dash", width=1))
            fig.add_annotation(x=ct, y=1, yref="paper", yanchor="bottom",
                               text="call", showarrow=False, font=dict(color=INK, size=11))
        if pd.notna(row["entry_mc"]):
            fig.add_hline(y=row["entry_mc"], line_dash="dot", line_color=MUTE,
                          annotation_text=f"entry {fmt_mc(row['entry_mc'])}",
                          annotation_font_color=MUTE)
        pt = row["peak_ts_utc"]
        if pd.notna(pt) and pd.notna(row["peak_mc"]):
            fig.add_trace(go.Scatter(
                x=[pt], y=[row["peak_mc"]], mode="markers+text",
                marker=dict(size=10, color=POS, symbol="diamond", line=dict(width=1, color=BG)),
                text=[f" ATH {fmt_mult(row['peak_gain_pct'])}"], textposition="top center",
                textfont=dict(family=MONO, color=POS, size=11),
                hovertemplate="ATH %{y:$,.0f}<extra></extra>", showlegend=False))
        fig.update_layout(yaxis_title="market cap (USD)", showlegend=False)
        st.plotly_chart(terminal_layout(fig, 360), width="stretch")

st.markdown(
    f"<div style='margin-top:30px;border-top:1px solid {HAIR};padding-top:12px;"
    f"font-family:{MONO};font-size:10.5px;color:{DIM};letter-spacing:.1em'>"
    f"CALL ANALYZER TERMINAL · data: calls_analysis.csv + ohlcv/ · refresh from the sidebar "
    f"after re-running the pipeline · all times UTC</div>", unsafe_allow_html=True)
