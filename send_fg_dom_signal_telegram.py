#!/usr/bin/env python3
"""
Send Telegram alert with:

- Current HMI (Hive Mind Index) level and tier name
- Emoji colour (circles) based on tier
- BTC dominance vs ALT (BTC:ALT % as '75/25')
- BTC / ETH / SOL prices
- Current trade signal from dominance + HMI
- Percent-of-portfolio change vs previous day
- Suggested allocation based on fully linear model
"""

import os
import time

import requests
import pandas as pd

COINBASE_BASE   = "https://api.exchange.coinbase.com"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"

DOM_LOW       = 0.75
DOM_HIGH      = 0.81
DOM_MID_LOW   = 0.771
DOM_MID_HIGH  = 0.789
GREED_STABLE_THRESHOLD = 77.0  # HMI >= 77 => fully stables

RISK_IDS = ["bitcoin", "ethereum", "solana", "binancecoin"]

# Support both TELEGRAM_* and TG_* env names
TG_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TG_BOT_TOKEN")
)
TG_CHAT  = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TG_CHAT_ID")
)


def cb_get(path, params=None, sleep=0.1):
    if params is None:
        params = {}
    url = COINBASE_BASE + path
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    time.sleep(sleep)
    return r.json()


def cg_get(path, params=None, sleep=0.3):
    if params is None:
        params = {}
    url = COINGECKO_BASE + path
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    time.sleep(sleep)
    return r.json()


def load_latest_hmi(path="output/fg2_daily.csv"):
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date")
    row = df.iloc[-1]
    # FG_lite in CSV is our HMI
    return float(row["FG_lite"]), row["date"].date()


def hmi_band_name_and_emoji(hmi):
    """
    HMI tiers:

    <10      -> Zombie apocalypse
    10–25    -> McDonalds applications
    25–40    -> Ngmi
    40–60    -> Stable
    60–80    -> We're early
    80+      -> It's the future of finance
    """
    if hmi < 10:
        return "Zombie apocalypse", "⚫🧟‍♂️"
    elif hmi < 25:
        return "McDonalds applications", "🔴🍔"
    elif hmi < 40:
        return "Ngmi", "🟠📉"
    elif hmi < 60:
        return "Stable", "⚪😐"
    elif hmi < 80:
        return "We're early", "🟢🚀"
    else:
        return "It's the future of finance", "🟢🟢🌈"


def allocation_from_dom_and_hmi(btc_dom, hmi):
    """
    Same allocation model as in the backtest:

    1) HMI >= 77 -> 100% stables
    2) 0.771 < dom < 0.789 -> 100% stables
    3) dom <= 0.75 -> 100% BTC
    4) dom >= 0.81 -> 100% ALTs
    5) BTC side linear between 0.75–0.771
    6) ALT side linear between 0.789–0.81
    """

    # 1) Greed override
    if hmi >= GREED_STABLE_THRESHOLD:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 2) Stable mid-zone (open interval)
    if DOM_MID_LOW < btc_dom < DOM_MID_HIGH:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 3) Extremes
    if btc_dom <= DOM_LOW:
        return {"btc": 1.0, "alts": 0.0, "stables": 0.0}
    if btc_dom >= DOM_HIGH:
        return {"btc": 0.0, "alts": 1.0, "stables": 0.0}

    # 4) BTC side linear: DOM_LOW < dom <= DOM_MID_LOW
    if btc_dom <= DOM_MID_LOW:
        # dom = 0.75   -> btc = 1, alt = 0
        # dom = 0.771  -> btc = 0, alt = 1
        t = (btc_dom - DOM_LOW) / (DOM_MID_LOW - DOM_LOW)
        btc_w = 1.0 - t
        alt_w = t
        return {"btc": btc_w, "alts": alt_w, "stables": 0.0}

    # 5) ALT side linear: DOM_MID_HIGH <= dom < DOM_HIGH
    if btc_dom >= DOM_MID_HIGH:
        # dom = 0.789 -> btc = 1, alt = 0
        # dom = 0.81  -> btc = 0, alt = 1
        t = (btc_dom - DOM_MID_HIGH) / (DOM_HIGH - DOM_MID_HIGH)
        btc_w = 1.0 - t
        alt_w = t
        return {"btc": btc_w, "alts": alt_w, "stables": 0.0}

    # Fallback
    return {"btc": 0.0, "alts": 0.0, "stables": 1.0}


def fetch_spot_prices():
    prices = {}
    for sym in ["BTC-USD", "ETH-USD", "SOL-USD"]:
        js = cb_get(f"/products/{sym}/ticker")
        prices[sym.split("-")[0]] = float(js["price"])
    return prices


def fetch_btc_dom_current():
    js = cg_get(
        "/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ",".join(RISK_IDS),
            "per_page": len(RISK_IDS),
            "page": 1
        }
    )
    caps = {row["id"]: row["market_cap"] for row in js}
    btc_mc = caps.get("bitcoin", 0.0)
    alt_mc = caps.get("ethereum", 0.0) + caps.get("solana", 0.0) + caps.get("binancecoin", 0.0)
    if btc_mc + alt_mc == 0:
        return 0.5
    dom = btc_mc / (btc_mc + alt_mc)
    return dom


def load_last_two_allocations(path="output/equity_curve_fg_dom.csv"):
    if not os.path.exists(path):
        return None, None
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date")
    if len(df) < 2:
        return None, None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_w = {
        "btc": float(prev["w_btc"]),
        "alts": float(prev["w_alts"]),
        "stables": float(prev["w_stables"]),
    }
    curr_w = {
        "btc": float(curr["w_btc"]),
        "alts": float(curr["w_alts"]),
        "stables": float(curr["w_stables"]),
    }
    return prev_w, curr_w


def describe_allocation_change(prev_w, curr_w):
    if prev_w is None or curr_w is None:
        return "No prior allocation history. (First run or file missing.)", ""

    deltas = {k: curr_w[k] - prev_w[k] for k in ["btc", "alts", "stables"]}
    deltas_pct = {k: round(100 * v, 1) for k, v in deltas.items()}

    if all(abs(v) < 1.0 for v in deltas_pct.values()):
        detail = (
            f"Allocations unchanged (Δ<1% each).\n"
            f"Now: BTC {curr_w['btc']*100:.1f}%, ALTs {curr_w['alts']*100:.1f}%, "
            f"Stables {curr_w['stables']*100:.1f}%."
        )
        return "No major reallocation today.", detail

    src_asset = min(deltas, key=lambda k: deltas[k])
    dst_asset = max(deltas, key=lambda k: deltas[k])
    flow_size = min(abs(deltas_pct[src_asset]), abs(deltas_pct[dst_asset]))
    flow_size = round(flow_size, 1)

    asset_names = {"btc": "BTC", "alts": "ALTs", "stables": "Stables"}
    main_flow = f"Move ~{flow_size:.1f}% of portfolio from {asset_names[src_asset]} → {asset_names[dst_asset]}."

    detail = (
        f"Allocation change vs yesterday:\n"
        f" • BTC: {prev_w['btc']*100:.1f}% → {curr_w['btc']*100:.1f}% (Δ {deltas_pct['btc']:+.1f}%)\n"
        f" • ALTs: {prev_w['alts']*100:.1f}% → {curr_w['alts']*100:.1f}% (Δ {deltas_pct['alts']:+.1f}%)\n"
        f" • Stables: {prev_w['stables']*100:.1f}% → {curr_w['stables']*100:.1f}% (Δ {deltas_pct['stables']:+.1f}%)"
    )

    return main_flow, detail


def format_signal_message():
    hmi, hmi_date = load_latest_hmi()
    band_name, emoji = hmi_band_name_and_emoji(hmi)

    prices = fetch_spot_prices()
    btc_dom = fetch_btc_dom_current()
    w = allocation_from_dom_and_hmi(btc_dom, hmi)

    btc_pct_dom = int(round(btc_dom * 100))
    alt_pct_dom = 100 - btc_pct_dom

    if w["stables"] == 1.0:
        signal = "Stable all (100% Stables)."
    elif w["btc"] > w["alts"]:
        signal = f"Rotate toward BTC ({w['btc']*100:.1f}% BTC / {w['alts']*100:.1f}% ALTs)."
    else:
        signal = f"Rotate toward ALTs ({w['alts']*100:.1f}% ALTs / {w['btc']*100:.1f}% BTC)."

    prev_w, curr_w = load_last_two_allocations()
    flow_summary, flow_detail = describe_allocation_change(prev_w, curr_w)

    sugg_btc = w["btc"] * 100
    sugg_alt = w["alts"] * 100
    sugg_stb = w["stables"] * 100

    text = (
        f"🧠 *Hive Mind Index (HMI) & Dominance Signal*\n"
        f"`{hmi_date}`\n\n"
        f"{emoji} *HMI:* {hmi:.1f} — _{band_name}_\n"
        f"📊 *BTC dominance:* {btc_pct_dom}/{alt_pct_dom}\n\n"
        f"💰 *Prices:*\n"
        f" • BTC: ${prices['BTC']:.0f}\n"
        f" • ETH: ${prices['ETH']:.0f}\n"
        f" • SOL: ${prices['SOL']:.2f}\n\n"
        f"📐 *Suggested allocation:*\n"
        f" • BTC: {sugg_btc:.1f}%\n"
        f" • ALTs: {sugg_alt:.1f}%\n"
        f" • Stables: {sugg_stb:.1f}%\n\n"
        f"⚙️ *Signal:* {signal}\n\n"
        f"🔁 *Portfolio Flow:*\n"
        f"{flow_summary}\n"
        f"{flow_detail}"
    )
    return text


def send_telegram_message(text: str):
    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError(
            "Telegram env vars not set "
            "(TELEGRAM_BOT_TOKEN/TG_BOT_TOKEN or TELEGRAM_CHAT_ID/TG_CHAT_ID)."
        )
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def main():
    msg = format_signal_message()
    resp = send_telegram_message(msg)
    print("Sent Telegram message, message_id:", resp.get("result", {}).get("message_id"))


if __name__ == "__main__":
    main()
    
