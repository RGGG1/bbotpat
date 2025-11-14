#!/usr/bin/env python3
"""
Telegram alert for 'Hive' (HMI + BTC dominance rotation).

Message layout:

Hive   14/11/25    💵 Balance: $xxx.xx

⚪ HMI: 49.7 — Stable
📊 BTC vs EthSol: 77/23

💰 Prices:
 • BTC: $...
 • ETH: $...
 • SOL: $...

🧠 Suggested allocation:
 • BTC: 20.6% - $X
 • ALTs: 79.4% - $Y
 • Stables: 0.0% - $Z

⚙️ Action: Rotate X% of <SRC> to <DST>.

🔁 Portfolio Flow:
Yesterday: a% BTC, b% ALTs, c% Stables
Today:     d% BTC, e% ALTs, f% Stables

🧠 Hive ROI:  +P% / +$P
BTC buy & hold ROI: +Q% / +$Q
Hive is outperforming BTC buy & hold by Yx.
"""

import os
import time
from datetime import datetime

import requests
import pandas as pd

COINBASE_BASE   = "https://api.exchange.coinbase.com"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"

# Dominance thresholds (must match backtest)
DOM_LOW       = 0.75
DOM_HIGH      = 0.81
DOM_MID_LOW   = 0.771
DOM_MID_HIGH  = 0.789
GREED_STABLE_THRESHOLD = 77.0  # HMI >= 77 => fully stables

# Assets used in dominance & prices
RISK_IDS  = ["bitcoin", "ethereum", "solana", "binancecoin"]
ALT_IDS   = ["ethereum", "solana", "binancecoin"]
ALT_LABEL = {
    "ethereum": "Eth",
    "solana": "Sol",
    "binancecoin": "Bnb",
}

EQUITY_CSV = "output/equity_curve_fg_dom.csv"
HMI_CSV    = "output/fg2_daily.csv"

INITIAL_CAPITAL_FOR_ROI = 100.0  # your original starting capital


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


# ---------- HMI & Price / Dominance Data ----------

def load_latest_hmi(path=HMI_CSV):
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
        return "Zombie apocalypse", "⚫"
    elif hmi < 25:
        return "McDonalds applications", "🔴"
    elif hmi < 40:
        return "Ngmi", "🟠"
    elif hmi < 60:
        return "Stable", "⚪"
    elif hmi < 80:
        return "We're early", "🟢"
    else:
        return "It's the future of finance", "🟢"


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
        t = (btc_dom - DOM_LOW) / (DOM_MID_LOW - DOM_LOW)
        btc_w = 1.0 - t
        alt_w = t
        return {"btc": btc_w, "alts": alt_w, "stables": 0.0}

    # 5) ALT side linear: DOM_MID_HIGH <= dom < DOM_HIGH
    if btc_dom >= DOM_MID_HIGH:
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


def fetch_dominance_and_alt_label():
    """
    Returns (btc_dom, label_str) where label_str is e.g. 'EthSolBnb'
    based on ALT_IDS sorted by *current* market cap.
    """
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
    alt_caps = [(aid, caps.get(aid, 0.0)) for aid in ALT_IDS]
    alt_caps_sorted = sorted(alt_caps, key=lambda x: x[1], reverse=True)

    alt_mc_sum = sum(mc for _, mc in alt_caps_sorted)

    if btc_mc + alt_mc_sum == 0:
        btc_dom = 0.5
    else:
        btc_dom = btc_mc / (btc_mc + alt_mc_sum)

    # Build label like 'EthSol' or 'EthSolBnb'
    alt_label = "".join(ALT_LABEL.get(aid, aid[:3].title()) for aid, _ in alt_caps_sorted)

    return btc_dom, alt_label


# ---------- Equity & Allocation History ----------

def load_equity_snapshot(path=EQUITY_CSV):
    """
    Returns (last_row, prev_row) where rows include
    date, equity, btc_only, w_btc, w_alts, w_stables.
    """
    if not os.path.exists(path):
        return None, None
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date")
    if len(df) == 0:
        return None, None
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else None
    return last, prev


def describe_allocation_change(prev_row, curr_row):
    if prev_row is None or curr_row is None:
        return (
            "No prior allocation history. (First run or file missing.)",
            None,  # main_flow
            None,
            None,
        )

    prev_w = {
        "btc": float(prev_row["w_btc"]),
        "alts": float(prev_row["w_alts"]),
        "stables": float(prev_row["w_stables"]),
    }
    curr_w = {
        "btc": float(curr_row["w_btc"]),
        "alts": float(curr_row["w_alts"]),
        "stables": float(curr_row["w_stables"]),
    }

    deltas = {k: curr_w[k] - prev_w[k] for k in ["btc", "alts", "stables"]}
    deltas_pct = {k: round(100 * v, 1) for k, v in deltas.items()}

    if all(abs(v) < 1.0 for v in deltas_pct.values()):
        text = (
            "No major reallocation today.\n"
            f"Yesterday: BTC {prev_w['btc']*100:.1f}%, ALTs {prev_w['alts']*100:.1f}%, "
            f"Stables {prev_w['stables']*100:.1f}%\n"
            f"Today:     BTC {curr_w['btc']*100:.1f}%, ALTs {curr_w['alts']*100:.1f}%, "
            f"Stables {curr_w['stables']*100:.1f}%"
        )
        return text, None, prev_w, curr_w

    # Find main source (most negative) and destination (most positive)
    src_asset = min(deltas, key=lambda k: deltas[k])
    dst_asset = max(deltas, key=lambda k: deltas[k])
    flow_size = min(abs(deltas_pct[src_asset]), abs(deltas_pct[dst_asset]))
    flow_size = round(flow_size, 1)

    asset_names = {"btc": "BTC", "alts": "ALTs", "stables": "Stables"}

    summary = (
        f"Yesterday: BTC {prev_w['btc']*100:.1f}%, ALTs {prev_w['alts']*100:.1f}%, "
        f"Stables {prev_w['stables']*100:.1f}%\n"
        f"Today:     BTC {curr_w['btc']*100:.1f}%, ALTs {curr_w['alts']*100:.1f}%, "
        f"Stables {curr_w['stables']*100:.1f}%"
    )

    main_flow = {
        "src": asset_names[src_asset],
        "dst": asset_names[dst_asset],
        "size": flow_size,
    }

    return summary, main_flow, prev_w, curr_w


def compute_roi_fields(last_row):
    """
    Returns:
      hive_equity, btc_equity, hive_roi_pct, hive_profit,
      btc_roi_pct, btc_profit, outperformance_multiple
    All ROI vs INITIAL_CAPITAL_FOR_ROI (100).
    """
    equity    = float(last_row["equity"])
    btc_only  = float(last_row["btc_only"])

    hive_profit = equity - INITIAL_CAPITAL_FOR_ROI
    btc_profit  = btc_only - INITIAL_CAPITAL_FOR_ROI

    hive_roi_pct = hive_profit / INITIAL_CAPITAL_FOR_ROI * 100.0
    btc_roi_pct  = btc_profit / INITIAL_CAPITAL_FOR_ROI * 100.0

    if btc_profit <= 0:
        outperf = None
    else:
        outperf = hive_profit / btc_profit

    return equity, btc_only, hive_roi_pct, hive_profit, btc_roi_pct, btc_profit, outperf


# ---------- Message Formatting ----------

def format_signal_message():
    # Load HMI
    hmi, hmi_date = load_latest_hmi()
    band_name, hmi_circle = hmi_band_name_and_emoji(hmi)

    # Load equity history
    last_row, prev_row = load_equity_snapshot()
    if last_row is None:
        raise RuntimeError("Equity curve file not found or empty; run backtest_dominance_rotation.py first.")

    # Balance & ROI
    (
        equity,
        btc_equity,
        hive_roi_pct,
        hive_profit,
        btc_roi_pct,
        btc_profit,
        outperf,
    ) = compute_roi_fields(last_row)

    # Dominance & alt-label
    btc_dom, alt_label = fetch_dominance_and_alt_label()
    btc_pct_dom = int(round(btc_dom * 100))
    alt_pct_dom = 100 - btc_pct_dom

    # Prices (order by market cap: BTC, ETH, SOL)
    prices = fetch_spot_prices()

    # Suggested allocation from LIVE dom + HMI (not from CSV)
    w = allocation_from_dom_and_hmi(btc_dom, hmi)
    sugg_btc = w["btc"] * 100
    sugg_alt = w["alts"] * 100
    sugg_stb = w["stables"] * 100

    # Dollar allocation from current balance
    btc_usd = equity * w["btc"]
    alt_usd = equity * w["alts"]
    stb_usd = equity * w["stables"]

    # Allocation change & action text
    flow_summary_text, main_flow, prev_w, curr_w = describe_allocation_change(prev_row, last_row)

    if main_flow is None:
        action_line = "No action (Δ<1%)."
    else:
        action_line = f"Rotate {main_flow['size']:.1f}% of {main_flow['src']} to {main_flow['dst']}."

    # Top line: Hive   14/11/25    💵 Balance: $xxx.xx
    date_str = hmi_date.strftime("%d/%m/%y")
    top_line = f"Hive   {date_str}    💵 Balance: ${equity:,.2f}"

    # ROI text
    hive_profit_sign = "+" if hive_profit >= 0 else "-"
    btc_profit_sign  = "+" if btc_profit >= 0 else "-"

    hive_roi_line = f"Hive ROI:  {hive_roi_pct:+.1f}% / {hive_profit_sign}${abs(hive_profit):,.2f}"
    btc_roi_line  = f"BTC buy & hold ROI: {btc_roi_pct:+.1f}% / {btc_profit_sign}${abs(btc_profit):,.2f}"

    if outperf is None or outperf <= 0:
        outperf_line = "Hive outperformance vs BTC buy & hold: n/a"
    else:
        outperf_line = f"Hive is outperforming BTC buy & hold by ~{outperf:.2f}x."

    text = (
        f"{top_line}\n\n"
        f"{hmi_circle} HMI: {hmi:.1f} — {band_name}\n"
        f"📊 BTC vs {alt_label}: {btc_pct_dom}/{alt_pct_dom}\n\n"
        f"💰 Prices:\n"
        f" • BTC: ${prices['BTC']:.0f}\n"
        f" • ETH: ${prices['ETH']:.0f}\n"
        f" • SOL: ${prices['SOL']:.2f}\n\n"
        f"🧠 Suggested allocation:\n"
        f" • BTC: {sugg_btc:.1f}% - ${btc_usd:,.2f}\n"
        f" • ALTs: {sugg_alt:.1f}% - ${alt_usd:,.2f}\n"
        f" • Stables: {sugg_stb:.1f}% - ${stb_usd:,.2f}\n\n"
        f"⚙️ Action: {action_line}\n\n"
        f"🔁 Portfolio Flow:\n"
        f"{flow_summary_text}\n\n"
        f"🧠 {hive_roi_line}\n"
        f"{btc_roi_line}\n"
        f"{outperf_line}"
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
    
