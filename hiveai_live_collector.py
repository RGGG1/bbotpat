#!/usr/bin/env python3
import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

# -----------------------------
# v1-style HMI helpers (live)
# -----------------------------
import math
from pathlib import Path

def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def load_v1_hmi_calibration(csv_path: str):
    """
    Loads v1 calibration bounds from the same history file v1 uses.
    Uses rolling 365-day 5%/95% quantiles for:
      - OI (oi_usd)
      - perp_frac = perp_volume / (perp_volume + spot_volume)
      - V_raw = RV_90 / RV_30 (from spot_close)
    """
    try:
        import pandas as pd
        import numpy as np
    except Exception:
        return None

    p = Path(csv_path)
    if not p.exists():
        return None

    df = pd.read_csv(p)
    if df is None or df.empty:
        return None

    required = ("spot_close", "spot_volume", "perp_volume", "oi_usd")
    for c in required:
        if c not in df.columns:
            return None

    df = df.sort_values("date").reset_index(drop=True).copy()
    eps = 1e-9

    # Volatility components like v1
    df["log_ret"] = np.log(df["spot_close"] / df["spot_close"].shift(1))
    df["RV_30"] = df["log_ret"].rolling(30).std() * np.sqrt(365)
    df["RV_90"] = df["log_ret"].rolling(90).std() * np.sqrt(365)
    V_raw = df["RV_90"] / (df["RV_30"] + eps)

    # Perp pressure like v1
    df["perp_frac"] = df["perp_volume"] / (df["perp_volume"] + df["spot_volume"] + eps)

    def rolling_bounds(series, window=365, lo_q=0.05, hi_q=0.95):
        low = series.rolling(window=window, min_periods=window).quantile(lo_q)
        high = series.rolling(window=window, min_periods=window).quantile(hi_q)
        # avoid divide-by-zero
        mask = (high - low).abs() < eps
        high[mask] = low[mask] + eps
        return low, high

    OI_low_s, OI_high_s = rolling_bounds(df["oi_usd"], window=365)
    PF_low_s, PF_high_s = rolling_bounds(df["perp_frac"], window=365)
    V_low_s,  V_high_s  = rolling_bounds(V_raw, window=365)

    valid = (~OI_low_s.isna()) & (~OI_high_s.isna()) & (~PF_low_s.isna()) & (~PF_high_s.isna()) & (~V_low_s.isna()) & (~V_high_s.isna())
    if not valid.any():
        return None

    i = int(valid[valid].index[-1])

    return {
        "oi_low": float(OI_low_s.iloc[i]),
        "oi_high": float(OI_high_s.iloc[i]),
        "pf_low": float(PF_low_s.iloc[i]),
        "pf_high": float(PF_high_s.iloc[i]),
        "v_low": float(V_low_s.iloc[i]),
        "v_high": float(V_high_s.iloc[i]),
        "v_raw_last": float(V_raw.iloc[i]),
    }

def compute_hmi_v1_style_live(oi_usd_now: float, spot_vol_usd_24h: float, perp_vol_usd_24h: float, calib: dict) -> float | None:
    """
    v1 weighting:
      HMI = 0.50*OI_score + 0.30*SP_score + 0.20*V_score
    where each score is 0..100 based on rolling 365d quantile bounds.
    """
    if calib is None:
        return None

    eps = 1e-9
    if oi_usd_now is None or spot_vol_usd_24h is None or perp_vol_usd_24h is None:
        return None

    denom = perp_vol_usd_24h + spot_vol_usd_24h + eps
    perp_frac = perp_vol_usd_24h / denom

    oi_score = 100.0 * _clip01((oi_usd_now - calib["oi_low"]) / ((calib["oi_high"] - calib["oi_low"]) + eps))
    sp_score = 100.0 * _clip01((perp_frac - calib["pf_low"]) / ((calib["pf_high"] - calib["pf_low"]) + eps))

    v_raw = calib["v_raw_last"]
    v_score = 100.0 * _clip01((v_raw - calib["v_low"]) / ((calib["v_high"] - calib["v_low"]) + eps))

    return (0.50 * oi_score) + (0.30 * sp_score) + (0.20 * v_score)




# -------------------------
# Paths (write into webroot)
# -------------------------
WEBROOT = Path("/var/www/bbotpat_live")
WEBROOT.mkdir(parents=True, exist_ok=True)

HMI_OUT = WEBROOT / "hmi_latest.json"
PRICES_OUT = WEBROOT / "prices_latest.json"
DOM_BANDS_OUT = WEBROOT / "dom_bands_latest.json"
HMI_CALIB = load_v1_hmi_calibration("/root/bbotpat/data/hmi_oi_history.csv")

SUPPLIES_PATHS = [
    Path("/root/bbotpat_live/supplies_latest.json"),
    Path("/root/bbotpat_live/docs/supplies_latest.json"),
    Path("/root/bbotpat/supplies_latest.json"),
    Path("/root/bbotpat/docs/supplies_latest.json"),
]

# Universe used on your site (match your V1 index token set)
TOKENS = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]
STABLE_HINTS = ("USD",)

BINANCE_SPOT_WS = "wss://stream.binance.com:9443/stream"
BINANCE_FUT_WS = "wss://fstream.binance.com/stream"

SYMBOLS = {t: f"{t.lower()}usdt" for t in TOKENS}  # "btcusdt" etc.

# For the “BTC vs Alts” box you described: use ETH+BNB+SOL
ALTS_FOR_TOP_DOM = ["ETH", "BNB", "SOL"]

# -------------------------
# Helpers
# -------------------------

def read_v1_hmi_json():
    candidates = [
        Path("/var/www/bbotpat/hmi_latest.json"),
        Path("/var/www/bbotpat_v2/hmi_latest.json"),
        Path("/root/bbotpat/docs/hmi_latest.json"),
        Path("/root/bbotpat/docs_v2/hmi_latest.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    return None


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def read_supplies():
    for p in SUPPLIES_PATHS:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                # supports either {"supplies": {...}} or direct dict
                supplies = js.get("supplies", js)
                return {k.upper(): float(v.get("circulating_supply", v)) for k, v in supplies.items()}
            except Exception:
                continue
    return {}

def fmt_mc(v: float) -> str:
    if not v or v <= 0:
        return "$0"
    if v >= 1e12:
        return f"${v/1e12:.1f}T"
    if v >= 1e9:
        return f"${round(v/1e9)}B"
    if v >= 1e6:
        return f"${round(v/1e6)}M"
    return f"${round(v):,}"

def band_label_from_hmi(hmi: float) -> str:
    # keep your V1 band labels
    if hmi < 10: return "Zombie Apocalypse"
    if hmi < 25: return "McDonald's Applications in high demand"
    if hmi < 45: return "NGMI"
    if hmi < 50: return "Leaning bearish"
    if hmi < 55: return "Cautiously bullish"
    if hmi < 75: return "It's digital gold"
    if hmi < 90: return "Frothy"
    return "It's the future of finance"

def clamp(x, a, b):
    return max(a, min(b, x))

# -------------------------
# Live state (updated by WS)
# -------------------------
spot_price = {t: None for t in TOKENS}
spot_change_24h = {t: None for t in TOKENS}
fut_price = {t: None for t in TOKENS}

# We compute HMI from BTC only (as your repo does)
# Using minute-like “fast HMI”: combine:
# - volatility proxy: abs 24h change
# - perps vs spot proxy: futures price premium (tiny) + activity
# - OI proxy: REST openInterest each minute
last_btc_open_interest = None

async def fetch_open_interest(session: aiohttp.ClientSession):
    # Futures open interest endpoint (weight 1)
    # doc: GET /fapi/v1/openInterest?symbol=BTCUSDT :contentReference[oaicite:3]{index=3}
    url = "https://fapi.binance.com/fapi/v1/openInterest"
    params = {"symbol": "BTCUSDT"}
    async with session.get(url, params=params, timeout=10) as r:
        r.raise_for_status()
        js = await r.json()
        # "openInterest" is in contracts; still usable as a relative series
        return float(js.get("openInterest"))

def compute_hmi_fast():
    """
    Fast HMI (minute-updating) that stays faithful to your “factors” concept,
    without rewriting your whole daily quantile pipeline.

    Inputs:
      - BTC 24h % change (spot)
      - BTC futures premium vs spot (tiny signal of perp pressure)
      - BTC open interest (relative level)
    Output:
      hmi in [0,100]
    """
    sp = spot_price.get("BTC")
    fp = fut_price.get("BTC")
    ch = spot_change_24h.get("BTC")
    oi = last_btc_open_interest

    if sp is None or fp is None or ch is None or oi is None:
        return None

    # Normalize components into roughly comparable ranges
    # 1) volatility proxy
    vol = clamp(abs(ch) / 10.0, 0.0, 1.0)          # abs(±10%) ~= 1.0
    # 2) perp pressure proxy (premium)
    prem = clamp((fp - sp) / sp * 50.0 + 0.5, 0.0, 1.0)  # scaled & centered
    # 3) OI proxy: log-scale squash
    oi_s = clamp((math.log(max(oi, 1.0)) - 6.0) / 4.0, 0.0, 1.0)

    # Map to fear/greed:
    # - higher vol tends to fear (invert)
    # - higher prem & OI tends to greed
    score = (0.45 * (1.0 - vol)) + (0.25 * prem) + (0.30 * oi_s)
    return clamp(score * 100.0, 0.0, 100.0)

def compute_market_caps(supplies):
    mcs = {}
    for t in TOKENS:
        p = spot_price.get(t)
        s = supplies.get(t)
        if p is None or s is None:
            mcs[t] = 0.0
        else:
            mcs[t] = float(p) * float(s)
    return mcs

def compute_btc_dom_vs_token(btc_mc, token_mc):
    if btc_mc <= 0 or token_mc <= 0:
        return None
    return 100.0 * btc_mc / (btc_mc + token_mc)

def read_hourly_ranges():
    candidates = [
        Path("/var/www/bbotpat/prices_latest.json"),
        Path("/var/www/bbotpat_v2/prices_latest.json"),
        Path("/root/bbotpat/docs/prices_latest.json"),
        Path("/root/bbotpat/docs_v2/prices_latest.json"),
    ]
    for p in candidates:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                rows = js.get("rows", [])
                out = {}
                for r in rows:
                    tok = (r.get("token") or "").upper()
                    rng = r.get("range")
                    if tok and rng:
                        out[tok] = rng
                if out:
                    return out
            except Exception:
                pass
    return {}


async def write_outputs():
    supplies = read_supplies()
    mcs = compute_market_caps(supplies)
    hourly_ranges = read_hourly_ranges()

    # prices_latest.json (match V1 frontend expectations)
    rows = []
    btc_mc = mcs.get("BTC", 0.0)
    for t in TOKENS:
        p = spot_price.get(t)
        mc = mcs.get(t, 0.0)
        ch = spot_change_24h.get(t)

        btc_dom = None
        if t != "BTC" and not any(x in t for x in STABLE_HINTS):
            btc_dom = compute_btc_dom_vs_token(btc_mc, mc)

        # Range/action/potROI are computed by your existing scripts today.
        # For live page we keep them “–” unless you want me to wire in your
        # exact historical range engine into this collector.
        rows.append({
            "token": t,
            "price": p,
            "mc": mc,
            "change_24h": ch,
            "btc_dom": btc_dom,
            "range": hourly_ranges.get(t, "–"),
        })


    PRICES_OUT.write_text(json.dumps({
        "timestamp": utc_now_iso(),
        "rows": rows,
    }, indent=2))

    # dom_bands_latest.json (BTC vs ETH+BNB+SOL)
    alt_mc = sum(mcs.get(t, 0.0) for t in ALTS_FOR_TOP_DOM)
    dom = 100.0 * btc_mc / (btc_mc + alt_mc) if (btc_mc + alt_mc) > 0 else None

    DOM_BANDS_OUT.write_text(json.dumps({
        "timestamp": utc_now_iso(),
        "btc_pct": round(dom, 1) if dom is not None else None,
        "alt_pct": round(100.0 - dom, 1) if dom is not None else None,
        "min_pct": None,
        "max_pct": None,
        "btc_mc_fmt": fmt_mc(btc_mc),
        "alt_mc_fmt": fmt_mc(alt_mc),
        "action": None,
    }, indent=2))

    # hmi_latest.json
    hmi = compute_hmi_v1_style_live(
        oi_usd_now=btc_oi_usd_now,
        spot_vol_usd_24h=btc_spot_quotevol_24h_usd,
        perp_vol_usd_24h=btc_perp_quotevol_24h_usd,
        calib=HMI_CALIB,
    )
    if hmi is not None:
        HMI_OUT.write_text(json.dumps({
            "hmi": round(hmi, 1),
            "band": band_label_from_hmi(hmi),
            "exported_at": utc_now_iso(),
        }, indent=2))


async def ws_consumer(url, streams, handler_name):
    """
    Connect a combined stream WS and update globals.
    """
    params = "/".join(streams)
    full = f"{url}?streams={params}"

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(full, heartbeat=30) as ws:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                stream = data.get("stream", "")
                payload = data.get("data", {})

                # Spot ticker 24h: <symbol>@ticker
                if handler_name == "spot":
                    if payload.get("e") == "24hrTicker":
                        sym = payload.get("s", "").lower()
                        last = float(payload.get("c"))
                        ch = float(payload.get("P"))  # percent
                        for t, s in SYMBOLS.items():
                            if s.upper() == sym.upper():
                                spot_price[t] = last
                                spot_change_24h[t] = ch

                # Futures mark/mini ticker: use <symbol>@markPrice or @ticker
                if handler_name == "futures":
                    # we use futures ticker too
                    if payload.get("e") in ("24hrTicker", "bookTicker", "markPriceUpdate"):
                        sym = payload.get("s", "").lower()
                        if "c" in payload:
                            last = float(payload.get("c"))
                        elif "p" in payload:
                            last = float(payload.get("p"))
                        else:
                            continue
                        for t, s in SYMBOLS.items():
                            if s.upper() == sym.upper():
                                fut_price[t] = last

async def main():
    # Streams:
    # Spot: per-symbol 24hr ticker
    spot_streams = [f"{SYMBOLS[t]}@ticker" for t in TOKENS]
    # Futures: mark price stream for BTC only is enough, but we’ll do tickers for all tokens
    fut_streams = [f"{SYMBOLS[t]}@ticker" for t in TOKENS]

    async def oi_poller():
        global last_btc_open_interest
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    last_btc_open_interest = await fetch_open_interest(session)
                except Exception:
                    pass
                await asyncio.sleep(60)

    async def writer_loop():
        while True:
            try:
                await write_outputs()
            except Exception:
                pass
            await asyncio.sleep(5)  # write often; page always fresh

    await asyncio.gather(
        ws_consumer(BINANCE_SPOT_WS, spot_streams, "spot"),
        ws_consumer(BINANCE_FUT_WS, fut_streams, "futures"),
        oi_poller(),
        writer_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())

