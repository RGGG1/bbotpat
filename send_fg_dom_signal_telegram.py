#!/usr/bin/env python3
"""
send_fg_dom_signal_telegram.py

Runs twice daily (05:15, 17:15 UTC).

Binance + supplies version:

- Load latest HMI from hmi_latest.json (root or docs/)
- Load circulating supplies from supplies_latest.json (root or docs/)
- Use Binance spot API for:
    * live prices and 24h change (ticker/24hr)
    * daily close prices (klines) for BTC + all alts (up to ~730d)
- Compute market caps = price * circulating_supply
- Compute per-token BTC dominance and ~2-yr dominance ranges (BTC vs each alt)
    * range horizon: up to 730 days of overlapping price history
    * record actual number of days used
- Compute BTC vs ALL ALTS dominance (excluding stables) + range + days
- For each alt:
    - Map dominance position within its range to
      BTC / ALT / STABLE weights (35% / 30% / 35% bands), respecting HMI
- Combine all per-token weights into one global allocation
    (BTC + each ALT + stables)
- Write:
    - dom_bands_latest.json
    - prices_latest.json
    - portfolio_weights.json
  in both root and docs/
- Send a Telegram message with summary + service health
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

BINANCE_SPOT = "https://api.binance.com"

ROOT = Path(".")
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True, parents=True)

HMI_FILES = [ROOT / "hmi_latest.json", DOCS / "hmi_latest.json"]
SUPPLIES_FILES = [ROOT / "supplies_latest.json", DOCS / "supplies_latest.json"]

DOM_JSON_ROOT = ROOT / "dom_bands_latest.json"
DOM_JSON_DOCS = DOCS / "dom_bands_latest.json"
PRICES_JSON_ROOT = ROOT / "prices_latest.json"
PRICES_JSON_DOCS = DOCS / "prices_latest.json"
PW_JSON_ROOT = ROOT / "portfolio_weights.json"
PW_JSON_DOCS = DOCS / "portfolio_weights.json"

TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

GREED_STABLE_THRESHOLD = 77.0
DAYS_HISTORY_TARGET = 730

# Symbols and their Binance pairs for USD-equivalent pricing
BINANCE_SYMBOLS = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "BNB":  "BNBUSDT",
    "SOL":  "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "TON":  "TONUSDT",
    "SUI":  "SUIUSDT",
    "UNI":  "UNIUSDT",
    "USDT": None,         # treat as $1
    "USDC": "USDCUSDT",
}

# Alts used for BTC vs Alts dominance (exclude stables)
ALTS_FOR_DOM = ["ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]

# Order for website table
DISPLAY_ORDER = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "USDTC", "SUI", "UNI"]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def bn_spot_get(path, params=None, timeout=30, sleep=0.1, max_retries=5):
    if params is None:
        params = {}
    url = BINANCE_SPOT + path
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = str(e)
        else:
            if r.status_code == 200:
                time.sleep(sleep)
                return r.json()
            if r.status_code in (429, 418, 500, 502, 503, 504):
                last_err = r.text[:300]
            else:
                raise RuntimeError(f"Binance error {r.status_code}: {r.text[:300]}")
        if attempt < max_retries:
            delay = sleep * attempt
            print(f"[Binance] retry {attempt}/{max_retries} in {delay:.2f}s…")
            time.sleep(delay)
    raise RuntimeError(f"Binance error after retries: {last_err}")


def load_hmi():
    for p in HMI_FILES:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                return float(js.get("hmi")), js.get("band", "")
            except Exception:
                continue
    return None, ""


def load_supplies():
    """
    Load circulating supplies from supplies_latest.json.
    Returns (supplies_dict, supplies_ok, missing_list).
    supplies_dict: sym -> circulating_supply (float)
    """
    for p in SUPPLIES_FILES:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                sup = js.get("supplies", {})
                out = {}
                for sym, entry in sup.items():
                    try:
                        out[sym] = float(entry.get("circulating_supply"))
                    except Exception:
                        continue
                missing = js.get("missing", [])
                return out, True, missing
            except Exception as e:
                print("[supplies] Error parsing", p, ":", e)

    # no file
    return {}, False, []


def fmt_mc(v):
    if v <= 0:
        return "$0"
    if v >= 1e12:
        return f"${v/1e12:.1f}T"
    if v >= 1e9:
        return f"${int(round(v/1e9))}B"
    if v >= 1e6:
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
        return 0.0, 0.0, 1.0

    t = (dom_pct - dom_min) / span
    t = max(0.0, min(1.0, t))

    if t < 0.35:
        local = t / 0.35
        w_btc = 1.0 - local
        w_alt = local
        return w_btc, w_alt, 0.0

    if t < 0.65:
        return 0.0, 0.0, 1.0

    local = (t - 0.65) / 0.35
    w_btc = 1.0 - local
    w_alt = local
    return w_btc, w_alt, 0.0


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[tg] Missing TG token or chat ID, skipping Telegram.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print("[tg] Error:", r.text[:200])
    except Exception as e:
        print("[tg] Exception:", e)


def fetch_price_history(symbol: str, days_limit=730):
    """
    Fetch up to `days_limit` daily closing prices for a Binance spot symbol,
    using /api/v3/klines with interval=1d and limit=days_limit.

    Returns {date -> close_price}.
    """
    data = bn_spot_get(
        "/api/v3/klines",
        params={"symbol": symbol, "interval": "1d", "limit": days_limit},
    )
    out = {}
    for k in data:
        open_time_ms = k[0]
        close_price = float(k[4])
        d = datetime.utcfromtimestamp(open_time_ms / 1000.0).date()
        out[d] = close_price
    return out


def load_previous_prices_ranges():
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
    for p in [DOM_JSON_ROOT, DOM_JSON_DOCS]:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                return js.get("min_pct"), js.get("max_pct"), js.get("days")
            except Exception:
                continue
    return None, None, None


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    health = {
        "hmi_ok": False,
        "binance_ok": True,
        "supplies_ok": False,
        "bands_ok": False,
        "prices_ok": False,
    }

    # 1) HMI
    hmi, hmi_band = load_hmi()
    if hmi is not None:
        health["hmi_ok"] = True

    # 2) Supplies
    supplies, supplies_ok, missing_sup = load_supplies()
    health["supplies_ok"] = supplies_ok

    # 3) Previous ranges for caching
    prev_ranges = load_previous_prices_ranges()
    prev_min_pct, prev_max_pct, prev_days_all = load_previous_dom_range()

    # 4) Live prices & tickers from Binance
    try:
        ticker_list = bn_spot_get("/api/v3/ticker/24hr")
        by_symbol = {row["symbol"]: row for row in ticker_list}
    except Exception as e:
        print("[dom] Binance ticker error:", e)
        health["binance_ok"] = False
        by_symbol = {}

    def live_price(sym):
        if sym in ("USDT", "USDTC"):
            return 1.0
        bsym = BINANCE_SYMBOLS.get(sym)
        if not bsym:
            return 0.0
        row = by_symbol.get(bsym)
        if not row:
            return 0.0
        try:
            return float(row["lastPrice"])
        except Exception:
            return 0.0

    def live_change_24h(sym):
        if sym in ("USDT", "USDTC"):
            return 0.0
        bsym = BINANCE_SYMBOLS.get(sym)
        if not bsym:
            return 0.0
        row = by_symbol.get(bsym)
        if not row:
            return 0.0
        try:
            return float(row.get("priceChangePercent", 0.0))
        except Exception:
            return 0.0

    def supply(sym):
        return float(supplies.get(sym, 0.0))

    def mc_live(sym):
        if sym == "USDTC":
            return supply("USDT") * 1.0 + supply("USDC") * 1.0
        p = live_price(sym)
        s = supply(sym)
        return p * s

    # live MCs
    btc_mc_now = mc_live("BTC")
    alt_mc_now_map = {sym: mc_live(sym) for sym in ALTS_FOR_DOM}
    alt_mc_now_total = sum(alt_mc_now_map.values())

    # 5) Historical price series (close prices) for BTC + alts
    btc_hist = {}
    alt_histories = {}
    try:
        bsym_btc = BINANCE_SYMBOLS["BTC"]
        btc_hist = fetch_price_history(bsym_btc, days_limit=DAYS_HISTORY_TARGET)
        for sym in ALTS_FOR_DOM:
            bsym_alt = BINANCE_SYMBOLS.get(sym)
            if not bsym_alt:
                alt_histories[sym] = {}
                continue
            alt_histories[sym] = fetch_price_history(bsym_alt, days_limit=DAYS_HISTORY_TARGET)
    except Exception as e:
        print("[dom] Binance klines error:", e)
        health["binance_ok"] = False

    # 6) Per-token dominance and weights

    per_token_dom = {}
    per_token_weights = {}
    per_token_days = {}

    for sym in ALTS_FOR_DOM:
        s_btc = supply("BTC")
        s_alt = supply(sym)
        if s_btc <= 0 or s_alt <= 0:
            per_token_dom[sym] = (None, 0.0, 0.0)
            per_token_weights[sym] = (0.0, 0.0, 1.0)
            per_token_days[sym] = 0
            continue

        dom_series = []
        btc_prices = btc_hist or {}
        alt_prices = alt_histories.get(sym, {}) or {}

        for d, p_btc in btc_prices.items():
            p_alt = alt_prices.get(d)
            if p_alt is None:
                continue
            mc_btc_d = p_btc * s_btc
            mc_alt_d = p_alt * s_alt
            tot = mc_btc_d + mc_alt_d
            if tot <= 0:
                continue
            dom_series.append(100.0 * mc_btc_d / tot)

        days_count = len(dom_series)

        # current dominance now
        mc_btc_now_sym = btc_mc_now
        mc_alt_now_sym = alt_mc_now_map.get(sym, 0.0)
        tot_now = mc_btc_now_sym + mc_alt_now_sym
        dom_now = 100.0 * mc_btc_now_sym / tot_now if tot_now > 0 else None

        if days_count > 0:
            dom_min = min(dom_series)
            dom_max = max(dom_series)
        elif dom_now is not None:
            dom_min = dom_max = dom_now
        else:
            dom_min = dom_max = 0.0

        if dom_now is not None and dom_max > dom_min:
            w_btc, w_alt, w_st = weights_from_dom(dom_now, dom_min, dom_max, hmi)
        else:
            w_btc, w_alt, w_st = (0.0, 0.0, 1.0)

        per_token_dom[sym] = (dom_now, dom_min, dom_max)
        per_token_weights[sym] = (w_btc, w_alt, w_st)
        per_token_days[sym] = days_count

    # 7) Aggregate BTC vs ALL ALTS dominance range

    s_btc = supply("BTC")
    alt_supplies = {sym: supply(sym) for sym in ALTS_FOR_DOM}
    alt_hist_total = {}

    for sym in ALTS_FOR_DOM:
        s_alt = alt_supplies.get(sym, 0.0)
        if s_alt <= 0:
            continue
        for d, p_alt in (alt_histories.get(sym, {}) or {}).items():
            alt_hist_total.setdefault(d, 0.0)
            alt_hist_total[d] += p_alt * s_alt

    dom_all_series = []
    btc_prices = btc_hist or {}

    for d, p_btc in btc_prices.items():
        mc_btc_d = p_btc * s_btc
        mc_alt_d = alt_hist_total.get(d, 0.0)
        tot = mc_btc_d + mc_alt_d
        if tot <= 0:
            continue
        dom_all_series.append(100.0 * mc_btc_d / tot)

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
        if prev_min_pct is not None and prev_max_pct is not None:
            dom_all_min = float(prev_min_pct)
            dom_all_max = float(prev_max_pct)
            days_all = prev_days_all or 0
        else:
            dom_all_min = dom_all_max = btc_dom_all_now

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

    # 8) Build prices_latest.json rows for website

    rows = []

    mc_usdt = mc_live("USDT")
    mc_usdc = mc_live("USDC")
    mc_usdtc = mc_usdt + mc_usdc

    health["prices_ok"] = bool(by_symbol)

    for sym in DISPLAY_ORDER:
        if sym == "USDTC":
            price_val = 1.0
            mc_val = mc_usdtc
            change_val = 0.0
            dom_now = None
            rng_str = ""
        elif sym == "BTC":
            price_val = live_price(sym)
            mc_val = btc_mc_now
            change_val = live_change_24h(sym)
            dom_now = None
            rng_str = ""
        else:
            price_val = live_price(sym)
            mc_val = mc_live(sym)
            change_val = live_change_24h(sym)

            dom_info = per_token_dom.get(sym)
            days_count = per_token_days.get(sym, 0)

            if dom_info:
                dom_now, dom_min, dom_max = dom_info
                if dom_now is not None:
                    mn_i = round(dom_min)
                    mx_i = round(dom_max)
                    base = f"{mn_i}–{mx_i}%"
                    if days_count > 0:
                        if days_count < DAYS_HISTORY_TARGET:
                            rng_str = f"{base} ({days_count}d)"
                        else:
                            rng_str = base
                    else:
                        prev = prev_ranges.get(sym)
                        rng_str = prev if prev else "N/A (0d)"
                else:
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

    # 9) Build dom_bands_latest.json

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

    # 10) Combine mini-portfolios into global allocation

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
            "weight": round(global_weights[k], 4),
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

    # 11) Telegram message

    def pct_str(x):
        return f"{x*100:.1f}%"

    lines = []
    lines.append("<b>HiveAI Rotation Update</b>")
    lines.append("")
    if hmi is not None:
        lines.append(f"HMI: <b>{hmi:.1f}</b> ({hmi_band})")
    else:
        lines.append("HMI: unavailable")

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
    lines.append(f"Binance: {'yes' if health['binance_ok'] else 'no'}")
    lines.append(f"Supplies file: {'yes' if health['supplies_ok'] else 'no'}")
    lines.append(f"Bands: {'yes' if health['bands_ok'] else 'no'}")
    lines.append(f"Prices: {'yes' if health['prices_ok'] else 'no'}")

    tg_send("\n".join(lines))

    print("[dom] Updated dom_bands_latest.json, prices_latest.json, portfolio_weights.json")


if __name__ == "__main__":
    main()
        
