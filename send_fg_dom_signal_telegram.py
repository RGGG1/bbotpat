#!/usr/bin/env python3
"""
send_fg_dom_signal_telegram.py

Runs twice daily (05:15, 17:15 UTC).

Responsibilities:
- Load latest HMI from hmi_latest.json (root or docs/)
- Fetch live prices + market caps for:
    BTC, ETH, BNB, SOL, DOGE, TON, SUI, UNI, USDT, USDC
- Compute per-token BTC dominance and ~2-yr dominance ranges (BTC vs each alt)
    * target horizon: 730 days, with fallbacks to 365, 180 and 'max'
    * record the actual number of days of overlapping history
    * if no new history, reuse last-good ranges from previous JSON
- Compute BTC vs ALL ALTS dominance (excluding stables) + range + days
    * if no new history, reuse last-good aggregate range
- For each alt:
    - Map dominance position within its range to
      BTC / ALT / STABLE weights (35% / 30% / 35% bands)
- Combine all per-token weights into one global allocation
    (BTC + each ALT + stables)
- Write:
    - dom_bands_latest.json (BTC vs Alts aggregate)
    - prices_latest.json (for website token table)
    - portfolio_weights.json (combined allocation)
  in both root and docs/
- Send a Telegram message with summary + service health.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

COINGECKO = "https://api.coingecko.com/api/v3"
DAYS_HISTORY_TARGET = 730  # approx 2 years
# note: last entry 'max' uses CoinGecko's full-history shortcut
FALLBACK_HORIZONS = [DAYS_HISTORY_TARGET, 365, 180, "max"]

ROOT = Path(".")
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True, parents=True)

HMI_FILES = [ROOT / "hmi_latest.json", DOCS / "hmi_latest.json"]
DOM_JSON_ROOT = ROOT / "dom_bands_latest.json"
DOM_JSON_DOCS = DOCS / "dom_bands_latest.json"
PRICES_JSON_ROOT = ROOT / "prices_latest.json"
PRICES_JSON_DOCS = DOCS / "prices_latest.json"
PW_JSON_ROOT = ROOT / "portfolio_weights.json"
PW_JSON_DOCS = DOCS / "portfolio_weights.json"

TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

GREED_STABLE_THRESHOLD = 77.0

# Token universe
TOKENS = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "BNB":  "binancecoin",
    "SOL":  "solana",
    "DOGE": "dogecoin",
    "TON":  "the-open-network",  # CoinGecko id
    "SUI":  "sui",
    "UNI":  "uniswap",
    "USDT": "tether",
    "USDC": "usd-coin",
}

# Alts used for BTC vs Alts dominance (exclude stables)
ALTS_FOR_DOM = ["ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]

# Order for website table
DISPLAY_ORDER = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "USDTC", "SUI", "UNI"]


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def cg_get(path, params=None, timeout=60):
    """Simple GET for live markets; fail fast on error."""
    if params is None:
        params = {}
    url = COINGECKO + path
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"CoinGecko error {r.status_code}: {r.text[:200]}")
    return r.json()


def cg_get_history(path, params=None, timeout=60, max_retries=4, base_pause=2.0):
    """
    GET for historical endpoints (market_chart) with retries + exponential backoff.
    Retries on network errors, 429, and 5xx.
    """
    if params is None:
        params = {}
    url = COINGECKO + path
    last_err = None

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except Exception as e:
            last_err = e
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(
                    f"status {r.status_code}: {r.text[:200]}"
                )
            else:
                # hard failure (4xx other than 429)
                raise RuntimeError(
                    f"CoinGecko history error {r.status_code}: {r.text[:200]}"
                )
        # backoff
        sleep_s = base_pause * attempt
        print(f"[history] retrying in {sleep_s:.1f}s for {path} with params {params}")
        time.sleep(sleep_s)

    raise RuntimeError(f"CoinGecko history failed after retries: {last_err}")


def load_hmi():
    for p in HMI_FILES:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                return float(js.get("hmi")), js.get("band", "")
            except Exception:
                continue
    return None, ""


def fmt_mc(v):
    if v <= 0:
        return "$0"
    if v >= 1e12:
        # trillions: one decimal, e.g. $1.2T
        return f"${v/1e12:.1f}T"
    if v >= 1e9:
        # billions: integer, e.g. $600B
        return f"${int(round(v/1e9))}B"
    if v >= 1e6:
        # millions: integer, e.g. $25M
        return f"${int(round(v/1e6))}M"
    return f"${int(v):,}"


def weights_from_dom(dom_pct, dom_min, dom_max, hmi):
    """
    Map dominance to (w_btc, w_alt, w_stables) with:
    - lower 35% of range: BTC -> ALTs linearly
    - middle 30%: 100% stables
    - upper 35%: BTC -> ALTs linearly
    HMI override: if hmi >= GREED_STABLE_THRESHOLD => 100% stables.
    """
    if hmi is not None and hmi >= GREED_STABLE_THRESHOLD:
        return 0.0, 0.0, 1.0

    span = dom_max - dom_min
    if span <= 0:
        # no meaningful range; safest is stables
        return 0.0, 0.0, 1.0

    t = (dom_pct - dom_min) / span
    t = max(0.0, min(1.0, t))

    # lower 35%: BTC-heavy -> mix
    if t < 0.35:
        local = t / 0.35  # 0..1
        w_btc = 1.0 - local
        w_alt = local
        return w_btc, w_alt, 0.0

    # middle 30%: stable midzone
    if t < 0.65:
        return 0.0, 0.0, 1.0

    # upper 35%: fade BTC to ALTs
    local = (t - 0.65) / 0.35  # 0..1
    w_btc = 1.0 - local
    w_alt = local
    return w_btc, w_alt, 0.0


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[tg] Missing TG token or chat ID, skipping Telegram.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)
    if r.status_code != 200:
        print("[tg] Error:", r.text[:200])


def fetch_mc_history(coin_id: str):
    """
    Try to fetch historical market caps for `coin_id` using several horizons
    (730d, 365d, 180d, max). Return (history_dict, days_available):

    - history_dict: {date -> market_cap}
    - days_available: number of distinct dates
    """
    last_err = None
    for horizon in FALLBACK_HORIZONS:
        try:
            if horizon == "max":
                params = {"vs_currency": "usd", "days": "max"}
            else:
                params = {"vs_currency": "usd", "days": str(horizon)}

            js = cg_get_history(
                f"/coins/{coin_id}/market_chart",
                params=params,
                timeout=80,
                max_retries=4,
                base_pause=2.0,
            )
        except Exception as e:
            last_err = e
            continue

        out = {}
        for ts, cap in js.get("market_caps", []):
            d = datetime.utcfromtimestamp(ts / 1000.0).date()
            out[d] = float(cap)

        if out:
            print(f"[history] {coin_id}: {len(out)} days (requested {horizon})")
            return out, len(out)

    if last_err:
        print(f"[history] {coin_id} failed: {last_err}")
    else:
        print(f"[history] {coin_id}: no data returned")
    return {}, 0


def load_previous_prices_ranges():
    """
    Load previous per-token Range strings from prices_latest.json (if any)
    so that we can reuse them when a fresh run can't compute new ranges.
    """
    for p in [PRICES_JSON_ROOT, PRICES_JSON_DOCS]:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                rows = js.get("rows", [])
                return {row.get("token"): row.get("range", "") for row in rows}
            except Exception:
                continue
    return {}


def load_previous_dom_range():
    """
    Load previous aggregate BTC vs Alts range from dom_bands_latest.json (if any).
    Returns (min_pct, max_pct, days).
    """
    for p in [DOM_JSON_ROOT, DOM_JSON_DOCS]:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                return js.get("min_pct"), js.get("max_pct"), js.get("days")
            except Exception:
                continue
    return None, None, None


# ------------------------------------------------------------------
# Main logic
# ------------------------------------------------------------------

def main():
    health = {
        "hmi_ok": False,
        "cg_ok": True,
        "bands_ok": False,
        "prices_ok": False,
    }

    # 1) Load HMI
    hmi, hmi_band = load_hmi()
    if hmi is not None:
        health["hmi_ok"] = True

    # 2) Load previous ranges for caching
    prev_ranges = load_previous_prices_ranges()
    prev_min_pct, prev_max_pct, prev_days_all = load_previous_dom_range()

    # 3) Live prices & MCs (single call)
    try:
        ids_str = ",".join(TOKENS.values())
        js = cg_get(
            "/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids_str,
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
        )
        by_id = {row["id"]: row for row in js}
    except Exception as e:
        print("[dom] CoinGecko markets error:", e)
        health["cg_ok"] = False
        by_id = {}

    def price(sym):
        r = by_id.get(TOKENS[sym])
        return float(r["current_price"]) if r else 0.0

    def mc(sym):
        r = by_id.get(TOKENS[sym])
        return float(r["market_cap"]) if r and r.get("market_cap") else 0.0

    def change_24h(sym):
        r = by_id.get(TOKENS[sym])
        if not r:
            return 0.0
        v = r.get("price_change_percentage_24h")
        return float(v) if v is not None else 0.0

    btc_mc_now = mc("BTC")
    alt_mc_now_total = sum(mc(sym) for sym in ALTS_FOR_DOM)

    # 4) Historical dominance ranges for BTC vs each alt & aggregate

    # Fetch BTC history once
    btc_hist, btc_hist_days = fetch_mc_history(TOKENS["BTC"])

    # Fetch alt histories once and store
    alt_histories = {}
    alt_hist_days = {}
    for sym in ALTS_FOR_DOM:
        hdict, dcount = fetch_mc_history(TOKENS[sym])
        alt_histories[sym] = hdict
        alt_hist_days[sym] = dcount

    # Per-token dominance data: sym -> (dom_now, dom_min, dom_max, days_count)
    per_token_dom = {}
    per_token_weights = {}
    per_token_days = {}

    for sym in ALTS_FOR_DOM:
        alt_hist = alt_histories.get(sym, {})
        dom_series = []
        for d, btc_cap in btc_hist.items():
            alt_cap = alt_hist.get(d)
            if alt_cap is None:
                continue
            tot = btc_cap + alt_cap
            if tot <= 0:
                continue
            dom_series.append(100.0 * btc_cap / tot)

        days_count = len(dom_series)

        # current dominance now
        alt_mc_now = mc(sym)
        tot_now = btc_mc_now + alt_mc_now
        dom_now = 100.0 * btc_mc_now / tot_now if tot_now > 0 else None

        if days_count > 0:
            dom_min = min(dom_series)
            dom_max = max(dom_series)
        elif dom_now is not None:
            # no overlapping history, but we at least know today's dominance
            dom_min = dom_max = dom_now
        else:
            dom_min = dom_max = 0.0

        if dom_now is not None and dom_max > dom_min:
            w_btc, w_alt, w_st = weights_from_dom(dom_now, dom_min, dom_max, hmi)
        else:
            # no safe range, default to stables
            w_btc, w_alt, w_st = (0.0, 0.0, 1.0)

        per_token_dom[sym] = (dom_now, dom_min, dom_max)
        per_token_weights[sym] = (w_btc, w_alt, w_st)
        per_token_days[sym] = days_count

    # Aggregate BTC vs ALL ALTS dominance range
    alt_hist_total = {}
    for sym in ALTS_FOR_DOM:
        for d, cap in alt_histories.get(sym, {}).items():
            alt_hist_total[d] = alt_hist_total.get(d, 0.0) + cap

    dom_all_series = []
    for d, btc_cap in btc_hist.items():
        alt_cap = alt_hist_total.get(d, 0.0)
        tot = btc_cap + alt_cap
        if tot <= 0:
            continue
        dom_all_series.append(100.0 * btc_cap / tot)

    days_all = len(dom_all_series)

    if btc_mc_now + alt_mc_now_total > 0:
        btc_dom_all_now = 100.0 * btc_mc_now / (btc_mc_now + alt_mc_now_total)
        alt_dom_all_now = 100.0 - btc_dom_all_now
    else:
        btc_dom_all_now = 50.0
        alt_dom_all_now = 50.0

    if days_all > 0:
        dom_all_min = min(dom_all_series)
        dom_all_max = max(dom_all_series)
    else:
        # no aggregate history this run; reuse previous if possible
        if prev_min_pct is not None and prev_max_pct is not None:
            dom_all_min = float(prev_min_pct)
            dom_all_max = float(prev_max_pct)
            days_all = prev_days_all or 0
        else:
            # absolute last resort: treat today's dominance as degenerate range
            dom_all_min = dom_all_max = btc_dom_all_now

    # For aggregate action, reuse weights_from_dom on BTC vs All Alts
    if dom_all_max > dom_all_min:
        w_btc_all, w_alt_all, w_st_all = weights_from_dom(
            btc_dom_all_now, dom_all_min, dom_all_max, hmi
        )
    else:
        w_btc_all, w_alt_all, w_st_all = (0.0, 0.0, 1.0)

    if w_st_all > max(w_btc_all, w_alt_all):
        agg_action = "Stable up"
    elif w_btc_all >= w_alt_all:
        agg_action = "Buy BTC"
    else:
        agg_action = "Buy Alts"

    # 5) Build prices_latest.json rows (for website)
    rows = []

    usdt_mc = mc("USDT")
    usdc_mc = mc("USDC")
    usdctc_mc = usdt_mc + usdc_mc

    health["prices_ok"] = bool(by_id)

    for sym in DISPLAY_ORDER:
        if sym == "USDTC":
            price_val = 1.0
            mc_val = usdctc_mc
            change_val = 0.0
            dom_now = None
            rng_str = ""
        elif sym == "BTC":
            price_val = price(sym)
            mc_val = mc(sym)
            change_val = change_24h(sym)
            dom_now = None
            rng_str = ""
        else:
            price_val = price(sym)
            mc_val = mc(sym)
            change_val = change_24h(sym)

            dom_info = per_token_dom.get(sym)
            days_count = per_token_days.get(sym, 0)

            if dom_info:
                dom_now, dom_min, dom_max = dom_info
                mn_i = round(dom_min)
                mx_i = round(dom_max)
                base = f"{mn_i}–{mx_i}%"

                if days_count > 0:
                    # show range and days if we have any history
                    if days_count < DAYS_HISTORY_TARGET:
                        rng_str = f"{base} ({days_count}d)"
                    else:
                        rng_str = base
                else:
                    # no new history this run; try to reuse previous range
                    prev = prev_ranges.get(sym)
                    rng_str = prev if prev else "N/A (0d)"
            else:
                dom_now = None
                prev = prev_ranges.get(sym)
                rng_str = prev if prev else "N/A (0d)"

        rows.append({
            "token": sym,
            "price": price_val,
            "mc": mc_val,
            "change_24h": change_val,
            "btc_dom": round(dom_now, 1) if dom_now is not None else None,
            "range": rng_str,
        })

    prices_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "health": health,
        "rows": rows,
    }
    text_prices = json.dumps(prices_payload, indent=2)
    PRICES_JSON_ROOT.write_text(text_prices)
    PRICES_JSON_DOCS.write_text(text_prices)

    # 6) Build dom_bands_latest.json (aggregate BTC vs Alts)

    agg_mn_i = round(dom_all_min)
    agg_mx_i = round(dom_all_max)

    dom_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "btc_pct": round(btc_dom_all_now, 1),
        "alt_pct": round(alt_dom_all_now, 1),
        "min_pct": float(agg_mn_i),
        "max_pct": float(agg_mx_i),
        "days": days_all,
        "btc_mc_fmt": fmt_mc(btc_mc_now),
        "alt_mc_fmt": fmt_mc(alt_mc_now_total),
        "action": agg_action,
    }
    text_dom = json.dumps(dom_payload, indent=2)
    DOM_JSON_ROOT.write_text(text_dom)
    DOM_JSON_DOCS.write_text(text_dom)
    health["bands_ok"] = True

    # 7) Combine mini-portfolios into global allocation

    global_weights = {"BTC": 0.0, "STABLES": 0.0}
    for sym in ALTS_FOR_DOM:
        global_weights[sym] = 0.0

    slot_count = len(ALTS_FOR_DOM) or 1

    for sym in ALTS_FOR_DOM:
        w_btc, w_alt, w_st = per_token_weights.get(sym, (0.0, 0.0, 1.0))
        slot_factor = 1.0 / slot_count
        global_weights["BTC"] += w_btc * slot_factor
        global_weights["STABLES"] += w_st * slot_factor
        global_weights[sym] += w_alt * slot_factor

    total = sum(global_weights.values())
    if total <= 0:
        global_weights = {k: (1.0 if k == "STABLES" else 0.0) for k in global_weights}
        total = 1.0

    for k in list(global_weights.keys()):
        global_weights[k] = global_weights[k] / total

    portfolio_rows = []
    for k in ["BTC"] + ALTS_FOR_DOM + ["STABLES"]:
        if k not in global_weights:
            continue
        portfolio_rows.append({
            "asset": k,
            "weight": round(global_weights[k], 4)
        })

    pw_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "hmi": hmi,
        "hmi_band": hmi_band,
        "weights": portfolio_rows,
    }
    text_pw = json.dumps(pw_payload, indent=2)
    PW_JSON_ROOT.write_text(text_pw)
    PW_JSON_DOCS.write_text(text_pw)

    # 8) Telegram message

    def pct_str(x):
        return f"{x*100:.1f}%"

    lines = []
    lines.append("<b>HiveAI Rotation Update</b>")
    lines.append("")
    lines.append(f"HMI: <b>{hmi:.1f}</b> ({hmi_band})" if hmi is not None else "HMI: unavailable")
    if days_all > 0:
        lines.append(
            f"BTC vs Alts: <b>{dom_payload['btc_pct']:.1f}%</b> "
            f"(range {agg_mn_i}–{agg_mx_i}% over {days_all}d)"
        )
    else:
        lines.append(
            f"BTC vs Alts: <b>{dom_payload['btc_pct']:.1f}%</b> "
            f"(range {agg_mn_i}–{agg_mx_i}%, 0d)"
        )
    lines.append(f"Action: <b>{agg_action}</b>")
    lines.append("")
    lines.append("<b>Portfolio weights</b>:")

    for row in portfolio_rows:
        lines.append(f"{row['asset']}: {pct_str(row['weight'])}")

    lines.append("")
    lines.append("<b>Service health</b>:")
    lines.append(f"HMI: {'yes' if health['hmi_ok'] else 'no'}")
    lines.append(f"CoinGecko: {'yes' if health['cg_ok'] else 'no'}")
    lines.append(f"Bands: {'yes' if health['bands_ok'] else 'no'}")
    lines.append(f"Prices: {'yes' if health['prices_ok'] else 'no'}")

    tg_send("\n".join(lines))

    print("[dom] Updated dom_bands_latest.json, prices_latest.json, portfolio_weights.json")


if __name__ == "__main__":
    main()
        
